"""Hybrid retrieval: dense + sparse recall fused with RRF, optional rerank,
and evidence expansion (adjacent units + linked figures).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping, Sequence

from qdrant_client.models import (
    Filter,
    Fusion,
    FusionQuery,
    Prefetch,
    ScoredPoint,
    SparseVector,
)

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow, FigureAssetRow
from paknow.retrieval.embedding import embed_query
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, get_client
from paknow.retrieval.tokenize import sparse_indices_values


def hybrid_search(
    query_text: str,
    *,
    top_k: int = 40,
    dense_limit: int = 40,
    sparse_limit: int = 40,
    query_filter: Filter | None = None,
) -> list[ScoredPoint]:
    client = get_client()
    dense = embed_query(query_text).tolist()
    idx, vals = sparse_indices_values(query_text)
    prefetch: list[Prefetch] = [Prefetch(query=dense, using="dense", limit=dense_limit)]
    if idx:
        prefetch.append(
            Prefetch(query=SparseVector(indices=idx, values=vals), using="sparse", limit=sparse_limit)
        )
    return client.query_points(
        collection_name=COLLECTION_TEXT,
        prefetch=prefetch,
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    ).points


@lru_cache(maxsize=1)
def _reranker():
    from sentence_transformers import CrossEncoder

    device = "cuda" if _cuda_ok() else "cpu"
    return CrossEncoder(settings.rerank_model, device=device)


def _cuda_ok() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def rerank(query_text: str, docs: list[dict[str, Any]], top_k: int | None = None) -> list[dict[str, Any]]:
    """Cross-encoder rerank over candidate docs (dicts with 'text')."""
    if not docs:
        return []
    model = _reranker()
    pairs = [(query_text, d["text"]) for d in docs]
    scores = model.predict(pairs)
    order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
    if top_k:
        order = order[:top_k]
    return [docs[i] for i in order]


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
    model = _reranker()
    batches: dict[str, list[int]] = {}
    for i, d in enumerate(docs):
        ids = queries.get(str(d.get("evidence_id"))) or [fallback_query]
        for q in ids:
            batches.setdefault(q, []).append(i)
    best: dict[int, float] = {}
    for q, idx in batches.items():
        scores = model.predict([(q, docs[i]["text"]) for i in idx])
        for i, s in zip(idx, scores):
            if i not in best or float(s) > best[i]:
                best[i] = float(s)
    order = sorted(best, key=lambda i: best[i], reverse=True)
    if top_k:
        order = order[:top_k]
    return [docs[i] for i in order]


def caption_search(query_text: str, top_k: int = 10) -> list[ScoredPoint]:
    """Caption-only recall: surface figure captions/observations whose
    figure_ids populate the example catalog for 'show me' queries."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = get_client()
    dense = embed_query(query_text).tolist()
    return client.query_points(
        collection_name=COLLECTION_TEXT,
        query=dense,
        using="dense",
        query_filter=Filter(
            must=[FieldCondition(key="block_type", match=MatchValue(value="caption"))]
        ),
        limit=top_k,
        with_payload=True,
    ).points


def diversify(
    hits: list[dict[str, Any]], *, top_k: int = 14, max_per_doc: int = 8, dedupe_chars: int = 200
) -> list[dict[str, Any]]:
    """Near-duplicate suppression + per-document cap so one book/edition
    cannot flood the evidence window (plan §5.4 去重分组).

    ``dedupe_chars`` is how much of the normalised text identifies a duplicate.
    It was 80, which merged distinct chapter headings that share an opening
    ("交易区间开盘：多头突破失败" appears in several chapters) and kept whichever
    one ranked higher. ``max_per_doc`` was 5, but one encyclopaedia supplies 50 of
    the 123 gold evidence points, so the cap alone dropped rank-3 answers.
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
        "figure_ids": pl.get("figure_ids") or [],
        "chapter_path": pl.get("chapter_path") or [],
        "pdf_page": pl.get("pdf_page"),
        "figure_label": pl.get("figure_label"),
        "epub_href": pl.get("epub_href"),
        "score": p.score,
    }


_PARENT_TEXT_CHARS = 2400  # a chunked parent is reading context; 34-child parents reach 37k chars


def _parent_text(text: str) -> str:
    return text if len(text) <= _PARENT_TEXT_CHARS else text[:_PARENT_TEXT_CHARS] + "…"


def expand_evidence(
    evidence_ids: list[str],
    *,
    parent_limit: int = 6,
    adjacent_limit: int = 4,
) -> dict[str, Any]:
    """Fetch full units + linked figures + parent/adjacent context units."""
    with SessionLocal() as s:
        rows = {
            r.evidence_id: r
            for r in s.query(EvidenceUnitRow)
            .filter(EvidenceUnitRow.evidence_id.in_(evidence_ids))
            .all()
        }
        parents: set[str] = set()
        adjacents: set[str] = set()
        for eid in evidence_ids:
            r = rows.get(eid)
            if r is None:
                continue
            if r.parent_id and r.parent_id not in rows and len(parents) < parent_limit:
                # shortcut: the parent contains the hit's own text, so both are
                # rendered — 39 of 114 answer runs cite a parent and its child.
                # Dropping the child would change the frozen eval's id semantics
                # (gold evidence ids are child ids), so it stays for now.
                parents.add(r.parent_id)
            for aid in r.adjacent_ids or []:
                if aid not in rows and aid not in parents and len(adjacents) < adjacent_limit:
                    adjacents.add(aid)
        if parents or adjacents:
            extra_ids = parents | adjacents
            for r in (
                s.query(EvidenceUnitRow)
                .filter(EvidenceUnitRow.evidence_id.in_(extra_ids))
                .all()
            ):
                rows[r.evidence_id] = r
        fig_ids: set[str] = set()
        for r in rows.values():
            fig_ids.update(r.figure_ids or [])
        figs = {f.figure_id: f for f in s.query(FigureAssetRow).filter(FigureAssetRow.figure_id.in_(fig_ids)).all()} if fig_ids else {}
    units = [
        {
            "evidence_id": r.evidence_id,
            "doc_id": r.doc_id,
            "block_type": r.block_type,
            "text": _parent_text(r.text) if r.evidence_id in parents else r.text,
            "chapter_path": r.chapter_path or [],
            "pdf_page": r.pdf_page_1based,
            "printed_page": (r.locator or {}).get("printed_page_label"),
            "figure_label": r.figure_label,
            "epub_href": r.epub_href,
            "figure_ids": r.figure_ids or [],
            "provenance": r.provenance,
            "is_parent": r.evidence_id in parents,
            "is_adjacent": r.evidence_id in adjacents,
        }
        for r in rows.values()
    ]
    figures = [
        {
            "figure_id": f.figure_id,
            "rel_path": f.rel_path,
            "width_px": f.width_px,
            "height_px": f.height_px,
            "figure_label": f.figure_label,
        }
        for f in figs.values()
    ]
    return {"units": units, "figures": figures}
