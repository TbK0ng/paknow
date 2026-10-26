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

from apps.api import main
from paknow.retrieval import qdrant_store


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


def test_a_ready_store_returns_the_count(objects_dir, monkeypatch):
    _install(monkeypatch, FakeStore(exists=True, points=42))

    assert qdrant_store.check_store_ready() == 42


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


def test_the_app_starts_and_serves_health_against_a_ready_store(
    objects_dir, monkeypatch
):
    _install(monkeypatch, FakeStore(exists=True, points=3))

    _start_app()

    assert main.health() == {"status": "ok"}
