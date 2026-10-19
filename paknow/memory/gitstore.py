"""Git-backed memory store (plan §9.3 版本化 SOP).

``sop_registry/`` lives inside the project repo (E:/PAknow). Each SOP is a
YAML file; every change is committed from the registry directory, so commits
are scoped to ``sop_registry/`` and the full version history is ``git log``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

REGISTRY_DIR = Path(__file__).resolve().parent.parent.parent / "sop_registry"


def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=REGISTRY_DIR, capture_output=True, text=True, check=True
        )
        return r.stdout.strip()
    except subprocess.CalledProcessError as e:
        return (e.stdout or "") + (e.stderr or "")


def _in_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree") == "true"


def ensure_repo() -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)


def write_sop(sop: dict[str, Any]) -> Path:
    """Dump a SOP dict to YAML (idempotent)."""
    ensure_repo()
    p = REGISTRY_DIR / f"{sop['sop_id']}.yaml"
    p.write_text(
        yaml.safe_dump(sop, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return p


def commit(message: str) -> str:
    """Commit pending SOP changes (scoped to sop_registry/ via cwd).

    Returns the new HEAD one-liner; empty string when no repo is initialized.
    """
    if not _in_repo():
        return ""
    _git("add", "-A", ".")
    _git("commit", "-q", "-m", message)
    return _git("log", "--oneline", "-1")


def log(sop_id: str | None = None, n: int = 15) -> list[str]:
    args = ["log", "--oneline", f"-{n}"]
    if sop_id:
        args += ["--", f"{sop_id}.yaml"]
    out = _git(*args)
    return out.splitlines() if out else []


def current_commit() -> str:
    return _git("rev-parse", "--short", "HEAD")
