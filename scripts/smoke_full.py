"""End-to-end: reset -> ingest EPUB -> index -> search."""
import os
import shutil
import time

from paknow.config import settings
from paknow.db import init_db
from paknow.ingest.pipeline import ingest_epub
from paknow.retrieval.index import index_all
from paknow.retrieval.search import hybrid_search, point_to_doc

# reset
if os.path.exists("data/paknow.db"):
    os.remove("data/paknow.db")
if os.path.exists("data/objects/qdrant"):
    shutil.rmtree("data/objects/qdrant")

settings.ensure_dirs()
init_db()

t0 = time.time()
row, res = ingest_epub(
    "Trading Price Action Trends_ Technical Analysis of Price Charts Bar by Bar for the Serious Trader.epub"
)
print(f"ingest: {len(res.evidence_units)} units in {time.time()-t0:.1f}s", flush=True)

t0 = time.time()
n = index_all()
print(f"index: {n} points in {time.time()-t0:.1f}s", flush=True)

for q in [
    "double bottom bull flag second low slightly higher",
    "双底多头旗形的两个低点必须一样吗",
]:
    print(f"\nquery: {q}", flush=True)
    for p in hybrid_search(q, top_k=5):
        d = point_to_doc(p)
        print(f"  {d['score']:.4f} [{d['block_type']}] {d['text'][:70]!r}", flush=True)
