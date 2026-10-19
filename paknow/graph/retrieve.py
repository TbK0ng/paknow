"""Evidence-graph retrieval: bounded BFS from hit evidence to related claims (plan §6.3)."""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, GraphEdgeRow


def expand_evidence_ids(evidence_ids: list[str], max_hops: int = 2, limit: int = 20) -> list[dict[str, Any]]:
    """From hit evidence units, follow SUPPORTED_BY edges to claims, then to
    other evidence. Returns related evidence ids + claim summaries."""
    if not evidence_ids:
        return []
    with SessionLocal() as s:
        # claims supported by these evidence units
        claims = (
            s.query(ClaimRow)
            .filter(ClaimRow.supporting_evidence.overlap(evidence_ids))
            .limit(limit)
            .all()
        )
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
