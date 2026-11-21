"""Real embedded BM25 and the current text index readiness contract."""
import math
from contextlib import closing

import numpy as np
import pytest
from qdrant_client import QdrantClient, models

from paknow.retrieval import qdrant_store, search
from paknow.retrieval.bm25 import VECTOR_NAME, query_vector
from paknow.retrieval.index import rebuild_bm25


def test_sparse_scoring_and_hybrid_search_survive_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(qdrant_store.settings, "objects_dir", tmp_path)
    monkeypatch.setattr(qdrant_store.settings, "qdrant_url", "")
    name = qdrant_store.COLLECTION_TEXT
    with closing(QdrantClient(path=str(tmp_path / "qdrant"))) as client:
        client.create_collection(name, {"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
                                 sparse_vectors_config={VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)})
        texts = ["zephyr common", "zephyr zephyr common filler filler filler filler", "common neutral", "..."]
        client.upsert(name, [models.PointStruct(id=i,
            vector={"dense": [1.0, 0.0] if i == 4 else [0.0, 1.0]},
            payload={"text": text, "search_text": text, "evidence_id": f"e{i}"}) for i, text in enumerate(texts, 1)])
        before, _ = client.scroll(name, with_vectors=True)
        with pytest.raises(qdrant_store.StoreNotReadyError, match="BM25"):
            qdrant_store.check_store_ready(client)
        assert rebuild_bm25(client) == 3
    with closing(QdrantClient(path=str(tmp_path / "qdrant"))) as client:
        assert qdrant_store.check_store_ready(client) == 4
        after, _ = client.scroll(name, with_vectors=True)
        for old, new in zip(before, after, strict=True):
            assert old.id == new.id and old.payload == new.payload
            assert old.vector == {key: new.vector[key] for key in old.vector}
        assert VECTOR_NAME not in after[-1].vector
        scored = client.query_points(name, query_vector("zephyr zephyr"), using=VECTOR_NAME).points
        assert [hit.id for hit in scored] == [1, 2]
        # Worked example: N=3, df=2, document lengths 2/7/2, avgdl=11/3.
        idf = math.log(1.6)
        assert scored[0].score == pytest.approx(idf * 2.2 / (1.3 + 0.9 * 6 / 11))
        assert scored[1].score == pytest.approx(idf * 4.4 / (2.3 + 0.9 * 21 / 11))
        monkeypatch.setattr(search, "get_client", lambda: client)
        monkeypatch.setattr(search, "embed_query", lambda _: np.array([1.0, 0.0]))
        hits = search.hybrid_search("zephyr", dense_limit=1, sparse_limit=1)
        assert {hit.id for hit in hits} == {1, 4}
        generation = qdrant_store.bm25_generation()
        client.upsert(name, [models.PointStruct(id=5, vector={"dense": [0.0, 1.0]},
                                               payload={"search_text": "another wider neutral passage"})])
        with pytest.raises(qdrant_store.StoreNotReadyError, match="BM25"):
            qdrant_store.check_store_ready(client)
        assert rebuild_bm25(client) == 4
        assert qdrant_store.check_store_ready(client) == 5
        assert qdrant_store.bm25_generation() != generation


def test_canonical_terms_and_case_keep_shared_lexical_ids():
    assert set(query_vector("双底").indices) & set(query_vector("double bottom").indices)
    assert query_vector("EMA20") == query_vector("ema20")
    assert not (set(query_vector("H2").indices) & set(query_vector("H20").indices))
