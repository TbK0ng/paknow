"""Hybrid retrieval: dense + sparse recall fused with RRF, optional rerank,
and evidence expansion (adjacent units + linked figures).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from qdrant_client.models import (
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


def expand_evidence(evidence_ids: list[str]) -> dict[str, Any]:
    """Fetch full units + linked figures + adjacent units from the DB."""
    with SessionLocal() as s:
        rows = {r.evidence_id: r for r in s.query(EvidenceUnitRow).filter(EvidenceUnitRow.evidence_id.in_(evidence_ids)).all()}
        fig_ids: set[str] = set()
        for r in rows.values():
            fig_ids.update(r.figure_ids or [])
        figs = {f.figure_id: f for f in s.query(FigureAssetRow).filter(FigureAssetRow.figure_id.in_(fig_ids)).all()} if fig_ids else {}
    units = [
        {
            "evidence_id": r.evidence_id,
            "doc_id": r.doc_id,
            "block_type": r.block_type,
            "text": r.text,
            "chapter_path": r.chapter_path or [],
            "pdf_page": r.pdf_page_1based,
            "printed_page": (r.locator or {}).get("printed_page_label"),
            "figure_label": r.figure_label,
            "epub_href": r.epub_href,
            "figure_ids": r.figure_ids or [],
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
