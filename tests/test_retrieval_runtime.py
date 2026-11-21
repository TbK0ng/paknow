from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest

from paknow.retrieval.runtime import InferenceLane, Priority, SingleFlightLRU


def until(predicate):
    end = monotonic() + 4
    while monotonic() < end:
        if predicate():
            return
        sleep(0.005)
    pytest.fail("expected worker state was not reached")


def test_lru_single_flight_eviction_and_failure_recovery():
    cache = SingleFlightLRU(2)
    started, finish = Event(), Event()
    batch, manual = Priority(), Priority(manual=True)

    def fetch(owner):
        started.set()
        assert finish.wait(3)
        assert owner.manual  # the manual subscriber promotes the batch owner
        return 11

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.get, "q", fetch, batch)
        try:
            assert started.wait(3)
            second = pool.submit(cache.get, "q", lambda _: pytest.fail("duplicate work"), manual)
            until(lambda: cache.stats().get("joined"))
        finally:
            finish.set()
        assert first.result(3) == second.result(3) == 11
    assert cache.get("q", lambda _: pytest.fail("same query must survive across jobs"), batch) == 11
    cache.get("2", lambda _: 2, batch)
    cache.get("q", lambda _: -1, batch)
    cache.get("3", lambda _: 3, batch)
    with pytest.raises(OSError):
        cache.get("2", lambda _: (_ for _ in ()).throw(OSError()), batch)
    assert cache.get("2", lambda _: 20, batch) == 20


def test_priority_microbatches_share_one_model_lane_and_scatter_to_each_caller():
    lane = InferenceLane({"embed": 3, "rerank": 2}, coalesce_ms=0)
    running, release = Event(), Event()
    seen = []
    batch, manual = Priority(), Priority(manual=True)

    def model(values):
        seen.append(tuple(values))
        if values == [0]:
            running.set()
            assert release.wait(3)
        return [n * 10 for n in values]

    try:
        with ThreadPoolExecutor(4) as pool:
            lead = pool.submit(lane.call, "rerank", [0], model, batch)
            try:
                assert running.wait(3)
                left = pool.submit(lane.call, "embed", [1, 2], model, batch)
                until(lambda: lane.stats()["queued_jobs"] == 1)
                right = pool.submit(lane.call, "embed", [3, 4], model, batch)
                until(lambda: lane.stats()["queued_jobs"] == 2)
                urgent = pool.submit(lane.call, "embed", [9], model, manual)
                until(lambda: lane.stats()["queued_jobs"] == 3)
            finally:
                release.set()
            assert lead.result(3) == (0,)
            assert urgent.result(3) == (90,)
            assert left.result(3) == (10, 20) and right.result(3) == (30, 40)
        assert seen == [(0,), (9,), (1, 2, 3), (4,)]
        assert lane.stats()["embed_mixed_batches"] == 1
    finally:
        lane.close()


def test_rerank_reuses_exact_pairs_not_order_and_preserves_ties(monkeypatch):
    from paknow.retrieval import search

    calls = []
    monkeypatch.setattr(search, "rank_scores", SingleFlightLRU(10))
    monkeypatch.setattr(search, "_predict_pairs", lambda pairs: calls.extend(pairs) or [1.0] * len(pairs))
    docs = [{"evidence_id": str(i), "text": f"text {i}"} for i in range(3)]
    queries = {str(i): [q] for i, q in enumerate(("A", "B", "A"))}
    assert search.rerank_best(docs, queries, fallback_query="unused") == [docs[0], docs[2], docs[1]]
    assert search.rerank_best([docs[2], docs[0]], {}, fallback_query="A") == [docs[2], docs[0]]
    assert calls == [("A", "text 0"), ("A", "text 2"), ("B", "text 1")]
    assert search.score_pairs([("A", "text 0"), ("A", "changed")]) == (1, 1)
    assert calls[-1] == ("A", "changed") and len(calls) == 4


def test_recall_cache_namespaces_filter_limit_and_generation_and_copies_payload(monkeypatch):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    from paknow.retrieval import search

    calls, generation = [], ["g1"]
    monkeypatch.setattr(search, "recall", SingleFlightLRU(16))
    monkeypatch.setattr(search, "bm25_generation", lambda: "bm25")
    monkeypatch.setattr(search, "corpus", lambda: (generation[0], frozenset({"doc"})))
    def fetch(*args):
        calls.append(args)
        return [{"nested": ["original"]}]
    monkeypatch.setattr(search, "_hybrid_search", fetch)
    filter_a = Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value="a"))])
    first = search.hybrid_search("A", top_k=8, query_filter=filter_a)
    first[0]["nested"].append("changed")
    assert search.hybrid_search("A", top_k=8, query_filter=filter_a)[0]["nested"] == ["original"]
    assert len(calls) == 1
    search.hybrid_search("A", top_k=9, query_filter=filter_a)
    search.hybrid_search("A", top_k=8)
    generation[0] = "g2"
    search.hybrid_search("A", top_k=8, query_filter=filter_a)
    assert len(calls) == 4
    generation[0] = "unsealed"
    for _ in range(2):
        search.hybrid_search("A", top_k=8)
    assert len(calls) == 6
