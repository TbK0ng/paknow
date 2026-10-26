"""Graph expansion: non-scored, non-citable recall along concept/claim edges.

The text channel decides rank and citation; expansion only widens the material
available for understanding. Seeds are concepts hit by the query dictionary
(issued alongside text recall) plus concepts found in text hits (second pass).
From each seed the planner follows two kinds of edge:

- concept taxonomy, one hop (``PART_OF`` / ``EXCEPTION_TO`` /
  ``INVALIDATED_BY`` / ``TRANSITIONS_TO``), hydrated from the heading evidence
  that proved the edge;
- mechanism chains, concept -> aligned claim -> ``SUPPORTED_BY`` evidence, two
  hops.

Selection is deterministic and bounded: concepts <= 8, edges <= 32, evidence
<= 8, graph block <= 1,200 estimated tokens. Mechanism entries outrank
taxonomy entries and candidates keep relevance order (seed order, then claim
confidence), so truncation never promotes a less relevant edge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import or_

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, EvidenceUnitRow, GraphEdgeRow, GraphNodeRow
from paknow.graph.contracts import EdgeOrigin
from paknow.graph.identity import concept_node_id, label_for, resolved_canonical
from paknow.graph.taxonomy import TAXONOMY_RELATIONS
from paknow.retrieval.dictionary import find_terms

MAX_SEED_CONCEPTS = 8
MAX_EDGES = 32
MAX_EVIDENCE = 14
MAX_CONTEXT_TOKENS = 1200

_WORD = re.compile(r"[A-Za-z0-9_]+")


def seed_concepts(text: str, *, limit: int = MAX_SEED_CONCEPTS) -> list[str]:
    """Canonical concepts mentioned in ``text``, first mention first, deduped."""
    if not text:
        return []
    out: list[str] = []
    for match in find_terms(text):
        canonical = resolved_canonical(match.alias, match.canonical)
        if canonical and canonical not in out:
            out.append(canonical)
        if len(out) >= limit:
            break
    return out


def concepts_in_units(
    units: Iterable[Mapping[str, Any]], *, limit: int = MAX_SEED_CONCEPTS
) -> list[str]:
    """Concepts mentioned in already-recalled evidence, in rank order."""
    out: list[str] = []
    for unit in units:
        for canonical in seed_concepts(str(unit.get("text") or ""), limit=limit):
            if canonical not in out:
                out.append(canonical)
            if len(out) >= limit:
                return out
    return out


def select_new_seeds(
    *,
    query_seeds: Sequence[str] = (),
    text_units: Iterable[Mapping[str, Any]] = (),
    expanded: Iterable[str] = (),
    limit: int = MAX_SEED_CONCEPTS,
) -> list[str]:
    """Seeds for this round: query concepts first, then hit-evidence concepts.

    Concepts already expanded in an earlier round are skipped, so a gap round
    only pays for genuinely new material.
    """
    prior = set(expanded)
    seeds: list[str] = []
    for canonical in [*query_seeds, *concepts_in_units(text_units)]:
        if canonical in prior or canonical in seeds:
            continue
        seeds.append(canonical)
        if len(seeds) >= limit:
            break
    return seeds


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate: one per CJK char or ASCII word."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + len(_WORD.findall(text))


# ---------------------------------------------------------------------------
# pure planner
# ---------------------------------------------------------------------------


@dataclass
class PlannedContext:
    entries: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(
        default_factory=lambda: {
            "candidates": 0,
            "selected": 0,
            "truncated_edges": False,
            "truncated_evidence": False,
            "truncated_tokens": False,
            "tokens": 0,
        }
    )


def _entry(candidate: Mapping[str, Any], *, seen_index: int | None) -> dict[str, Any]:
    return {
        "evidence_id": candidate.get("evidence_id"),
        "doc_id": candidate.get("doc_id"),
        "text": "" if seen_index is not None else (candidate.get("text") or ""),
        "chapter_path": list(candidate.get("chapter_path") or []),
        "pdf_page": candidate.get("pdf_page"),
        "printed_page": candidate.get("printed_page"),
        "epub_href": candidate.get("epub_href"),
        "figure_label": candidate.get("figure_label"),
        "provenance": "graph",
        "non_citable": True,
        "source": candidate.get("source"),
        "relation": candidate.get("relation"),
        "from_label": candidate.get("from_label"),
        "to_label": candidate.get("to_label"),
        "claim": candidate.get("claim"),
        "origin": candidate.get("origin"),
        "concept": candidate.get("concept"),
        "seen_index": seen_index,
    }


def plan_context(
    *,
    mechanism: Sequence[Mapping[str, Any]],
    taxonomy: Sequence[Mapping[str, Any]],
    seen_index: Mapping[str, int] | None = None,
    max_edges: int = MAX_EDGES,
    max_evidence: int = MAX_EVIDENCE,
    max_tokens: int = MAX_CONTEXT_TOKENS,
) -> PlannedContext:
    """Select graph entries under the edge/evidence/token budgets.

    Mechanism candidates are considered before taxonomy ones. Every candidate
    is one traversed edge, so the edge budget binds on candidates examined,
    not on entries selected. Evidence already present in the citable text is
    kept as a ``seen_index`` marker with no body text, preserving the citation
    binding without duplicating content.
    """
    seen = dict(seen_index or {})
    plan = PlannedContext()
    chosen: set[str] = set()
    edges = 0
    tokens = 0
    stop = False

    for candidates in (mechanism, taxonomy):
        if stop:
            break
        for candidate in candidates:
            plan.stats["candidates"] += 1
            if edges >= max_edges:
                plan.stats["truncated_edges"] = True
                stop = True
                break
            edges += 1  # this candidate is an edge traversal, selected or not
            evidence_id = candidate.get("evidence_id")
            if not evidence_id or str(evidence_id) in chosen:
                continue
            if len(plan.entries) >= max_evidence:
                plan.stats["truncated_evidence"] = True
                stop = True
                break
            index = seen.get(str(evidence_id))
            text = "" if index is not None else str(candidate.get("text") or "")
            cost = estimate_tokens(text) + 6  # per-line label/locator overhead
            if tokens + cost > max_tokens:
                plan.stats["truncated_tokens"] = True
                stop = True
                break
            tokens += cost
            chosen.add(str(evidence_id))
            plan.entries.append(_entry(candidate, seen_index=index))

    plan.stats["selected"] = len(plan.entries)
    plan.stats["tokens"] = tokens
    return plan


# ---------------------------------------------------------------------------
# database hydration
# ---------------------------------------------------------------------------


def _locator(row: EvidenceUnitRow) -> dict[str, Any]:
    return {
        "doc_id": row.doc_id,
        "chapter_path": list(row.chapter_path or []),
        "pdf_page": row.pdf_page_1based,
        "printed_page": (row.locator or {}).get("printed_page_label"),
        "epub_href": row.epub_href,
        "figure_label": row.figure_label,
    }


def load_mechanism_candidates(
    seeds: Sequence[str],
    *,
    limit: int = MAX_EDGES,
    session_factory=None,
) -> list[dict[str, Any]]:
    """Concept -> aligned claim -> SUPPORTED_BY evidence, in relevance order."""
    canonical = [s for s in seeds if s]
    if not canonical:
        return []
    order = {concept: i for i, concept in enumerate(canonical)}
    factory = session_factory or SessionLocal

    with factory() as session:
        claims = (
            session.query(ClaimRow)
            .filter(ClaimRow.subject_concept_id.in_(canonical))
            .all()
        )
        claims.sort(
            key=lambda c: (
                order.get(c.subject_concept_id, len(order)),
                -(c.extraction_confidence or 0.0),
                c.claim_id,
            )
        )
        pairs: list[tuple[ClaimRow, str]] = []
        for claim in claims:
            for evidence_id in claim.supporting_evidence or []:
                if not evidence_id:
                    continue
                pairs.append((claim, evidence_id))
                if len(pairs) >= limit:
                    break
            if len(pairs) >= limit:
                break
        evidence_rows = {
            row.evidence_id: row
            for row in session.query(EvidenceUnitRow)
            .filter(EvidenceUnitRow.evidence_id.in_({eid for _c, eid in pairs}))
            .all()
        } if pairs else {}

    out: list[dict[str, Any]] = []
    for claim, evidence_id in pairs:
        row = evidence_rows.get(evidence_id)
        if row is None:
            continue
        loc = _locator(row)
        concept = claim.subject_concept_id or ""
        out.append(
            {
                "evidence_id": row.evidence_id,
                "text": row.text,
                "source": "mechanism",
                "relation": "SUPPORTED_BY",
                "from_label": label_for(concept) if concept else claim.subject,
                "to_label": claim.predicate,
                "claim": {
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "qualifiers": claim.qualifiers or {},
                },
                "origin": EdgeOrigin.claim_extract.value,
                "concept": concept,
                **loc,
            }
        )
    return out[:limit]


def load_taxonomy_candidates(
    seeds: Sequence[str],
    *,
    limit: int = MAX_EDGES,
    session_factory=None,
) -> list[dict[str, Any]]:
    """One-hop taxonomy edges touching a seed, hydrated from heading evidence."""
    canonical = [s for s in seeds if s]
    if not canonical:
        return []
    order = {concept: i for i, concept in enumerate(canonical)}
    node_ids = {concept_node_id(concept) for concept in canonical}
    factory = session_factory or SessionLocal

    with factory() as session:
        edges = (
            session.query(GraphEdgeRow)
            .filter(
                GraphEdgeRow.relation.in_(TAXONOMY_RELATIONS),
                or_(
                    GraphEdgeRow.from_node.in_(node_ids),
                    GraphEdgeRow.to_node.in_(node_ids),
                ),
            )
            .all()
        )

        def edge_key(edge: GraphEdgeRow) -> tuple[int, str, str]:
            starts = edge.from_node in node_ids
            seed_node = edge.from_node if starts else edge.to_node
            index = len(order)
            for concept, position in order.items():
                if concept_node_id(concept) == seed_node:
                    index = position
                    break
            return (index, edge.relation, edge.edge_id)

        edges.sort(key=edge_key)
        edges = edges[:limit]
        node_rows = {
            row.node_id: row
            for row in session.query(GraphNodeRow)
            .filter(
                GraphNodeRow.node_id.in_(
                    {edge.from_node for edge in edges} | {edge.to_node for edge in edges}
                )
            )
            .all()
        } if edges else {}
        evidence_rows = {
            row.evidence_id: row
            for row in session.query(EvidenceUnitRow)
            .filter(EvidenceUnitRow.evidence_id.in_({edge.source for edge in edges}))
            .all()
        } if edges else {}

    def label(node_id: str) -> str:
        node = node_rows.get(node_id)
        return (node.label or node.concept_id or node_id) if node else node_id

    out: list[dict[str, Any]] = []
    for edge in edges:
        row = evidence_rows.get(edge.source)
        if row is None:
            continue
        loc = _locator(row)
        starts = edge.from_node in node_ids
        concept = next(
            (
                c
                for c in canonical
                if concept_node_id(c) == (edge.from_node if starts else edge.to_node)
            ),
            "",
        )
        out.append(
            {
                "evidence_id": row.evidence_id,
                "text": row.text,
                "source": "taxonomy",
                "relation": edge.relation,
                "from_label": label(edge.from_node),
                "to_label": label(edge.to_node),
                "claim": None,
                "origin": edge.origin,
                "concept": concept,
                **loc,
            }
        )
    return out


def expand(
    seeds: Sequence[str],
    *,
    seen_index: Mapping[str, int] | None = None,
    session_factory=None,
) -> tuple[PlannedContext, list[str]]:
    """Plan one expansion round; every failing part is reported, never raised."""
    failures: list[str] = []
    mechanism: list[dict[str, Any]] = []
    taxonomy: list[dict[str, Any]] = []
    try:
        mechanism = load_mechanism_candidates(seeds, session_factory=session_factory)
    except Exception as exc:  # noqa: BLE001 - expansion is fail-open
        failures.append(f"graph_expansion.mechanism: {type(exc).__name__}: {exc}")
    try:
        taxonomy = load_taxonomy_candidates(seeds, session_factory=session_factory)
    except Exception as exc:  # noqa: BLE001 - expansion is fail-open
        failures.append(f"graph_expansion.taxonomy: {type(exc).__name__}: {exc}")
    plan = plan_context(mechanism=mechanism, taxonomy=taxonomy, seen_index=seen_index)
    return plan, failures
