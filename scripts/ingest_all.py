"""Full ingest + index over all source files (plan P1 L0)."""
import os

from paknow.config import settings
from paknow.db import init_db
from paknow.ingest.pipeline import ingest_epub, ingest_pdf
from paknow.retrieval.index import index_all

settings.ensure_dirs()
init_db()

SOURCES = [
    ("Reading Price Charts Bar by Bar.pdf", "pdf"),
    ("Encyclopedia of Chart Patterns.pdf", "pdf"),
    (
        "Trading Price Action Reversals_ Technical Analysis of Price Charts Bar by Bar for the Serious Trader.epub",
        "epub",
    ),
    (
        "Trading Price Action Trading Ranges_ Technical Analysis of Price Charts Bar by Bar for the Serious Trader.epub",
        "epub",
    ),
    (
        "Trading Price Action Trends_ Technical Analysis of Price Charts Bar by Bar for the Serious Trader.epub",
        "epub",
    ),
]

for path, kind in SOURCES:
    if not os.path.exists(path):
        print(f"SKIP (missing): {path}", flush=True)
        continue
    if kind == "epub":
        row, res = ingest_epub(path)
        print(f"EPUB {path[:40]}: {len(res.evidence_units)} units, {len(res.figures)} figs", flush=True)
    else:
        row, res = ingest_pdf(path)
        print(f"PDF  {path[:40]}: {len(res.evidence_units)} units, {len(res.figures)} figs, {len(res.ocr_pages)} ocr-pages", flush=True)

print("indexing (dense+sparse)...", flush=True)
n = index_all()
print(f"INDEXED {n} points", flush=True)
