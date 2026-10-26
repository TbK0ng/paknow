"""Backfill the chunk parents that ingest used to drop.

``chunk_units`` kept only the children, so every ``parent_id`` dangled
(1,670/1,670 on the 2026-10-24 snapshot). Child ids are deterministic —
``stable_id("ev", doc_id, parent_id, str(j))`` — so a parent row is rebuilt
from its children in split order: same locator, joined text, ``child_ids``
filled, no ``parent_id`` of its own. Parents are never indexed into Qdrant,
so this script writes rows only.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow
from paknow.ids import content_hash, stable_id

settings.ensure_dirs()
init_db()

with SessionLocal() as s:
    rows = s.execute(
        text(
            "SELECT evidence_id, doc_id, parent_id FROM evidence_units "
            "WHERE parent_id IS NOT NULL"
        )
    ).all()
    by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for eid, doc_id, parent_id in rows:
        by_parent[parent_id].append((eid, doc_id))
    existing = {
        eid
        for (eid,) in s.query(EvidenceUnitRow.evidence_id).filter(
            EvidenceUnitRow.evidence_id.in_(list(by_parent))
        )
    } if by_parent else set()

created = 0
with SessionLocal() as s:
    for parent_id, children in by_parent.items():
        if parent_id in existing:
            continue
        doc_id = children[0][1]
        ordered = [stable_id("ev", doc_id, parent_id, str(j)) for j in range(len(children))]
        if set(ordered) != {eid for eid, _ in children}:
            print(f"skip {parent_id}: children do not match the split order")
            continue
        first = s.get(EvidenceUnitRow, ordered[0])
        if first is None:
            print(f"skip {parent_id}: first child {ordered[0]} missing")
            continue
        joined = "".join(s.get(EvidenceUnitRow, eid).text for eid in ordered)
        parent = EvidenceUnitRow(
            **{
                c.name: getattr(first, c.name)
                for c in EvidenceUnitRow.__table__.columns
            }
        )
        parent.evidence_id = parent_id
        parent.text = joined
        parent.text_hash = content_hash(joined.encode("utf-8"))
        parent.parent_id = None
        parent.child_ids = ordered
        s.merge(parent)
        created += 1
    s.commit()
print(f"created {created} parent units from {len(by_parent)} dangling parent ids")
