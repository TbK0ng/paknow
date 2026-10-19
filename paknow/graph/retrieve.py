"""Evidence-graph retrieval: concept lookup, cycle chains, and bounded BFS from
hit evidence to related claims (plan §6.3)."""
from __future__ import annotations

import re
from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, GraphEdgeRow, GraphNodeRow
from paknow.retrieval import dictionary


def expand_evidence_ids(evidence_ids: list[str], max_hops: int = 2, limit: int = 20) -> list[dict[str, Any]]:
    """From hit evidence units, follow SUPPORTED_BY edges to claims, then to
    other evidence. Returns related evidence ids + claim summaries."""
    if not evidence_ids:
        return []
    ev_set = set(evidence_ids)
    with SessionLocal() as s:
        # claims supported by these evidence units (SQLite JSON: filter in Python)
        claims = [
            c
            for c in s.query(ClaimRow).all()
            if ev_set & set(c.supporting_evidence or [])
        ][:limit]
        related: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in claims:
            related.append(
                {
                    "claim_id": c.claim_id,
                    "subject": c.subject,
                    "predicate": c.predicate,
                    "qualifiers": c.qualifiers or {},
                }
            )
            seen.add(c.claim_id)
        # 1-hop: evidence referenced by those claims
        if max_hops >= 2 and claims:
            claim_ids = [c.claim_id for c in claims]
            edges = (
                s.query(GraphEdgeRow)
                .filter(GraphEdgeRow.from_node.in_(claim_ids), GraphEdgeRow.relation == "SUPPORTED_BY")
                .limit(limit)
                .all()
            )
            for e in edges:
                if e.to_node not in evidence_ids:
                    related.append({"evidence_id": e.to_node, "via": e.from_node})
        return related


def concept_lookup(terms: list[str], limit: int = 12) -> list[dict[str, Any]]:
    """Match query terms (via the term dictionary) to concept nodes + claims."""
    raw = [t.strip() for t in terms if t and t.strip()]
    if not raw:
        return []
    joined = " ".join(raw)
    exp = dictionary.expand_query(joined)
    # all alias forms of matched dictionary terms bridge query -> node labels
    forms = sorted(set(exp["canonical"]) | set(exp["zh"]) | set(exp["en"]), key=len, reverse=True)
    forms += [t for t in raw if len(t) <= 12 and t not in forms]
    if not forms:
        return []
    with SessionLocal() as s:
        nodes = (
            s.query(GraphNodeRow)
            .filter(GraphNodeRow.node_type == "concept")
            .all()
        )
        matched_nodes: list[GraphNodeRow] = []
        for n in nodes:
            target = f"{n.label or ''} {n.concept_id or ''}"
            if any(_term_alias_match(f, target) for f in forms):
                matched_nodes.append(n)
        labels = {n.label for n in matched_nodes} | {n.concept_id for n in matched_nodes}
        # claims ranked by: subject match > predicate match > condition-only match
        scored_claims: list[tuple[int, ClaimRow]] = []
        for c in s.query(ClaimRow).all():
            if c.subject in labels:
                scored_claims.append((3, c))
                continue
            subj, pred = c.subject or "", c.predicate or ""
            subj_hit = any(_term_alias_match(f, subj) for f in forms)
            pred_hit = any(_term_alias_match(f, pred) for f in forms)
            ctx_vals = (c.qualifiers or {}).get("context") or []
            if isinstance(ctx_vals, str):
                ctx_vals = [ctx_vals]
            ctx_hit = any(
                _term_alias_match(f, str(v)) for v in ctx_vals for f in forms
            )
            if subj_hit:
                scored_claims.append((2, c))
            elif pred_hit:
                scored_claims.append((1, c))
            elif ctx_hit:
                scored_claims.append((0, c))
        scored_claims.sort(key=lambda x: -x[0])
        claims = [c for _score, c in scored_claims[:limit]]
    out: list[dict[str, Any]] = []
    for n in matched_nodes[:limit]:
        out.append(
            {"type": "concept", "id": n.node_id, "label": n.label, "concept_id": n.concept_id}
        )
    for c in claims:
        out.append(
            {
                "type": "claim",
                "id": c.claim_id,
                "subject": c.subject,
                "predicate": c.predicate,
                "qualifiers": c.qualifiers or {},
                "supporting_evidence": c.supporting_evidence or [],
            }
        )
    return out


def _term_alias_match(needle: str, target: str) -> bool:
    """Substring match with short-alias protection (exact-only below 3 chars)."""
    needle = needle.strip()
    if not needle:
        return False
    if needle[0].isascii():
        if len(needle) < 3:
            return needle == target
        low = target.lower()
        for m in re.finditer(re.escape(needle.lower()), low):
            s, e = m.start(), m.end()
            before = low[s - 1] if s > 0 else ""
            after = low[e] if e < len(low) else ""
            if not (before.isalnum() or after.isalnum()):
                return True
        return False
    return needle in target


def transition_chains(max_len: int = 6) -> list[dict[str, Any]]:
    """All TRANSITIONS_TO chains (tiny subgraph; cycle-aware, label-resolved)."""
    with SessionLocal() as s:
        nodes = s.query(GraphNodeRow).all()
        id_label = {n.node_id: (n.label or n.concept_id or n.node_id) for n in nodes}
        edges = (
            s.query(GraphEdgeRow)
            .filter(GraphEdgeRow.relation == "TRANSITIONS_TO")
            .all()
        )
        adj: dict[str, str] = {}
        for e in edges:
            adj.setdefault(e.from_node, e.to_node)
        out: list[dict[str, Any]] = []
        for start in adj:
            path = [start]
            cur = start
            seen = {start}
            cycle = False
            for _ in range(max_len):
                nxt = adj.get(cur)
                if nxt is None:
                    break
                if nxt in seen:
                    path.append(nxt)
                    cycle = True
                    break
                seen.add(nxt)
                path.append(nxt)
                cur = nxt
            out.append(
                {
                    "start": id_label.get(start, start),
                    "path": [id_label.get(x, x) for x in path],
                    "cycle": cycle,
                }
            )
        return out
