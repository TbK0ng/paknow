"""Qdrant client: embedded local mode (default) or remote server.

Text collection carries a dense vector (BGE-M3) and a named sparse vector
(lexical). Visual collection carries ColPali page multi-vectors (added later).

The embedded store belongs to one process at a time. ``get_client`` takes an
exclusive, non-blocking lock beside the store directory and fails fast with
:class:`StoreLockedError` naming the holder, so a concurrent evaluation or
script can never share the store and quietly return nothing. ``QDRANT_URL``
bypasses the lock: a server is built to be concurrent.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import IO

import portalocker
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    SparseVectorParams,
    VectorParams,
)

from paknow.config import settings

COLLECTION_TEXT = "evidence_text"
COLLECTION_VISUAL = "page_visual"
DENSE_DIM = 1024  # BGE-M3


class StoreLockedError(RuntimeError):
    """Another process owns the embedded store.

    The holder is identified by pid, acquisition time and command so the
    conflict is actionable (stop that process) and cheap to report from the API.
    """

    def __init__(self, path: Path, holder: str) -> None:
        self.path = path
        self.holder = holder
        super().__init__(
            f"内嵌向量库已被占用（{holder}）；评测或脚本正在占用存储，请稍后重试。"
            f"锁文件：{path}"
        )


# Held for the life of the process; closing it releases the lock.
_lock_handle: IO[bytes] | None = None


def store_lock_path() -> Path:
    """Lock file beside the store directory (tests inject a temp path)."""
    return settings.objects_dir / "qdrant.lock"


def _holder_path(lock_path: Path) -> Path:
    # Windows byte-range locks make the locked file unreadable to other
    # handles, so the holder identity lives beside it, not inside it.
    return lock_path.with_name(lock_path.name + ".holder")


def _holder_label(path: Path) -> str:
    """Read the holder identity the lock owner wrote, tolerating a stale file."""
    try:
        info = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return "持有进程未知"
    if info.get("pid") is None:
        return "持有进程未知"
    parts = [f"pid={info['pid']}", f"since={info.get('acquired_at')}"]
    command = " ".join(info.get("argv") or [])
    if command:
        parts.append(f"cmd={command}")
    return ", ".join(parts)


def _acquire_store_lock(path: Path) -> None:
    """Take the exclusive lock or fail immediately; record who holds it."""
    global _lock_handle
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    handle.seek(0)  # lock byte 0 regardless of the file's length
    try:
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.exceptions.LockException:
        holder = _holder_label(_holder_path(path))
        handle.close()
        raise StoreLockedError(path, holder) from None
    _holder_path(path).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "argv": [Path(sys.argv[0]).name, *sys.argv[1:3]],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _lock_handle = handle


def _release_store_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        portalocker.unlock(_lock_handle)
        _lock_handle.close()
        _lock_handle = None


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    """Singleton client. Embedded mode is exclusive to this process."""
    if settings.qdrant_url:
        return QdrantClient(url=settings.qdrant_url)
    _acquire_store_lock(store_lock_path())
    try:
        return QdrantClient(path=str(settings.objects_dir / "qdrant"))
    except BaseException:
        _release_store_lock()
        raise


def ensure_collections(client: QdrantClient | None = None) -> None:
    client = client or get_client()
    if not client.collection_exists(COLLECTION_TEXT):
        client.create_collection(
            collection_name=COLLECTION_TEXT,
            vectors_config={"dense": VectorParams(size=DENSE_DIM, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )
    if not client.collection_exists(COLLECTION_VISUAL):
        # multi-vector pages: one vector per patch; dims set at ColPali load
        client.create_collection(
            collection_name=COLLECTION_VISUAL,
            vectors_config={},
        )
