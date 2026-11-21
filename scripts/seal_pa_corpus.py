"""Seal a fresh six-source PA store after exact SQLite/Qdrant membership checks."""
from __future__ import annotations

from hashlib import sha256
import json
from uuid import UUID

from sqlalchemy import select

from paknow.config import settings
from paknow.db import SessionLocal, engine
from paknow.db.models import DocumentRow, EvidenceUnitRow, GraphEdgeRow
from paknow.graph.pa_context import SCOPE
from paknow.ids import file_hash
from paknow.ingest.chunk import BUILD_VERSION, VERSION
from paknow.ingest.manifest import expected_originals, resolve_source
from paknow.retrieval.index import TEXT_PAYLOAD_KEYS, _search_text
from paknow.retrieval.pa_corpus import MANIFEST, check_schema, evidence_digest, graph_digest, graph_sources_digest
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, bm25_generation, bm25_vector_count, check_store_ready, get_client
from scripts.rebuild_text_graph import seed_passages


def seal() -> dict:
    expected = expected_originals()
    target = settings.objects_dir / MANIFEST
    if settings.qdrant_url or target.exists():
        raise ValueError("seal only a fresh, isolated embedded store")
    check_schema(engine)
    client = get_client()
    try:
        total = check_store_ready(client)
        if {c.name for c in client.get_collections().collections} != {COLLECTION_TEXT}:
            raise ValueError("unexpected collection, including visual collections")
        with SessionLocal() as db:
            docs = list(db.scalars(select(DocumentRow)))
            names = {}
            for doc in docs:
                if doc.parser_version != BUILD_VERSION:
                    raise ValueError(f"document needs the current parser/chunker: {doc.doc_id}")
                path = resolve_source(doc.source_path)
                digest = file_hash(str(path))
                if digest != doc.content_hash or expected.get(path.name) != digest or path.name in names:
                    raise ValueError(f"unexpected original or source hash: {path.name}")
                names[path.name] = digest
            if names != expected:
                raise ValueError("expected exactly the six originals")
            counts = {"parents": 0, "joined": 0, "source_blocks": 0, "evidence": 0}
            points = {}
            for row in db.scalars(select(EvidenceUnitRow)):
                counts["evidence"] += 1
                loc = row.locator or {}
                counts["joined"] += len(loc.get("source_ids") or []) > 1 and row.parent_id is None
                counts["parents"] += bool(row.child_ids)
                counts["source_blocks"] += bool(loc.get("context_only"))
                if row.provenance not in {"native_text", "ocr"}:
                    raise ValueError("unexpected derived text")
                if sha256(row.text.encode()).hexdigest() != row.text_hash:
                    raise ValueError(f"invalid text hash: {row.evidence_id}")
                if row.block_type != "footer" and not row.child_ids and not loc.get("context_only") and row.text.strip():
                    points[row.evidence_id] = (row.doc_id, row.block_type, row.text, _search_text(row))
        if total != len(points):
            raise ValueError("index count differs from eligible evidence count")
        cursor = None
        while True:
            page, cursor = client.scroll(COLLECTION_TEXT, offset=cursor, limit=512, with_vectors=False)
            for point in page:
                data = point.payload
                if set(data) != TEXT_PAYLOAD_KEYS:
                    raise ValueError(f"unexpected text index payload: {point.id}")
                if UUID(str(point.id)).hex != data["evidence_id"] or points.pop(data["evidence_id"], None) != (
                        data["doc_id"], data["block_type"], data["text"], data["search_text"]):
                    raise ValueError(f"index/SQLite mismatch: {point.id}")
            if cursor is None:
                break
        if points:
            raise ValueError("index is missing eligible evidence")
        seeded = seed_passages()
        with SessionLocal() as db:
            if db.query(GraphEdgeRow).filter((GraphEdgeRow.origin != SCOPE) |
                    ~GraphEdgeRow.source.in_(select(EvidenceUnitRow.evidence_id))).count():
                raise ValueError("unexpected or unresolved PA graph edge")
            graph = graph_digest(db)
            evidence = evidence_digest(db)
        report = {"chunker": VERSION, "build_version": BUILD_VERSION, "documents": {d.doc_id: d.content_hash for d in docs},
                  "bm25": bm25_generation(), "graph": graph, "graph_sources": graph_sources_digest(),
                  "evidence_sha256": evidence,
                  "reviewed_leads": seeded, "dense_points": total, "sparse_points": bm25_vector_count(client), **counts}
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(target)
        return report
    finally:
        client.close()


if __name__ == "__main__":
    print(json.dumps(seal(), indent=2))
