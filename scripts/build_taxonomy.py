"""Rebuild concept taxonomy edges from encyclopedia heading grammar.

Dry run by default: prints the dedupe/parse/coverage numbers and the edge set
fingerprint without touching the database. ``--apply`` replaces every edge with
``origin='heading_parse'``; seeded, claim and figure edges are never touched.

Usage:
    python scripts/build_taxonomy.py
    python scripts/build_taxonomy.py --apply
    python scripts/build_taxonomy.py --doc-id <doc_id> --apply
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from datetime import datetime
from typing import Iterable

from sqlalchemy import func

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, GraphEdgeRow
from paknow.graph.taxonomy import (
    ORIGIN_HEADING,
    TaxonomyEdge,
    build_taxonomy_edges,
    load_heading_units,
    rebuild_taxonomy,
)

_ENCYCLOPEDIA_MARKER = "encyclopedia"


def encyclopedia_doc_ids() -> list[str]:
    """Documents whose source path names the encyclopedia (the parse target)."""
    with SessionLocal() as s:
        rows = (
            s.query(DocumentRow)
            .filter(func.lower(DocumentRow.source_path).like(f"%{_ENCYCLOPEDIA_MARKER}%"))
            .all()
        )
        return sorted(r.doc_id for r in rows)


def origin_counts() -> dict[str, int]:
    with SessionLocal() as s:
        rows = s.query(GraphEdgeRow.origin, func.count()).group_by(GraphEdgeRow.origin).all()
    return {origin: count for origin, count in rows}


def relation_counts(edges: Iterable[TaxonomyEdge]) -> str:
    counts = Counter(e.relation.value for e in edges)
    return "  ".join(f"{rel}={counts[rel]}" for rel in sorted(counts))


def fingerprint(edges: Iterable[TaxonomyEdge]) -> str:
    payload = "\n".join(
        sorted(f"{e.from_concept}|{e.relation.value}|{e.to_concept}|{e.source}" for e in edges)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def print_stats(build, *, scope: str) -> None:
    stats = build.stats
    dropped = stats["dropped_header"] + stats["dropped_duplicate"]
    print(f"database:   {settings.paknow_db_url}")
    print(f"scope:      {scope}")
    print(
        f"headings:   {stats['headings_in']} -> {stats['headings_after_dedupe']} "
        f"(dropped {dropped}: header {stats['dropped_header']}, duplicate {stats['dropped_duplicate']})"
    )
    print(
        f"parsed:     {stats['parsed']}  dropped_unparsed: {stats['dropped_unparsed']}  "
        f"coverage: {stats['coverage']:.2%}"
    )
    print(f"relations:  {relation_counts(build.edges)}")
    print(f"edges:      {stats['edges']}")
    print(f"fingerprint:{fingerprint(build.edges)}")


def print_edges(build) -> None:
    for e in build.edges:
        print(f"  {e.relation.value:<15} {e.from_concept} -> {e.to_concept}  [{e.source}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write edges (default: dry run)")
    ap.add_argument("--doc-id", action="append", default=None, help="scope to this document id")
    ap.add_argument(
        "--header-threshold",
        type=int,
        default=5,
        help="drop a normalized title repeated at least this often in one document (default: 5)",
    )
    args = ap.parse_args()

    if args.doc_id:
        doc_ids = sorted(set(args.doc_id))
        scope = f"doc_id={', '.join(doc_ids)}"
    else:
        doc_ids = encyclopedia_doc_ids()
        if not doc_ids:
            raise SystemExit("no encyclopedia document found; pass --doc-id <doc_id>")
        scope = f"{_ENCYCLOPEDIA_MARKER} (doc_id={', '.join(doc_ids)})"

    units = load_heading_units(doc_ids=doc_ids)
    build = build_taxonomy_edges(units, header_threshold=args.header_threshold)
    print_stats(build, scope=scope)

    before = origin_counts()
    if not args.apply:
        print(
            f"heading_parse edges currently in DB: {before.get(ORIGIN_HEADING, 0)} (would be replaced)"
        )
        print("dry run; re-run with --apply")
        return

    print("origins before: " + " ".join(f"{k}={v}" for k, v in sorted(before.items())))
    rebuild_taxonomy(doc_ids=doc_ids, header_threshold=args.header_threshold)
    after = origin_counts()

    print(f"inserted heading edges: {after.get(ORIGIN_HEADING, 0)}")
    print_edges(build)
    print("origins after:  " + " ".join(f"{k}={v}" for k, v in sorted(after.items())))
    for origin, count in sorted(before.items()):
        if origin == ORIGIN_HEADING:
            continue
        if after.get(origin, 0) != count:
            raise SystemExit(f"ERROR: {origin} edges changed {count} -> {after.get(origin, 0)}")
    print(f"preserved:      {', '.join(f'{k}={v}' for k, v in sorted(before.items()) if k != ORIGIN_HEADING) or 'none'}")
    print(f"applied at:     {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
