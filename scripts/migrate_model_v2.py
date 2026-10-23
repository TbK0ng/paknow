"""One-shot schema migration for the v2 domain model (SQLite).

This repo bootstraps schema with ``create_all`` and has no Alembic directory,
so column changes need an explicit migration:

1. ``evidence_units.quality``   -> ``provenance`` (values become
   ``native_text | ocr | derived_observation``)
2. ``graph_nodes.concept_kind`` — new nullable column
3. ``claims.subject_concept_id`` — new nullable, indexed column

Idempotent: already-migrated columns are skipped. Run with ``--apply``; without
it the script only reports what it would do. A timestamped backup is written
before any change.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

from paknow.config import settings


def sqlite_path() -> Path:
    url = settings.paknow_db_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"this migration handles SQLite only, got: {url}")
    path = Path(url[len(prefix) :])
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def path_changes(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Re-point documents whose stored absolute path no longer exists.

    Falls back to ``settings.corpus_dir / basename`` and stores the path
    relative to the corpus dir when possible.
    """
    from paknow.ingest.manifest import source_path_for

    out: list[tuple[str, str, str]] = []
    for doc_id, stored in con.execute("SELECT doc_id, source_path FROM documents"):
        current = Path(stored)
        if current.exists():
            continue
        candidate = settings.corpus_dir / current.name
        if not candidate.exists():
            continue
        new = source_path_for(candidate)
        if new != stored:
            out.append((doc_id, stored, new))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db = sqlite_path()
    if not db.exists():
        raise SystemExit(f"database not found: {db}")

    con = sqlite3.connect(db)
    statements: list[str] = []

    ev_cols = columns(con, "evidence_units")
    if "quality" in ev_cols and "provenance" not in ev_cols:
        statements.append("ALTER TABLE evidence_units RENAME COLUMN quality TO provenance")
    node_cols = columns(con, "graph_nodes")
    if "concept_kind" not in node_cols:
        statements.append("ALTER TABLE graph_nodes ADD COLUMN concept_kind VARCHAR(16)")
    claim_cols = columns(con, "claims")
    if "subject_concept_id" not in claim_cols:
        statements.append("ALTER TABLE claims ADD COLUMN subject_concept_id VARCHAR(64)")
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_claims_subject_concept_id "
            "ON claims (subject_concept_id)"
        )

    if not statements:
        print("schema: already migrated")
    else:
        print(f"database: {db}")
        for stmt in statements:
            print(f"  {stmt}")

    changes = path_changes(con)
    if changes:
        print(f"source paths to normalise: {len(changes)}")
        for doc_id, old, new in changes[:10]:
            print(f"  {doc_id[:8]}  {old} -> {new}")

    if not statements and not changes:
        print("nothing to do")
        return
    if not args.apply:
        print("dry run; re-run with --apply")
        return

    backup = db.with_suffix(db.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, backup)
    print(f"backup:   {backup}")

    with con:
        for stmt in statements:
            con.execute(stmt)
        con.executemany("UPDATE documents SET source_path = ? WHERE doc_id = ?", [(new, doc_id) for doc_id, _old, new in changes])
    con.close()
    print("schema migrated")


if __name__ == "__main__":
    main()
