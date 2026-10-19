"""Sparse-only reindex: rebuild lexical vectors for all indexed units.

Needed after tokenizer/dictionary changes (boundary-aware alias matching,
180 new bilingual terms) — the old sparse vectors contain substring-alias
pollution (e.g. trading_range tokens on any text containing "tr"). Dense
vectors are untouched (update_vectors)."""
from __future__ import annotations

from qdrant_client.models import PointVectors, SparseVector

from paknow.config import settings
from paknow.db import init_db
from paknow.retrieval.index import _point_id, iter_units
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, get_client
from paknow.retrieval.tokenize import sparse_indices_values

settings.ensure_dirs()
init_db()

client = get_client()
n = 0
for rows in iter_units(batch_size=256):
    pvs: list[PointVectors] = []
    for r in rows:
        if not (r.text or "").strip():
            continue
        idx, vals = sparse_indices_values(r.text)
        pvs.append(
            PointVectors(
                id=_point_id(r.evidence_id),
                vector={"sparse": SparseVector(indices=idx, values=vals)},
            )
        )
    if pvs:
        client.update_vectors(collection_name=COLLECTION_TEXT, points=pvs, wait=True)
    n += len(pvs)
print(f"sparse reindexed {n} points")
