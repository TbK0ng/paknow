"""Read the sealed PA corpus generation; never scan prior stores."""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from paknow.config import settings
from paknow.ingest.chunk import BUILD_VERSION, VERSION as CHUNK_VERSION
from paknow.ingest.manifest import expected_originals
from paknow.retrieval.qdrant_store import bm25_generation

MANIFEST = "pa-corpus.json"
PA_GRAPH_SOURCES = Path(__file__).resolve().parents[2] / "configs" / "pa_graph_sources.yaml"


def graph_sources_digest() -> str:
    return sha256(PA_GRAPH_SOURCES.read_bytes()).hexdigest()


@lru_cache(maxsize=4)
def _read(path: Path, mtime: int, size: int) -> tuple[str, dict]:
    raw = path.read_bytes()
    return sha256(raw).hexdigest(), json.loads(raw)


def corpus() -> tuple[str, frozenset[str] | None]:
    path = settings.objects_dir / MANIFEST
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "unsealed", None
    generation, seal = _read(path, stat.st_mtime_ns, stat.st_size)
    return generation, frozenset(seal["documents"])


def graph_digest(db) -> str:
    from sqlalchemy import select
    from paknow.db.models import ClaimRow, GraphEdgeRow
    from paknow.graph.pa_context import SCOPE

    claims = [(r.claim_id, r.subject_concept_id, r.extraction_status, r.qualifiers, r.supporting_evidence, r.predicate)
              for r in db.scalars(select(ClaimRow).where(ClaimRow.source_scope == SCOPE).order_by(ClaimRow.claim_id))]
    edges = [(r.edge_id, r.from_node, r.to_node, r.relation, r.source, r.extraction_status)
             for r in db.scalars(select(GraphEdgeRow).where(GraphEdgeRow.origin == SCOPE).order_by(GraphEdgeRow.edge_id))]
    return sha256(json.dumps([claims, edges], sort_keys=True).encode()).hexdigest()


def evidence_digest(db) -> str:
    """Bind original text, source locators and relationships used at runtime."""
    from sqlalchemy import select
    from paknow.db.models import DocumentRow, EvidenceUnitRow

    digest = sha256()
    for model, key, ignored in ((DocumentRow, DocumentRow.doc_id, "registered_at"),
                                (EvidenceUnitRow, EvidenceUnitRow.evidence_id, "parsed_at")):
        digest.update(model.__tablename__.encode() + b"\n")
        columns = [col for col in model.__table__.columns if col.name != ignored]
        records = db.execute(select(*columns).order_by(key).execution_options(yield_per=512))
        for row in records:
            digest.update(json.dumps(tuple(row), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def check_schema(engine) -> None:
    """Reject schemas from retired applications instead of serving or migrating them."""
    from sqlalchemy import inspect
    from paknow.db import Base
    from paknow.db import models  # noqa: F401

    inspector = inspect(engine)
    tables = Base.metadata.tables
    if set(inspector.get_table_names()) != set(tables) or any(
            {col["name"] for col in inspector.get_columns(name)} != set(table.columns.keys())
            for name, table in tables.items()):
        raise RuntimeError("需要当前的 PA 纯文本数据库；请在隔离目录导入并封存六份原件。")


def check_corpus() -> None:
    """Reject unsealed or mismatched corpus pairs on startup."""
    path = settings.objects_dir / MANIFEST
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError("PA 语料未封存；请运行 python -m scripts.seal_pa_corpus。") from exc
    _, seal = _read(path, stat.st_mtime_ns, stat.st_size)
    if (seal.get("chunker") != CHUNK_VERSION or seal.get("build_version") != BUILD_VERSION
            or seal.get("graph_sources") != graph_sources_digest()):
        raise RuntimeError("PA 语料与当前构建版本或图谱来源不匹配；请从原件重建并封存。")
    from sqlalchemy import select
    from paknow.db import SessionLocal, engine
    from paknow.db.models import ClaimRow, DocumentRow, GraphEdgeRow
    from paknow.graph.pa_context import SCOPE

    check_schema(engine)
    with SessionLocal() as db:
        if (db.query(ClaimRow).filter(ClaimRow.source_scope != SCOPE).count()
                or db.query(GraphEdgeRow).filter(GraphEdgeRow.origin != SCOPE).count()):
            raise RuntimeError("数据库包含不属于现役 PA 取证的图谱条目。")
        documents = dict(db.execute(select(DocumentRow.doc_id, DocumentRow.content_hash)).all())
        graph = graph_digest(db)
        evidence = evidence_digest(db)
        if db.query(DocumentRow).filter(DocumentRow.parser_version.is_distinct_from(BUILD_VERSION)).count():
            raise RuntimeError("存在并非由当前段落构建器产出的文档。")
    if (sorted(documents.values()) != sorted(expected_originals().values())
            or seal["bm25"] != bm25_generation() or seal["documents"] != documents or seal["graph"] != graph
            or seal.get("evidence_sha256") != evidence):
        raise RuntimeError("PA corpus seal does not match the active SQLite, graph, and Qdrant set")
