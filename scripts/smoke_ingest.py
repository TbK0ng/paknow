"""Smoke test: ingest one EPUB end-to-end and report counts."""
from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow, FigureAssetRow
from paknow.ingest.manifest import resolve_source
from paknow.ingest.pipeline import ingest_epub

settings.ensure_dirs()
init_db()

path = resolve_source(
    "Trading Price Action Trends_ Technical Analysis of Price Charts Bar by Bar for the Serious Trader.epub"
)
row, result = ingest_epub(str(path))

with SessionLocal() as s:
    n_units = (
        s.query(EvidenceUnitRow)
        .filter(EvidenceUnitRow.doc_id == row.doc_id)
        .count()
    )
    n_figs = (
        s.query(FigureAssetRow).filter(FigureAssetRow.doc_id == row.doc_id).count()
    )

print(f"doc_id    = {row.doc_id}")
print(f"title     = {result.title!r}")
print(f"author    = {result.author!r}")
print(f"language  = {result.language!r}")
print(f"entries   = {result.entry_count}")
print(f"toc       = {len(result.toc)} nav points")
print(f"units     = {len(result.evidence_units)} parsed / {n_units} persisted")
print(f"figures   = {len(result.figures)} parsed / {n_figs} persisted")
linked = sum(1 for u in result.evidence_units if u.figure_ids)
print(f"units with figure links = {linked}")

# sample: first figure + first explanation that references it
for f in result.figures[:3]:
    print(f"  FIG {f.figure_id} label={f.locator.figure_label} {f.width_px}x{f.height_px} rel={f.rel_path}")
for u in result.evidence_units:
    if u.figure_ids:
        print(f"  EXPLANATION[{u.evidence_id[:12]}] figs={u.figure_ids} text={u.text[:60]!r}")
        break
