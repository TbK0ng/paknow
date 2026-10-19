"""Content addressing and stable identifier helpers."""
from __future__ import annotations

import hashlib


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def file_hash(path: str) -> str:
    """Streaming SHA-256 of a file without loading it into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_id(*parts: str, length: int = 32) -> str:
    """Deterministic short id from ordered string parts."""
    h = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return h[:length]
