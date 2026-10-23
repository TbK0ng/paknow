"""Rebuild concept identity from ``configs/terms.yaml``.

Before: every extracted surface form became its own concept node
(2694 nodes, one per phrasing). After: a concept node exists only for a
dictionary canonical id; surface forms resolve to it, and unresolved forms go
to the ``concept_aliases`` review queue instead of growing the graph.

Edges are remapped onto canonical nodes and deduplicated; edges that belonged
only to unresolved nodes are dropped. Claims keep their extracted ``subject``
text and gain ``subject_concept_id`` when the subject resolves.

Dry run by default; ``--apply`` rewrites the database (a backup is written
first). Run ``scripts/migrate_model_v2.py --apply`` beforehand so the new
columns exist.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path

from paknow.config import settings
from paknow.graph.identity import (
    candidate_key,
    canonical_for,
    concept_kind_for,
    concept_node_id,
    label_for,
)


def sqlite_path() -> Path:
    url = settings.paknow_db_url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise SystemExit(f"this rebuild handles SQLite only, got: {url}")
    path = Path(url[len(prefix) :])
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def plan(con: sqlite3.Connection) -> dict[str, object]:
    concepts = con.execute(
        "SELECT node_id, label, concept_id FROM graph_nodes WHERE node_type = 'concept'"
    ).fetchall()
    mapping: dict[str, str] = {}
    canonical_meta: dict[str, tuple[str, str | None]] = {}
    unresolved: list[tuple[str, str]] = []
    for node_id, label, concept_id in concepts:
        canonical = canonical_for(label) or canonical_for(concept_id)
        if canonical is None:
            unresolved.append((node_id, label or concept_id or node_id))
            continue
        target = concept_node_id(canonical)
        mapping[node_id] = target
        canonical_meta.setdefault(canonical, (label_for(canonical), concept_kind_for(canonical)))

    edges_touched = 0
    edges_dropped = 0
    unresolved_ids = {nid for nid, _ in unresolved}
    unresolved_with_edges: dict[str, str] = {}
    surfaces = {nid: surface for nid, surface in unresolved}
    for from_node, to_node in con.execute("SELECT from_node, to_node FROM graph_edges"):
        if from_node in mapping or to_node in mapping:
            edges_touched += 1
        for endpoint in (from_node, to_node):
            if endpoint in unresolved_ids:
                edges_dropped += 1
                unresolved_with_edges[endpoint] = surfaces[endpoint]

    claims = con.execute("SELECT claim_id, subject FROM claims").fetchall()
    claim_hits = [(cid, canonical_for(subject)) for cid, subject in claims]
    claim_hits = [(cid, c) for cid, c in claim_hits if c]

    return {
        "concepts": len(concepts),
        "mapping": mapping,
        "canonical_meta": canonical_meta,
        "unresolved": unresolved,
        "edges_touched": edges_touched,
        "edges_dropped": edges_dropped,
        "unresolved_with_edges": unresolved_with_edges,
        "claims_tagged": len(claim_hits),
        "claim_hits": claim_hits,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument(
        "--allow-drop",
        action="store_true",
        help="delete edges attached to unresolved concepts instead of aborting",
    )
    args = ap.parse_args()

    db = sqlite_path()
    con = sqlite3.connect(db)
    for table, column in (("graph_nodes", "concept_kind"), ("claims", "subject_concept_id")):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            raise SystemExit(f"{table}.{column} missing; run scripts/migrate_model_v2.py --apply")

    p = plan(con)
    mapping: dict[str, str] = p["mapping"]  # type: ignore[assignment]
    canonical_meta: dict[str, tuple[str, str | None]] = p["canonical_meta"]  # type: ignore[assignment]

    print(f"database:            {db}")
    print(f"concept nodes:       {p['concepts']}")
    print(f"  resolved surfaces: {len(mapping)}")
    print(f"  unresolved (queue):{len(p['unresolved'])}")
    print(f"canonical nodes:     {len(canonical_meta)}")
    print(f"edges remapped:      {p['edges_touched']}")
    print(f"edges dropped:       {p['edges_dropped']} (unresolved concepts)")
    print(f"claims tagged:       {p['claims_tagged']}")

    with_edges: dict[str, str] = p["unresolved_with_edges"]  # type: ignore[assignment]
    if with_edges:
        print(f"\nunresolved concepts carrying edges ({len(with_edges)}):")
        for surface in list(with_edges.values())[:10]:
            print(f"  - {surface}")
        if args.apply and not args.allow_drop:
            raise SystemExit(
                "refusing to drop these edges; add the terms to configs/terms.yaml "
                "(or re-run with --allow-drop)"
            )

    if not args.apply:
        print("\ndry run; re-run with --apply")
        return

    backup = db.with_suffix(db.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, backup)
    print(f"backup:              {backup}")

    with con:
        # 1) canonical nodes (only those the data actually reached)
        for canonical, (label, kind) in canonical_meta.items():
            con.execute(
                "INSERT INTO graph_nodes (node_id, node_type, label, concept_id, concept_kind, meta) "
                "VALUES (?, 'concept', ?, ?, ?, '{}') "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "label = excluded.label, concept_id = excluded.concept_id, concept_kind = excluded.concept_kind",
                (concept_node_id(canonical), label, canonical, kind),
            )

        # 2) queue unresolved surfaces for review, then drop their nodes
        for _node_id, surface in p["unresolved"]:  # type: ignore[union-attr]
            key = candidate_key(surface)
            con.execute(
                "INSERT INTO concept_aliases (canonical_concept, aliases, ambiguity_type, context, review_status) "
                "VALUES (?, ?, NULL, 'extracted_unmatched', 'pending') "
                "ON CONFLICT(canonical_concept) DO NOTHING",
                (key, json.dumps([surface], ensure_ascii=False)),
            )
        unresolved_ids = [nid for nid, _ in p["unresolved"]]  # type: ignore[union-attr]
        con.executemany(
            "DELETE FROM graph_edges WHERE from_node = ? OR to_node = ?",
            [(nid, nid) for nid in unresolved_ids],
        )
        con.executemany("DELETE FROM graph_nodes WHERE node_id = ?", [(nid,) for nid in unresolved_ids])

        # 3) remap edges onto canonical ids (drop the now-duplicate source nodes)
        for old, new in mapping.items():
            if old == new:
                continue
            con.execute("UPDATE graph_edges SET from_node = ? WHERE from_node = ?", (new, old))
            con.execute("UPDATE graph_edges SET to_node = ? WHERE to_node = ?", (new, old))
            con.execute("DELETE FROM graph_nodes WHERE node_id = ?", (old,))

        # 4) collapse edges that became identical after merging
        con.execute(
            "DELETE FROM graph_edges WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM graph_edges GROUP BY from_node, to_node, relation)"
        )

        # 5) claims point at the concept, keep their extracted wording
        con.executemany(
            "UPDATE claims SET subject_concept_id = ? WHERE claim_id = ?",
            [(canonical, claim_id) for claim_id, canonical in p["claim_hits"]],  # type: ignore[union-attr]
        )

    con.close()
    print("identity rebuilt")


if __name__ == "__main__":
    main()
