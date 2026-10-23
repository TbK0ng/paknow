"""Add ``graph_edges.origin`` so rebuilds can tell edge sources apart.

Origins come from :class:`~paknow.graph.contracts.EdgeOrigin`:

- ``seed``            manually seeded edges (``source='seed'``)
- ``claim_extract``   claim -> evidence ``SUPPORTED_BY`` edges
- ``figure_extract``  figure -> evidence ``ILLUSTRATED_BY`` edges
- ``heading_parse``   concept taxonomy edges (written by ``build_taxonomy``)

Idempotent. Dry run by default; ``--apply`` writes a timestamped backup first.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

from paknow.config import settings
from paknow.graph.contracts import EdgeOrigin

_SEED = EdgeOrigin.seed.value
_CLAIM = EdgeOrigin.claim_extract.value
_FIGURE = EdgeOrigin.figure_extract.value


def sqlite_path() -> Path:
    url = settings.paknow_db_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"this migration handles SQLite only, got: {url}")
    path = Path(url[len(prefix) :])
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def statements_for_missing_origin() -> list[tuple[str, tuple[str, ...]]]:
    """Statements plus their parameters; SQL text carries no origin literals."""
    return [
        (
            f"ALTER TABLE graph_edges ADD COLUMN origin VARCHAR(16) NOT NULL DEFAULT '{_CLAIM}'",
            (),
        ),
        ("CREATE INDEX IF NOT EXISTS ix_graph_edges_origin ON graph_edges (origin)", ()),
        (
            "UPDATE graph_edges SET origin = CASE "
            "WHEN source = ? THEN ? "
            "WHEN relation = 'ILLUSTRATED_BY' THEN ? "
            "ELSE ? END",
            (_SEED, _SEED, _FIGURE, _CLAIM),
        ),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db = sqlite_path()
    if not db.exists():
        raise SystemExit(f"database not found: {db}")

    con = sqlite3.connect(db)
    statements = statements_for_missing_origin() if "origin" not in columns(con, "graph_edges") else []

    if not statements:
        print("schema: already migrated")
        return
    print(f"database: {db}")
    for stmt, params in statements:
        print(f"  {stmt}" + (f"  -- {params}" if params else ""))
    if not args.apply:
        print("dry run; re-run with --apply")
        return

    backup = db.with_suffix(db.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, backup)
    print(f"backup:   {backup}")
    with con:
        for stmt, params in statements:
            con.execute(stmt, params)
    con.close()
    print("schema migrated")


if __name__ == "__main__":
    main()
