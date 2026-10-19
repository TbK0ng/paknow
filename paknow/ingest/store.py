"""Content-addressed object store for derived artifacts (images, renders, reports)."""
from __future__ import annotations

from pathlib import Path

from paknow.config import settings


def object_path(rel_path: str) -> Path:
    return settings.objects_dir / rel_path


def write_object(rel_path: str, data: bytes) -> str:
    """Write bytes once (idempotent). Returns absolute path."""
    settings.ensure_dirs()
    p = object_path(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(data)
    return str(p)
