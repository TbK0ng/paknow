"""Startup self-check: the app refuses to serve a store it cannot answer from.

The three failure states are fabricated in a temp dir — a missing collection, an
empty collection, and a lock held by "another process" (held from this process,
same idiom as ``test_qdrant_store_lock.py``) — so no real store and no second
process is involved.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import portalocker
import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from sqlalchemy import create_engine

from apps.api import main
from paknow import db as database
from paknow.db import Base
from paknow.db.models import DocumentRow, EvidenceUnitRow
from paknow.retrieval import pa_corpus, qdrant_store
from paknow.retrieval.index import rebuild_bm25


class FakeStore:
    """Stand-in for the embedded client; records any collection told to be created."""

    def __init__(self, *, exists: bool, points: int = 0) -> None:
        self.exists = exists
        self.points = points
        self.created: list[str] = []

    def collection_exists(self, name: str) -> bool:
        return self.exists

    def create_collection(self, collection_name: str, **_: object) -> None:
        self.created.append(collection_name)

    def count(self, collection_name: str, **_: object) -> SimpleNamespace:
        return SimpleNamespace(count=self.points)


@pytest.fixture
def objects_dir(tmp_path, monkeypatch):
    """Point the store at a temp dir; never touch data/objects."""
    monkeypatch.setattr(qdrant_store.settings, "objects_dir", tmp_path)
    qdrant_store.get_client.cache_clear()
    yield tmp_path


def _install(monkeypatch, fake: FakeStore) -> FakeStore:
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)
    return fake


def test_a_missing_collection_is_reported_as_missing_and_not_created(
    objects_dir, monkeypatch
):
    fake = _install(monkeypatch, FakeStore(exists=False))

    with pytest.raises(qdrant_store.StoreNotReadyError) as excinfo:
        qdrant_store.check_store_ready()

    assert excinfo.value.reason == "missing"
    assert qdrant_store.COLLECTION_TEXT in str(excinfo.value)
    assert "入库" in str(excinfo.value)
    # creating it would turn a store that was never built into a healthy-looking one
    assert fake.created == []


def test_an_empty_collection_is_reported_as_empty(objects_dir, monkeypatch):
    fake = _install(monkeypatch, FakeStore(exists=True, points=0))

    with pytest.raises(qdrant_store.StoreNotReadyError) as excinfo:
        qdrant_store.check_store_ready()

    assert excinfo.value.reason == "empty"
    assert "为空" in str(excinfo.value)
    assert fake.created == []


def test_a_store_held_by_another_process_is_reported_as_locked(objects_dir):
    path = qdrant_store.store_lock_path()
    qdrant_store._holder_path(path).write_text(
        json.dumps(
            {
                "pid": 4242,
                "acquired_at": "2026-10-26T00:00:00+00:00",
                "argv": ["eval_text_arm.py"],
            }
        ),
        encoding="utf-8",
    )
    handle = open(path, "a+b")
    handle.seek(0)
    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    try:
        with pytest.raises(qdrant_store.StoreNotReadyError) as excinfo:
            qdrant_store.check_store_ready()
    finally:
        portalocker.unlock(handle)
        handle.close()

    assert excinfo.value.reason == "locked"
    assert "pid=4242" in str(excinfo.value)
    assert "eval_text_arm.py" in str(excinfo.value)


def test_the_three_reasons_do_not_share_one_generic_message(
    objects_dir, monkeypatch
):
    _install(monkeypatch, FakeStore(exists=False))
    with pytest.raises(qdrant_store.StoreNotReadyError) as missing:
        qdrant_store.check_store_ready()
    _install(monkeypatch, FakeStore(exists=True, points=0))
    with pytest.raises(qdrant_store.StoreNotReadyError) as empty:
        qdrant_store.check_store_ready()

    assert str(missing.value) != str(empty.value)


@pytest.fixture
def indexed_store(objects_dir):
    client = QdrantClient(":memory:")
    qdrant_store.ensure_collections(client)
    client.upsert(qdrant_store.COLLECTION_TEXT, [PointStruct(id=1,
        vector={"dense": [1.0] * qdrant_store.DENSE_DIM}, payload={"search_text": "trend pullback"})])
    try:
        yield client
    finally:
        client.close()


def test_nonempty_dense_store_is_not_ready_until_bm25_finishes(indexed_store):
    with pytest.raises(qdrant_store.StoreNotReadyError, match="重新导入并封存") as excinfo:
        qdrant_store.check_store_ready(indexed_store)
    assert excinfo.value.reason == "bm25"
    rebuild_bm25(indexed_store)
    assert qdrant_store.check_store_ready(indexed_store) == 1


def test_qdrants_own_folder_conflict_becomes_a_lock_error(objects_dir, monkeypatch):
    def factory(**_: object) -> object:
        raise RuntimeError(
            "Storage folder E:/x is already accessed by another instance of "
            "Qdrant client. If you require concurrent access, use Qdrant server instead."
        )

    monkeypatch.setattr(qdrant_store, "QdrantClient", factory)

    with pytest.raises(qdrant_store.StoreLockedError):
        qdrant_store.get_client()

    # a failed construction must not leave our own lock held
    assert qdrant_store._lock_handle is None


def _start_app() -> None:
    """Run the startup hook the way uvicorn would, without a server."""

    async def run() -> None:
        async with main.app.router.lifespan_context(main.app):
            pass

    asyncio.run(run())


def test_the_app_refuses_to_start_when_the_store_is_not_ready(
    objects_dir, monkeypatch
):
    _install(monkeypatch, FakeStore(exists=True, points=0))

    with pytest.raises(qdrant_store.StoreNotReadyError):
        _start_app()


def test_sealed_store_schema_excludes_retired_features(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    try:
        Base.metadata.create_all(engine)
        pa_corpus.check_schema(engine)
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE figure_assets (image_id TEXT PRIMARY KEY)")
        with pytest.raises(RuntimeError, match="纯文本数据库"):
            pa_corpus.check_schema(engine)
    finally:
        engine.dispose()


def test_the_app_rejects_unsealed_store(indexed_store, monkeypatch):
    rebuild_bm25(indexed_store)
    _install(monkeypatch, indexed_store)
    with pytest.raises(RuntimeError, match="未封存"):
        _start_app()


def test_the_app_starts_only_with_the_sealed_evidence_and_graph_sources(objects_dir, indexed_store, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    rebuild_bm25(indexed_store)
    _install(monkeypatch, indexed_store)
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "SessionLocal", sessions)
        monkeypatch.setattr(pa_corpus, "expected_originals", lambda: {"book.pdf": "digest"})
        graph_sources = objects_dir / "graph.yaml"
        graph_sources.write_text("[]\n", encoding="utf-8")
        monkeypatch.setattr(pa_corpus, "PA_GRAPH_SOURCES", graph_sources)
        with sessions.begin() as db:
            db.add(DocumentRow(doc_id="doc", content_hash="digest", format="pdf", source_path="book.pdf",
                               parser_version=pa_corpus.BUILD_VERSION))
            db.add(EvidenceUnitRow(evidence_id="passage", doc_id="doc", text="original", text_hash="text-digest",
                                   locator={"pdf_page_1based": 1}))
            db.flush()
            seal = {"chunker": pa_corpus.CHUNK_VERSION, "build_version": pa_corpus.BUILD_VERSION,
                    "graph_sources": pa_corpus.graph_sources_digest(), "bm25": qdrant_store.bm25_generation(),
                    "documents": {"doc": "digest"}, "graph": pa_corpus.graph_digest(db),
                    "evidence_sha256": pa_corpus.evidence_digest(db), "dense_points": 1, "sparse_points": 1}
        (objects_dir / pa_corpus.MANIFEST).write_text(json.dumps(seal), encoding="utf-8")
        _start_app()
        assert main.health() == {"status": "ok"}
        graph_sources.write_text("# changed\n[]\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="图谱来源不匹配"):
            _start_app()
        graph_sources.write_text("[]\n", encoding="utf-8")
        with sessions.begin() as db:
            db.get(EvidenceUnitRow, "passage").locator = {"pdf_page_1based": 2}
        with pytest.raises(RuntimeError, match="seal does not match"):
            _start_app()
    finally:
        engine.dispose()
