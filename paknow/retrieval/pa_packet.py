"""Final role-aware PA evidence packet, with one scorer for text and graph leads."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from itertools import product
import logging
import re
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny

from paknow.evidence.citations import citation_ready_hits, render_citation
from paknow.graph import pa_context
from paknow.graph.pa_context import Lead, Role, ROLES, Setup
from paknow.retrieval.pa_corpus import corpus
from paknow.retrieval.search import hybrid_search, point_to_doc, rerank_best

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class Packet:
    questions: dict[Role, str]
    sources: list[dict[str, Any]]
    gaps: list[Role]
    graph_status: str
    failures: list[str]
    generation: str
    query_plan: dict[Role, tuple[str, ...]] | None = None
    warnings: list[str] = field(default_factory=list)


def _dedupe_key(row: dict[str, Any]) -> tuple[str, str]:
    return row["passage_id"], re.sub(r"\s+", "", row["text"]).casefold()


def select_sources(ranked: dict[Role, list[dict]], verified: dict[Role, dict[str, Lead]]) -> tuple[list[dict], list[Role]]:
    chosen, chosen_ids, families, bodies = [], set(), set(), set()

    def add(row: dict, role: Role, lead: Lead | None) -> bool:
        eid = row["evidence_id"]
        family, body = _dedupe_key(row)
        if eid in chosen_ids or family in families or body in bodies:
            return False
        chosen_ids.add(eid)
        families.add(family)
        bodies.add(body)
        loc = row["locator"]
        # Keep the entire bounded passage, including its limiting clauses;
        # offsets are relative to this evidence row (locator offsets to parent).
        chosen.append({"id": eid, "label": render_citation(row, row["doc_title"]) or row["doc_id"],
                       "text": row["text"], "provenance": row["provenance"], "role": role,
                       "verified": lead is not None, "passage_id": family,
                       "span": [0, len(row["text"])], "locator": loc,
                       **({"graph": lead.reason()} if lead else {})})
        return True

    # Find an independent assignment before filling optional slots. A highly
    # ranked sibling for the first role must not hide the only valid risk.
    choices = []
    for role in ROLES:
        priorities = {l.profile: i for i, l in reversed(list(enumerate(verified[role].values())))}
        trusted = [r for r in ranked[role] if r["evidence_id"] in verified[role]]
        trusted.sort(key=lambda r: priorities[verified[role][r["evidence_id"]].profile])
        choices.append([*trusted, None])

    def independent(picks) -> bool:
        keys = [_dedupe_key(row) for row in picks if row is not None]
        return len(keys) == len({a for a, _ in keys}) == len({b for _, b in keys})

    # A bounded set of reviewed leads per role; ties keep planned/reranked order.
    assignment = max((rows for rows in product(*choices) if independent(rows)),
                     key=lambda rows: sum(r is not None for r in rows))
    gaps: list[Role] = []
    for role, row in zip(ROLES, assignment, strict=True):
        if row is None:
            gaps.append(role)
        else:
            add(row, role, verified[role][row["evidence_id"]])
    # A distinct second risk can expose e.g. both an edge and an EMA-proxy issue.
    for role in ("risk", "confirmation", "support"):
        eligible = [row for row in ranked[role] if row["evidence_id"] in verified[role]]
        if role == "risk":
            priority = {eid: i for i, eid in enumerate(verified[role])}
            eligible.sort(key=lambda row: priority[row["evidence_id"]])
        for row in eligible:
            if add(row, role, verified[role][row["evidence_id"]]):
                break
        if len(chosen) >= 5:
            return chosen, gaps
    for role in ROLES:
        for row in ranked[role]:
            if add(row, role, verified[role].get(row["evidence_id"])):
                break
        if len(chosen) >= 5:
            break
    return chosen, gaps


def retrieve_packet(setup: Setup, *, use_graph: bool = True) -> Packet:
    generation, allowed = corpus()
    fallback = pa_context.base_questions(setup)
    graph_status, leads = "disabled", ()
    if use_graph:
        try:
            leads = pa_context.plan(setup) if allowed else ()
            graph_status = "ok" if leads else "empty"
        except Exception as exc:
            _LOG.warning("PA graph planning unavailable (%s)", type(exc).__name__)
            graph_status = "unavailable"
    query_filter = Filter(must=[FieldCondition(key="doc_id", match=MatchAny(any=sorted(allowed)))]) if allowed else None
    failures, warnings, questions, query_plan, ranked, verified = [], [], {}, {}, {}, {}
    for role in ROLES:
        role_leads = [l for l in leads if l.role == role]
        exact = {l.evidence_id: l for l in role_leads}
        queries = tuple(dict.fromkeys([*(l.question for l in role_leads), fallback[role]]))
        question = questions[role] = queries[0]
        query_plan[role] = queries
        pool = {eid: {"evidence_id": eid} for eid in exact}
        by_query = {eid: [lead.question] for eid, lead in exact.items()}
        limit = (24 + len(queries) - 1) // len(queries)
        for query in queries:
            try:
                for raw in hybrid_search(query, top_k=limit, query_filter=query_filter):
                    row = point_to_doc(raw)
                    if not (eid := row["evidence_id"]):
                        continue
                    pool.setdefault(eid, row)
                    attributed = by_query.setdefault(eid, [])
                    if query not in attributed:
                        attributed.append(query)
            except Exception as exc:
                warnings.append(f"{role}:retrieve:{type(exc).__name__}")
        try:
            rows = [r for r in citation_ready_hits(list(pool.values()), doc_ids=allowed)
                    if r["block_type"] != "heading" and 35 <= len(r["text"].strip()) <= 2400]
        except Exception as exc:
            failures.append(f"{role}:citations:{type(exc).__name__}")
            rows = []
        hydrated = {r["evidence_id"]: r for r in rows}
        verified[role] = {eid: lead for eid, lead in exact.items() if eid in hydrated
                          and hydrated[eid]["text_hash"] == lead.sha256
                          and sha256(hydrated[eid]["text"].encode()).hexdigest() == lead.sha256}
        try:
            # Includes checked direct graph leads and RRF results in the same scoring pass.
            ranked[role] = rerank_best(rows, by_query, fallback_query=question) if len(rows) >= 2 else rows
        except Exception as exc:
            warnings.append(f"{role}:rerank:{type(exc).__name__}")
            ranked[role] = rows
    sources, gaps = select_sources(ranked, verified)
    parent_ids = {s["passage_id"] for s in sources if s["verified"] and s["passage_id"] != s["id"]}
    if parent_ids:
        try:
            parents = citation_ready_hits([{"evidence_id": eid} for eid in sorted(parent_ids)], doc_ids=allowed,
                                         allow_parent=True)
            by_id = {row["evidence_id"]: row for row in parents}
            for src in sources:
                if not src["verified"] or src["passage_id"] == src["id"]:
                    continue
                parent = by_id.get(src["passage_id"])
                if not parent or parent["doc_id"] != src["locator"].get("doc_id"):
                    failures.append(f"{src['role']}:missing_parent")
                    continue
                text = parent["text"]
                start = src["locator"].get("span_start")
                if start is None:
                    start = text.find(src["text"])
                end = start + len(src["text"])
                if start < 0 or text[start:end] != src["text"]:
                    failures.append(f"{src['role']}:invalid_parent_span")
                    continue
                src["reading_context"], src["reading_context_span"] = text, [0, len(text)]
                if len(text) > 2400:
                    # Keep the source and surrounding text; mark cut bounds instead of
                    # silently adding adjacent content to the citable excerpt.
                    before = max(0, min(start - (2400 - len(src["text"])) // 2, len(text) - 2400))
                    after = before + 2400
                    src["reading_context"], src["reading_context_span"] = text[before:after], [before, after]
        except Exception as exc:
            failures.append(f"parent_context:{type(exc).__name__}")
    if gaps:
        failures.extend(warnings)
    return Packet(questions, sources, gaps, graph_status, failures, query_plan=query_plan,
                  generation=generation, warnings=warnings)
