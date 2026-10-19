"""Link PDF caption units to their figures (both directions) and sync the
Qdrant payload so figure catalogs can be built from caption recall.

Strategy (geometric): caption bbox near the figure bbox on the same page
(x-overlap, y within ±120pt band); the figure label is backfilled from the
caption's "图N" prefix when missing. EPUB figures are matched by label
("FIGURE I.1") where captions mention them.
"""
from __future__ import annotations

import re
from collections import defaultdict

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow, FigureAssetRow
from paknow.retrieval.index import _point_id
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, get_client

settings.ensure_dirs()
init_db()

_ZH_RE = re.compile(r"图\s*(\d+(?:\.\d+)?)")


def _norm_label(label: str | None) -> str | None:
    if not label:
        return None
    return re.sub(r"^(图|figure)\s*", "", label.strip(), flags=re.I)


def _overlap_x(a: list, b: list) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2])


payload_updates: dict[str, list[str]] = {}
geo_linked = 0
label_linked = 0
labels_set = 0

with SessionLocal() as s:
    figs = s.query(FigureAssetRow).all()
    units = (
        s.query(EvidenceUnitRow)
        .filter(EvidenceUnitRow.block_type.in_(("caption", "paragraph")))
        .all()
    )
    units_by_doc_page: dict[tuple, list[tuple[EvidenceUnitRow, list]]] = defaultdict(list)
    for u in units:
        bb = (u.locator or {}).get("bbox")
        if bb and len(bb) >= 4:
            units_by_doc_page[(u.doc_id, u.pdf_page_1based)].append((u, bb))

    # 1) geometric: caption bbox near the figure bbox (same page, x-overlap)
    for f in figs:
        fb = (f.locator or {}).get("bbox")
        page = f.pdf_page_1based or (f.locator or {}).get("pdf_page_1based")
        if not (fb and len(fb) >= 4 and page):
            continue
        for u, ub in units_by_doc_page.get((f.doc_id, page), []):
            if not _overlap_x(fb, ub):
                continue
            if not (fb[1] - 120 <= ub[1] <= fb[3] + 120):
                continue
            ids = list(u.figure_ids or [])
            if f.figure_id not in ids:
                ids.append(f.figure_id)
                u.figure_ids = ids
                payload_updates[u.evidence_id] = ids
                geo_linked += 1
            # backfill the figure label from the caption prefix
            if not f.figure_label:
                m = _ZH_RE.search(u.text or "")
                if m and u.block_type == "caption":
                    f.figure_label = m.group(1)
                    labels_set += 1

    # 2) label fallback (EPUB "FIGURE I.1" style, or already-labeled figures)
    by_label: dict[tuple, list[FigureAssetRow]] = defaultdict(list)
    for f in figs:
        lab = _norm_label(f.figure_label)
        if lab:
            by_label[(f.doc_id, lab)].append(f)
    for u in units:
        m = _ZH_RE.search(u.text or "")
        if not m:
            continue
        for f in by_label.get((u.doc_id, m.group(1)), []):
            ids = list(u.figure_ids or [])
            if f.figure_id not in ids:
                ids.append(f.figure_id)
                u.figure_ids = ids
                payload_updates[u.evidence_id] = ids
                label_linked += 1
    s.commit()

# sync payloads so caption_search surfaces figure_ids
client = get_client()
for uid, fids in payload_updates.items():
    client.set_payload(
        collection_name=COLLECTION_TEXT,
        payload={"figure_ids": fids},
        points=[_point_id(uid)],
        wait=True,
    )
print(
    f"geo={geo_linked} label={label_linked} labels_set={labels_set} "
    f"payload={len(payload_updates)}"
)
