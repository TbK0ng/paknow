"""The embedded vector store belongs to one process: a second holder fails fast.

These tests inject the lock path and hold the lock from the test process itself,
so they exercise contention without starting a second process or touching
``data/objects``.
"""
from __future__ import annotations

import json
import os

import portalocker
import pytest

from paknow.retrieval import qdrant_store


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "qdrant.lock"
    monkeypatch.setattr(qdrant_store, "store_lock_path", lambda: path)
    qdrant_store.get_client.cache_clear()
    yield path
    qdrant_store.get_client.cache_clear()


def _hold(path, pid: int = 4242):
    """Play the other process: leave its identity beside the lock, hold the lock."""
    qdrant_store._holder_path(path).write_text(
        json.dumps(
            {
                "pid": pid,
                "acquired_at": "2026-10-26T00:00:00+00:00",
                "argv": ["eval_text_arm.py"],
            }
        ),
        encoding="utf-8",
    )
    handle = open(path, "a+b")
    handle.seek(0)
    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    return handle


def _release(handle) -> None:
    portalocker.unlock(handle)
    handle.close()


def _fake_client(monkeypatch):
    built: list[dict] = []

    def factory(**kwargs):
        built.append(kwargs)
        return object()

    monkeypatch.setattr(qdrant_store, "QdrantClient", factory)
    return built


def test_second_holder_fails_immediately_with_the_holder_identity(lock_path, monkeypatch):
    _fake_client(monkeypatch)
    handle = _hold(lock_path)
    try:
        with pytest.raises(qdrant_store.StoreLockedError) as excinfo:
            qdrant_store.get_client()
    finally:
        _release(handle)

    message = str(excinfo.value)
    assert isinstance(excinfo.value, RuntimeError)
    assert "pid=4242" in message
    assert "eval_text_arm.py" in message
    assert "稍后重试" in message
    assert str(lock_path) in message


def test_repeated_lookup_returns_the_same_client(lock_path, monkeypatch):
    built = _fake_client(monkeypatch)

    first = qdrant_store.get_client()

    assert qdrant_store.get_client() is first
    assert len(built) == 1


def test_the_holder_identity_is_recorded_beside_the_lock(lock_path, monkeypatch):
    _fake_client(monkeypatch)

    qdrant_store.get_client()

    info = json.loads(qdrant_store._holder_path(lock_path).read_text(encoding="utf-8"))
    assert info["pid"] == os.getpid()
    assert info["acquired_at"]
    assert info["argv"]


def test_a_server_url_bypasses_the_lock(lock_path, monkeypatch):
    built = _fake_client(monkeypatch)
    monkeypatch.setattr(qdrant_store.settings, "qdrant_url", "http://localhost:6333")
    handle = _hold(lock_path)  # embedded contention must not matter for a server
    try:
        qdrant_store.get_client()
    finally:
        _release(handle)

    assert built == [{"url": "http://localhost:6333"}]


def test_the_store_is_reachable_once_the_holder_exits(lock_path, monkeypatch):
    built = _fake_client(monkeypatch)
    handle = _hold(lock_path)
    with pytest.raises(qdrant_store.StoreLockedError):
        qdrant_store.get_client()
    _release(handle)

    qdrant_store.get_client()

    assert len(built) == 1
