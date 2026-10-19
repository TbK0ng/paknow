"""Benchmark each indexing phase to find the bottleneck."""
import time

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.retrieval.embedding import embed_texts
from paknow.retrieval.tokenize import sparse_indices_values

with SessionLocal() as s:
    rows = (
        s.query(EvidenceUnitRow)
        .filter(EvidenceUnitRow.block_type != "footer")
        .limit(500)
        .all()
    )
texts = [r.text for r in rows]
print(f"{len(texts)} texts, avg len {sum(len(t) for t in texts)//len(texts)}", flush=True)

t0 = time.time()
for t in texts:
    sparse_indices_values(t)
print(f"sparse 500: {time.time()-t0:.1f}s", flush=True)

t0 = time.time()
v = embed_texts(texts[:100])
print(f"embed 100: {time.time()-t0:.1f}s shape={v.shape}", flush=True)

t0 = time.time()
v = embed_texts(texts)
print(f"embed 500: {time.time()-t0:.1f}s", flush=True)
