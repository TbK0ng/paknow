"""The retrieval seam returns citable text hits, auxiliary context, graph slots,
and failures as separate partitions. Assertions are on that return value only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from paknow.graph import expansion
from paknow.retrieval import pipeline
from paknow.retrieval.qdrant_store import StoreLockedError


def _unit(eid: str, *, text: str = "text", parent: bool = False, adjacent: bool = False) -> dict:
    return {
        "evidence_id": eid,
        "doc_id": "d1",
        "block_type": "paragraph",
        "text": text,
        "chapter_path": ["ch"],
        "pdf_page": 1,
        "printed_page": None,
        "figure_label": None,
        "epub_href": None,
        "figure_ids": [],
        "provenance": "native_text",
        "is_parent": parent,
        "is_adjacent": adjacent,
    }


def _patched(monkeypatch, *, hits_by_query, fail_queries=(), expanded=None):
    def fake_search(q, *, top_k=40):
        if q in fail_queries:
            raise RuntimeError("index offline")
        return [SimpleNamespace(payload=doc, score=doc.get("score", 1.0)) for doc in hits_by_query.get(q, [])]

    def fake_point_to_doc(p):
        return p.payload

    def fake_rerank(docs, queries, *, fallback_query, top_k=None):
        return docs

    monkeypatch.setattr(pipeline, "hybrid_search", fake_search)
    monkeypatch.setattr(pipeline, "point_to_doc", fake_point_to_doc)
    monkeypatch.setattr(pipeline, "rerank_best", fake_rerank)
    monkeypatch.setattr(
        pipeline,
        "expand_evidence",
        lambda ids, **kwargs: expanded or {"units": [], "figures": []},
    )
    monkeypatch.setattr(pipeline, "toc_recall", lambda q: [{"doc_title": "T", "chapter_path": ["c"]}])
    monkeypatch.setattr(pipeline, "barref_lookup", lambda q: [{"bars": ["1"], "relation": "top", "pattern": "p", "snippet": "s"}])
    # graph expansion is a separate partition; this test file exercises text recall
    monkeypatch.setattr(expansion, "load_mechanism_candidates", lambda *a, **k: [])
    monkeypatch.setattr(expansion, "load_taxonomy_candidates", lambda *a, **k: [])


def test_text_hits_and_auxiliary_context_are_separate_partitions(monkeypatch):
    hits = {"q1": [{"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9}]}
    _patched(
        monkeypatch,
        hits_by_query=hits,
        expanded={
            "units": [_unit("e1"), _unit("p1", parent=True), _unit("n1", adjacent=True)],
            "figures": [{"figure_id": "f1"}],
        },
    )

    result = pipeline.retrieve_evidence("q1", ["q1", "q1"], needs=["toc"])

    assert [h["evidence_id"] for h in result.text_hits] == ["e1"]
    # parent/adjacent units are context, not citable hits
    assert {u["evidence_id"] for u in result.context_units} == {"p1", "n1"}
    assert [f["figure_id"] for f in result.figures] == ["f1"]
    assert result.toc_context == [{"doc_title": "T", "chapter_path": ["c"]}]
    assert result.barrefs[0]["pattern"] == "p"
    assert result.graph_evidence == []
    assert result.failures == []
    assert result.query_count == 1  # duplicate query collapsed


def test_failed_query_is_recorded_and_others_still_return(monkeypatch):
    hits = {
        "good": [{"evidence_id": "e2", "text": "b", "doc_id": "d1", "score": 0.5}],
    }
    _patched(monkeypatch, hits_by_query=hits, fail_queries={"bad"})

    result = pipeline.retrieve_evidence("good", ["bad", "good"])

    assert [h["evidence_id"] for h in result.text_hits] == ["e2"]
    assert any("index offline" in f for f in result.failures)
    assert result.query_count == 2


def test_accumulator_carries_hits_across_rounds(monkeypatch):
    _patched(monkeypatch, hits_by_query={"new": [{"evidence_id": "e3", "text": "c", "doc_id": "d2", "score": 0.1}]})

    result = pipeline.retrieve_evidence(
        "q",
        ["new"],
        acc={"old": {"evidence_id": "e_old", "text": "old", "doc_id": "d0", "score": 9.0}},
    )

    assert [h["evidence_id"] for h in result.text_hits] == ["e_old", "e3"]
    assert set(result.acc) == {"old", "e3"}


def test_expand_failure_does_not_break_text_hits(monkeypatch):
    _patched(monkeypatch, hits_by_query={"q": [{"evidence_id": "e4", "text": "d", "doc_id": "d1", "score": 0.2}]})

    def boom(ids, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(pipeline, "expand_evidence", boom)

    result = pipeline.retrieve_evidence("q", ["q"])

    assert [h["evidence_id"] for h in result.text_hits] == ["e4"]
    assert any("db locked" in f for f in result.failures)


def test_graph_off_is_an_empty_partition_with_identical_text_hits(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={
            "q": [
                {"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9},
                {"evidence_id": "e2", "text": "b", "doc_id": "d1", "score": 0.5},
            ]
        },
    )
    seeded: list[str] = []
    monkeypatch.setattr(
        expansion, "seed_concepts", lambda text, **kw: seeded.append(text) or ["concept"]
    )

    off = pipeline.retrieve_evidence(
        "q",
        ["q"],
        prior_graph_evidence=[{"evidence_id": "g1"}],
        expand_graph=False,
    )

    assert seeded == []  # no seed query was submitted
    assert off.graph_evidence == []
    assert off.graph_stats["enabled"] is False
    assert off.graph_stats["selected"] == 0
    assert off.graph_stats["accumulated"] == 0
    assert off.failures == []

    on = pipeline.retrieve_evidence("q", ["q"])

    assert seeded[0] == "q"  # the query seed is submitted once the layer is on
    assert on.graph_stats["enabled"] is True
    assert [h["evidence_id"] for h in on.text_hits] == [
        h["evidence_id"] for h in off.text_hits
    ]
    assert on.citable_units == off.citable_units
    assert on.citation_index == off.citation_index


def test_text_width_reaches_the_diversity_step(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={
            "q": [{"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9}]
        },
    )
    seen = {}

    def fake_diversify(hits, *, top_k=14, max_per_doc=8, dedupe_chars=200):
        seen["top_k"] = top_k
        seen["max_per_doc"] = max_per_doc
        return hits

    monkeypatch.setattr(pipeline, "diversify", fake_diversify)

    pipeline.retrieve_evidence("q", ["q"], text_width=20)

    assert seen == {"top_k": 20, "max_per_doc": 8}


def test_text_width_defaults_to_the_production_fourteen(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={
            "q": [{"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9}]
        },
    )
    seen = {}

    def fake_diversify(hits, *, top_k=14, max_per_doc=8, dedupe_chars=200):
        seen["top_k"] = top_k
        return hits

    monkeypatch.setattr(pipeline, "diversify", fake_diversify)

    pipeline.retrieve_evidence("q", ["q"], expand_graph=False)

    assert seen["top_k"] == 14


def test_candidate_pool_keeps_the_ranked_head_before_diversity(monkeypatch):
    docs = [
        {"evidence_id": f"e{i}", "text": f"文本{i}", "doc_id": f"d{i}", "score": 1.0 - i / 100}
        for i in range(40)
    ]
    _patched(monkeypatch, hits_by_query={"q": docs})

    result = pipeline.retrieve_evidence("q", ["q"], expand_graph=False)

    assert [h["evidence_id"] for h in result.text_hits] == [f"e{i}" for i in range(14)]
    # the judge's pool is the ranked head before the diversity cap: the whole
    # candidate set while it fits the 80-wide rerank pool, not the 14-item
    # window the answer reads
    assert [h["evidence_id"] for h in result.candidate_hits] == [
        f"e{i}" for i in range(40)
    ]
    assert {h["evidence_id"] for h in result.text_hits} <= {
        h["evidence_id"] for h in result.candidate_hits
    }


def test_text_width_must_be_positive():
    with pytest.raises(ValueError, match="text_width"):
        pipeline.retrieve_evidence("q", ["q"], text_width=0)


def test_graph_off_does_not_report_seed_failures(monkeypatch):
    _patched(monkeypatch, hits_by_query={"q": [{"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9}]})

    def boom(*args, **kwargs):
        raise RuntimeError("seed offline")

    monkeypatch.setattr(expansion, "seed_concepts", boom)

    result = pipeline.retrieve_evidence("q", ["q"], expand_graph=False)

    assert [h["evidence_id"] for h in result.text_hits] == ["e1"]
    assert result.failures == []


def test_each_hit_keeps_the_queries_that_returned_it(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={
            "orig": [{"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9}],
            "step-back": [{"evidence_id": "e2", "text": "b", "doc_id": "d1", "score": 0.8}],
            "sub": [
                {"evidence_id": "e1", "text": "a", "doc_id": "d1", "score": 0.9},
                {"evidence_id": "e3", "text": "c", "doc_id": "d2", "score": 0.7},
            ],
        },
    )
    seen = {}

    def capture(docs, queries, *, fallback_query, top_k=None):
        seen["queries"] = {k: list(v) for k, v in queries.items()}
        seen["fallback"] = fallback_query
        seen["pool"] = len(docs)
        return docs

    monkeypatch.setattr(pipeline, "rerank_best", capture)

    pipeline.retrieve_evidence("orig", ["orig", "step-back", "sub"], expand_graph=False)

    assert seen["queries"] == {"e1": ["orig", "sub"], "e2": ["step-back"], "e3": ["sub"]}
    assert seen["fallback"] == "orig"


def test_rerank_scores_a_hit_against_the_query_that_found_it(monkeypatch):
    from paknow.retrieval import search

    class FakeModel:
        def predict(self, pairs):
            return [10.0 if q == "step-back" else 0.0 for q, _ in pairs]

    monkeypatch.setattr(search, "_reranker", lambda: FakeModel())
    docs = [
        {"evidence_id": "orig-hit", "text": "a"},
        {"evidence_id": "step-hit", "text": "b"},
    ]

    out = search.rerank_best(
        docs,
        {"orig-hit": ["orig"], "step-hit": ["step-back"]},
        fallback_query="orig",
    )

    # scoring everything with "orig" would leave the input order untouched
    assert [d["evidence_id"] for d in out] == ["step-hit", "orig-hit"]


def test_rerank_falls_back_to_the_rewritten_query_for_carried_hits(monkeypatch):
    from paknow.retrieval import search

    class FakeModel:
        def predict(self, pairs):
            return [len(q) for q, _ in pairs]

    monkeypatch.setattr(search, "_reranker", lambda: FakeModel())
    docs = [{"evidence_id": "carried", "text": "a"}]

    out = search.rerank_best(docs, {}, fallback_query="orig")

    assert [d["evidence_id"] for d in out] == ["carried"]


def test_diversify_keeps_headings_that_only_share_an_opening():
    from paknow.retrieval import search

    head = "交易区间开盘：多头突破失败" + "甲" * 90
    docs = [
        {"evidence_id": "a", "doc_id": "d1", "text": head + "第一种结果" * 30},
        {"evidence_id": "b", "doc_id": "d1", "text": head + "第二种结果" * 30},
        {"evidence_id": "c", "doc_id": "d1", "text": head + "第一种结果" * 30},  # exact duplicate of a
    ]

    out = search.diversify(docs, top_k=5)

    assert [d["evidence_id"] for d in out] == ["a", "b"]


def test_diversify_caps_one_book_at_the_doc_limit():
    from paknow.retrieval import search

    docs = [
        {"evidence_id": f"e{i}", "doc_id": "big", "text": f" distintos {i} " * 20}
        for i in range(12)
    ]

    out = search.diversify(docs, top_k=14, max_per_doc=8)

    assert len(out) == 8
def test_store_lock_conflict_is_not_swallowed_as_a_failure(monkeypatch):
    # the API answers 503, so the round must not degrade into "no hits, no error"
    def locked(q, *, top_k=40):
        raise StoreLockedError(Path("data/objects/qdrant.lock"), "pid=4242")

    monkeypatch.setattr(pipeline, "hybrid_search", locked)

    with pytest.raises(StoreLockedError):
        pipeline.retrieve_evidence("q", ["q"], expand_graph=False)
