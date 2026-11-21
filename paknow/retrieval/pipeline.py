"""Text evidence for the manually requested analysis of a non-candidate bar."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from qdrant_client.models import FieldCondition, Filter, MatchAny

from paknow.retrieval.qdrant_store import StoreLockedError
from paknow.retrieval.search import adjacent_evidence, diversify, hybrid_search, point_to_doc, rerank_best


@dataclass(slots=True)
class TextRetrieval:
    citable_units: list[dict[str, Any]]
    failures: list[str]


def _record(failures: list[str], step: str, exc: Exception) -> None:
    if isinstance(exc, StoreLockedError):
        raise exc
    failures.append(f"{step}:{type(exc).__name__}")


def retrieve_text(queries: Sequence[str], *, doc_ids: frozenset[str] | None) -> TextRetrieval:
    questions = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:6]
    if not questions:
        return TextRetrieval([], [])
    filter_ = Filter(must=[FieldCondition(key="doc_id", match=MatchAny(any=sorted(doc_ids)))]) if doc_ids else None
    hits: dict[str, dict] = {}
    by_query: dict[str, list[str]] = {}
    failures: list[str] = []
    for query in questions:
        try:
            for point in hybrid_search(query, top_k=40, query_filter=filter_):
                row = point_to_doc(point)
                if eid := row.get("evidence_id"):
                    hits.setdefault(eid, row)
                    by_query.setdefault(eid, []).append(query)
        except Exception as exc:
            _record(failures, "recall", exc)
    ranked = sorted(hits.values(), key=lambda row: row["score"] or 0.0, reverse=True)
    if len(ranked) >= 2:
        try:
            ranked = rerank_best(ranked[:80], by_query, fallback_query=questions[0], top_k=80)
        except Exception as exc:
            _record(failures, "rerank", exc)
    ids = [row["evidence_id"] for row in diversify(ranked, top_k=10)]
    if ids:
        try:
            ids.extend(adjacent_evidence(ids))
        except Exception as exc:
            _record(failures, "adjacency", exc)
    # Citation hydration, including scope, provenance, and full source text,
    # always runs against the authoritative SQLite rows in the caller.
    return TextRetrieval([{"evidence_id": eid} for eid in ids], failures)
