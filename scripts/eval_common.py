"""Shared plumbing for the offline evaluation scripts.

The gold evaluation, the answer runner and the judge all need the same three
things: a SHA-256 of a file's bytes (so sidecars match independent tools), a
repo-relative path for artifacts, and an append-only JSONL checkpoint whose
last line per (item, arm) wins. They live here once instead of three times.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent


def display_path(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (e.g. temp datasets)."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def file_sha256(path: Path) -> str:
    """SHA-256 of the file bytes, so sidecars match independent tools."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record["id"]), str(record["arm"]))


def load_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    """Latest record per (item, arm); the last line wins."""
    latest: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # a killed process may leave a partial trailing line
            continue
        if "id" not in record or "arm" not in record:
            continue
        latest[record_key(record)] = record
    return latest


def append_checkpoint(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
