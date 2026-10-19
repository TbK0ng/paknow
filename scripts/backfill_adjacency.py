"""Backfill ``adjacent_ids`` on existing units (reading-order neighbors per doc).

Neighbors are the nearest non-footer units in ingest (rowid) order, which is
the parser's reading order for both PDF and EPUB sources.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow

settings.ensure_dirs()
init_db()

with SessionLocal() as s:
    rows = s.execute(
        text(
            "SELECT rowid, evidence_id, doc_id, block_type "
            "FROM evidence_units ORDER BY rowid"
        )
    ).all()
    by_doc: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _rowid, eid, doc_id, bt in rows:
        by_doc[doc_id].append((eid, bt))

updated = 0
with SessionLocal() as s:
    for _doc_id, units in by_doc.items():
        content = [eid for eid, bt in units if bt != "footer"]
        pos = {eid: i for i, eid in enumerate(content)}
        for eid, bt in units:
            if bt == "footer" or eid not in pos:
                continue
            i = pos[eid]
            adj = [
                e
                for e in (
                    content[i - 1] if i else None,
                    content[i + 1] if i + 1 < len(content) else None,
                )
                if e
            ]
            s.query(EvidenceUnitRow).filter(EvidenceUnitRow.evidence_id == eid).update(
                {"adjacent_ids": adj}, synchronize_session=False
            )
            updated += 1
    s.commit()
print(f"updated {updated} units")
