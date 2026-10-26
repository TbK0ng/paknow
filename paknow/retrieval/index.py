"""Text indexing: evidence units -> Qdrant (dense + sparse hybrid).

Batches over the DB, embeds with BGE-M3, computes lexical sparse weights, and
upserts into the hybrid collection. Footer/page-chrome blocks are skipped.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator

from qdrant_client.models import PointStruct, SparseVector

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.retrieval.embedding import embed_texts
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, ensure_collections, get_client
from paknow.retrieval.tokenize import sparse_indices_values

_BATCH = 64


def iter_units(batch_size: int = _BATCH, doc_id: str | None = None) -> Iterator[list[EvidenceUnitRow]]:
    """Stream non-footer evidence units in stable order (optionally one doc)."""
    with SessionLocal() as s:
        q = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.block_type != "footer")
        if doc_id:
            q = q.filter(EvidenceUnitRow.doc_id == doc_id)
        total = q.count()
        for offset in range(0, total, batch_size):
            rows = (
                q.order_by(EvidenceUnitRow.evidence_id)
                .offset(offset)
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            yield rows


def _point_id(evidence_id: str) -> str:
    return str(uuid.UUID(hex=evidence_id))


def _point(row: EvidenceUnitRow, dense: list[float], sparse: SparseVector) -> PointStruct:
    return PointStruct(
        id=_point_id(row.evidence_id),
        vector={"dense": dense, "sparse": sparse},
        payload={
            "evidence_id": row.evidence_id,
            "doc_id": row.doc_id,
            "block_type": row.block_type,
            "text": row.text,
            "figure_ids": row.figure_ids or [],
            "chapter_path": row.chapter_path or [],
            "pdf_page": row.pdf_page_1based,
            "figure_label": row.figure_label,
            "epub_href": row.epub_href,
        },
    )


def _index_rows(rows: list[EvidenceUnitRow]) -> int:
    client = get_client()
    ensure_collections(client)
    # Chunked parents are reading context only: their children carry the text
    # that should match a query, so indexing the parent would double-recall it.
    rows = [r for r in rows if not r.child_ids]
    texts = [r.text for r in rows]
    dense = embed_texts(texts)
    points: list[PointStruct] = []
    for i, r in enumerate(rows):
        if not texts[i].strip():
            continue
        idx, vals = sparse_indices_values(r.text)
        points.append(
            _point(r, dense[i].tolist(), SparseVector(indices=idx, values=vals))
        )
    if points:
        client.upsert(collection_name=COLLECTION_TEXT, points=points, wait=True)
    return len(points)


def index_doc(doc_id: str) -> int:
    """Index only the units of one document (incremental ingest)."""
    n = 0
    for rows in iter_units(doc_id=doc_id):
        n += _index_rows(rows)
    return n


def index_all() -> int:
    n = 0
    for rows in iter_units():
        n += _index_rows(rows)
    return n
