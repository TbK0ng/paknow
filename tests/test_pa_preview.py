"""Review previews are replaceable local state, never verdicts or ledger entries."""
from paknow import pa_preview


def test_preview_is_bounded_and_reuses_only_the_same_candidate_and_generation(monkeypatch):
    now = [12.0]
    monkeypatch.setattr(pa_preview, "monotonic", lambda: now[0])
    cache = pa_preview.EvidencePreviews(capacity=2, ttl_seconds=10)
    packet = {"corpus_generation": "sealed-1", "sources": [{"text": "Original"}]}
    old = cache.put("key", packet)
    token = cache.put("key", packet)
    assert cache.matching(token, "other-candle", "sealed-1") is None
    assert cache.matching(token, "key", "sealed-2") is None
    packet["sources"][0]["text"] = "Modified input"
    matched = cache.matching(token, "key", "sealed-1")
    assert matched["sources"][0]["text"] == "Original"
    matched["sources"].clear()
    assert cache.matching(token, "key", "sealed-1")["sources"]
    cache.put("third", packet)
    assert cache.matching(old, "key", "sealed-1") is None
    now[0] += 10
    assert cache.matching(token, "key", "sealed-1") is None
