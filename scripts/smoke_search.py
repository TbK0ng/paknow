"""Smoke test: index all ingested units, then hybrid-search a known question."""
from paknow.config import settings
from paknow.db import init_db
from paknow.retrieval.index import index_all
from paknow.retrieval.search import hybrid_search, point_to_doc

settings.ensure_dirs()
init_db()

print("indexing (downloads BGE-M3 on first run)...", flush=True)
n = index_all()
print(f"indexed {n} points", flush=True)

q = "double bottom bull flag second low slightly higher"
print(f"\nquery: {q}", flush=True)
for p in hybrid_search(q, top_k=5):
    d = point_to_doc(p)
    print(f"  {d['score']:.4f} [{d['block_type']}] page={d['pdf_page']} {d['text'][:80]!r}", flush=True)

q2 = "双底多头旗形的两个低点必须一样吗"
print(f"\nquery: {q2}", flush=True)
for p in hybrid_search(q2, top_k=5):
    d = point_to_doc(p)
    print(f"  {d['score']:.4f} [{d['block_type']}] page={d['pdf_page']} {d['text'][:80]!r}", flush=True)
