"""Hybrid text retrieval, cross-encoder reranking, and adjacent passage IDs."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from functools import lru_cache
import json
from hashlib import sha256
from math import isfinite
from typing import Any, Mapping, Sequence

from qdrant_client.models import (
    Filter,
    Fusion,
    FusionQuery,
    Prefetch,
    ScoredPoint,
)

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.retrieval.embedding import embed_query
from paknow.retrieval.pa_corpus import corpus
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, bm25_generation, get_client
from paknow.retrieval.runtime import inference, priority, qdrant_reads, rank_scores, recall
from paknow.retrieval.bm25 import VECTOR_NAME, query_vector


def hybrid_search(
    query_text: str,
    *,
    top_k: int = 40,
    dense_limit: int = 40,
    sparse_limit: int = 40,
    query_filter: Filter | None = None,
) -> list[ScoredPoint]:
    generation, _ = corpus()
    if generation == "unsealed":
        return _hybrid_search(query_text, top_k, dense_limit, sparse_limit, query_filter)
    filter_id = json.dumps(query_filter.model_dump(mode="json", exclude_none=True), sort_keys=True) if query_filter else None
    key = (settings.objects_dir, settings.qdrant_url, generation, bm25_generation(), settings.embed_model,
           query_text, top_k, dense_limit, sparse_limit, filter_id)

    def fetch(urgency):
        token = priority.set(urgency)
        try:
            return _hybrid_search(query_text, top_k, dense_limit, sparse_limit, query_filter)
        finally:
            priority.reset(token)

    return deepcopy(recall.get(key, fetch, priority.get()))


def _hybrid_search(query_text: str, top_k: int, dense_limit: int, sparse_limit: int,
                   query_filter: Filter | None) -> list[ScoredPoint]:
    dense = embed_query(query_text).tolist()
    lexical = query_vector(query_text)
    prefetch: list[Prefetch] = [Prefetch(query=dense, using="dense", filter=query_filter, limit=dense_limit)]
    if lexical.indices:
        prefetch.append(Prefetch(query=lexical, using=VECTOR_NAME, filter=query_filter, limit=sparse_limit))
    # Only the embedded vector query is serialized, not GPU encoding, text
    # planning, citation hydration, or waiting on a cached result.
    with (nullcontext() if settings.qdrant_url else qdrant_reads):
        return get_client().query_points(collection_name=COLLECTION_TEXT, prefetch=prefetch,
            query=FusionQuery(fusion=Fusion.RRF), query_filter=query_filter, limit=top_k, with_payload=True).points


@lru_cache(maxsize=1)
def _reranker():
    from sentence_transformers import CrossEncoder

    device = "cuda" if _cuda_ok() else "cpu"
    kwargs = {}
    if device == "cuda":
        import torch

        kwargs["dtype"] = torch.float16
    return CrossEncoder(settings.rerank_model, device=device, model_kwargs=kwargs)


def _cuda_ok() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def _predict_pairs(pairs: list[tuple[str, str]]) -> list[float]:
    scores = [float(s) for s in _reranker().predict(pairs, batch_size=6, show_progress_bar=False)]
    if not all(isfinite(s) for s in scores):
        raise ValueError("non-finite reranker score")
    return scores


def score_pairs(pairs: list[tuple[str, str]]) -> tuple[float, ...]:
    if not pairs:
        return ()
    urgency = priority.get()
    # Hash exact UTF-8 text, not bare evidence IDs that may be reused after a rebuild.
    by_key = {(settings.rerank_model, q, sha256(t.encode()).digest()): (q, t) for q, t in pairs}
    keys = [(settings.rerank_model, q, sha256(t.encode()).digest()) for q, t in pairs]
    return tuple(rank_scores.get_many(keys, lambda missing, owner:
        inference.call("rerank", [by_key[k] for k in missing], _predict_pairs, owner), urgency))


def rerank_best(
    docs: list[dict[str, Any]],
    queries: Mapping[str, Sequence[str]],
    *,
    fallback_query: str,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Rerank each doc against the queries that actually retrieved it.

    A unit found by a step-back or sub-query scores badly against the rewritten
    question, so scoring every candidate with that one question demotes real
    evidence. Each doc is scored against every query that returned it and keeps
    its best score; a doc carried over from an earlier round has no attribution
    and falls back to ``fallback_query``.
    """
    if not docs:
        return []
    cohorts: dict[str, list[int]] = {}
    for i, d in enumerate(docs):
        for q in queries.get(str(d.get("evidence_id"))) or [fallback_query]:
            cohorts.setdefault(q, []).append(i)
    flat = [(q, i) for q, subset in cohorts.items() for i in subset]
    best: dict[int, float] = {}
    for (_, i), s in zip(flat, score_pairs([(q, docs[i]["text"]) for q, i in flat]), strict=True):
        best[i] = max(best.get(i, -float("inf")), s)
    order = sorted(best, key=lambda i: best[i], reverse=True)
    if top_k:
        order = order[:top_k]
    return [docs[i] for i in order]


def diversify(
    hits: list[dict[str, Any]], *, top_k: int = 14, max_per_doc: int = 8, dedupe_chars: int = 200
) -> list[dict[str, Any]]:
    """Near-duplicate suppression + per-document cap so one book/edition
    cannot flood the evidence window (plan §5.4 去重分组).

    Compare leading normalized text, and cap units per document to leave
    room for other sources.
    """
    import re

    def norm(text: str) -> str:
        return re.sub(r"\s+", "", text)[:dedupe_chars].lower()

    seen_dup: set[str] = set()
    doc_counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for h in hits:
        eid = h.get("evidence_id")
        if not eid:
            out.append(h)
            continue
        key = norm(h.get("text") or "")
        if key in seen_dup:
            continue
        doc = h.get("doc_id") or eid
        if doc_counts.get(doc, 0) >= max_per_doc:
            continue
        seen_dup.add(key)
        doc_counts[doc] = doc_counts.get(doc, 0) + 1
        out.append(h)
        if len(out) >= top_k:
            break
    return out


def point_to_doc(p: ScoredPoint) -> dict[str, Any]:
    pl = p.payload or {}
    return {
        "evidence_id": pl.get("evidence_id"),
        "doc_id": pl.get("doc_id"),
        "block_type": pl.get("block_type"),
        "text": pl.get("text", ""),
        "score": p.score,
    }


def adjacent_evidence(evidence_ids: list[str], *, limit: int = 4) -> list[str]:
    """Look up nearby text for ordinary analysis; split parents are not sources."""
    with SessionLocal() as s:
        ordered = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.evidence_id.in_(evidence_ids)).all()
        by_id = {row.evidence_id: row for row in ordered}
        seen = set(evidence_ids) | {row.parent_id for row in ordered if row.parent_id}
        neighbors = []
        for eid in evidence_ids:
            if (row := by_id.get(eid)) is None:
                continue
            for neighbor in row.adjacent_ids or []:
                if neighbor not in seen:
                    seen.add(neighbor)
                    neighbors.append(neighbor)
                    if len(neighbors) == limit:
                        return neighbors
        return neighbors
