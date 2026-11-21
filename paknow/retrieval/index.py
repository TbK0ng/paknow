"""Build and maintain the BGE-M3 + BM25 text collection."""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from hashlib import sha256
from itertools import islice
from tempfile import TemporaryFile

from qdrant_client import QdrantClient
from qdrant_client.models import Modifier, PointStruct, PointVectors, UpdateStatus

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.retrieval.bm25 import VECTOR_NAME, document_vector, lexical_counts
from paknow.retrieval.embedding import embed_texts
from paknow.retrieval.qdrant_store import (COLLECTION_TEXT, bm25_vector_count, ensure_collections,
                                          get_client, invalidate_bm25, publish_bm25)

_BATCH = 64
TEXT_PAYLOAD_KEYS = frozenset({"evidence_id", "doc_id", "block_type", "text", "search_text"})


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


def _search_text(row: EvidenceUnitRow) -> str:
    # Use chapter context for recall, but never present the synthetic prefix as a quotation.
    chapter = " · ".join(row.chapter_path[-2:])[-160:] if row.chapter_path else ""
    return f"{chapter}\n{row.text}" if chapter and row.block_type == "paragraph" else row.text


def _point(row: EvidenceUnitRow, dense: list[float]) -> PointStruct:
    return PointStruct(
        id=_point_id(row.evidence_id),
        vector={"dense": dense},
        payload={
            "evidence_id": row.evidence_id,
            "doc_id": row.doc_id,
            "block_type": row.block_type,
            "text": row.text,
            "search_text": _search_text(row),
        },
    )


def _index_rows(rows: list[EvidenceUnitRow]) -> int:
    client = get_client()
    ensure_collections(client)
    # Chunked parents are reading context only: their children carry the text
    # that should match a query, so indexing the parent would double-recall it.
    rows = [r for r in rows if not r.child_ids and not (r.locator or {}).get("context_only")
            and r.provenance in {"native_text", "ocr"} and r.text.strip()]
    if not rows:
        return 0
    dense = embed_texts([_search_text(r) for r in rows])
    points = [_point(row, vector.tolist()) for row, vector in zip(rows, dense, strict=True)]
    invalidate_bm25()
    client.upsert(collection_name=COLLECTION_TEXT, points=points, wait=True)
    return len(points)


def index_doc(doc_id: str) -> int:
    """Upsert one document’s dense vectors; rebuild BM25 once after all writes."""
    n = 0
    for rows in iter_units(doc_id=doc_id):
        n += _index_rows(rows)
    return n


def rebuild_bm25(client: QdrantClient | None = None, *,
                 progress: Callable[[str], None] | None = None) -> int:
    """Backfill only the named sparse vector on every actually indexed point.

    Use the text stored with the existing BGE-M3 vectors, rather than adding
    unindexed SQLite rows or silently mixing sparse/dense on different texts.
    At offline cutover an interrupted run has no readiness manifest.
    """
    client = client or get_client()
    if not client.collection_exists(COLLECTION_TEXT):
        raise ValueError("没有文本索引，请先导入语料。")
    expected = client.count(COLLECTION_TEXT, exact=True).count
    if not expected:
        raise ValueError("文本索引为空，请先导入语料。")
    sparse_config = client.get_collection(COLLECTION_TEXT).config.params.sparse_vectors or {}
    if set(sparse_config) != {VECTOR_NAME} or sparse_config[VECTOR_NAME].modifier != Modifier.IDF:
        raise ValueError("expected a PA text collection with only its BM25 sparse vector")

    count, token_count, vectors = 0, 0, 0
    digest = sha256()
    # Spool one lexical pass; never hold all term-count maps or dense vectors in memory.
    with TemporaryFile(mode="w+t", encoding="utf-8") as spool:
        offset = None
        while True:
            page, offset = client.scroll(COLLECTION_TEXT, offset=offset, limit=512,
                                         with_payload=["search_text"], with_vectors=False)
            for point in page:
                text = (point.payload or {}).get("search_text")
                if not isinstance(text, str):
                    raise ValueError(f"indexed point {point.id} is missing its text payload")
                counts = lexical_counts(text)
                count += 1
                vectors += bool(counts)
                token_count += sum(counts.values())
                digest.update(json.dumps([point.id, text], ensure_ascii=False).encode("utf-8"))
                spool.write(json.dumps([point.id, list(counts.items())]) + "\n")
            if progress and (offset is None or count % 4096 == 0):
                progress(f"BM25 tokenize {count}/{expected}")
            if offset is None:
                break
        if count != expected or client.count(COLLECTION_TEXT, exact=True).count != expected:
            raise RuntimeError("文本索引在扫描期间变化；请停止其它写入后重试。")
        if not vectors:
            raise ValueError("文本索引中没有可分词的内容。")
        avgdl = token_count / vectors

        invalidate_bm25()
        spool.seek(0)
        written = 0
        while batch := list(islice(spool, 256)):
            updates, empty = [], []
            for line in batch:
                point_id, counts = json.loads(line)
                if counts:
                    updates.append(PointVectors(id=point_id, vector={
                        VECTOR_NAME: document_vector(dict(counts), avgdl=avgdl),
                    }))
                else:
                    empty.append(point_id)
            if updates:
                done = client.update_vectors(COLLECTION_TEXT, points=updates, wait=True)
                if done.status != UpdateStatus.COMPLETED:
                    raise RuntimeError("Qdrant did not finish updating BM25 vectors")
            if empty:
                done = client.delete_vectors(COLLECTION_TEXT, vectors=[VECTOR_NAME], points=empty, wait=True)
                if done.status != UpdateStatus.COMPLETED:
                    raise RuntimeError("Qdrant did not finish deleting old BM25 vectors")
            written += len(batch)
            if progress and (written == count or written % 4096 == 0):
                progress(f"BM25 write {written}/{count}")
    if client.count(COLLECTION_TEXT, exact=True).count != count or bm25_vector_count(client) != vectors:
        raise RuntimeError("BM25 coverage check failed; index was not marked ready")
    publish_bm25(points=count, vectors=vectors, avgdl=avgdl, corpus_sha256=digest.hexdigest())
    return vectors
