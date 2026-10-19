"""Backfill figure<->text links from the unit side.

FigureAssetRow.caption_ids / explanation_ids were never populated; reverse the
links that DO exist (units carrying figure_ids) so figure -> text expansion
and figure catalogs work in both directions."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow, FigureAssetRow

settings.ensure_dirs()
init_db()

with SessionLocal() as s:
    units = (
        s.query(EvidenceUnitRow)
        .filter(func.json_array_length(EvidenceUnitRow.figure_ids) > 0)
        .all()
    )
    figs = {f.figure_id: f for f in s.query(FigureAssetRow).all()}
    caps: dict[str, list[str]] = defaultdict(list)
    expl: dict[str, list[str]] = defaultdict(list)
    for u in units:
        for fid in u.figure_ids or []:
            if fid not in figs:
                continue
            if u.block_type == "caption":
                caps[fid].append(u.evidence_id)
            else:
                expl[fid].append(u.evidence_id)
    n_cap, n_exp = 0, 0
    for fid, ids in caps.items():
        figs[fid].caption_ids = ids
        n_cap += 1
    for fid, ids in expl.items():
        figs[fid].explanation_ids = ids
        n_exp += 1
    s.commit()
print(f"backfilled caption_ids on {n_cap} figures, explanation_ids on {n_exp} figures")
