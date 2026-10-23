"""The retrieval seam returns citable text hits, auxiliary context, graph slots,
and failures as separate partitions. Assertions are on that return value only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from paknow.retrieval import pipeline


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

    monkeypatch.setattr(pipeline, "hybrid_search", fake_search)
    monkeypatch.setattr(pipeline, "point_to_doc", fake_point_to_doc)
    monkeypatch.setattr(pipeline, "rerank", lambda q, docs, top_k=None: docs)
    monkeypatch.setattr(
        pipeline,
        "expand_evidence",
        lambda ids, **kwargs: expanded or {"units": [], "figures": []},
    )
    monkeypatch.setattr(pipeline, "toc_recall", lambda q: [{"doc_title": "T", "chapter_path": ["c"]}])
    monkeypatch.setattr(pipeline, "barref_lookup", lambda q: [{"bars": ["1"], "relation": "top", "pattern": "p", "snippet": "s"}])


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
    assert result.graph_evidence == [] and result.graph_context == {}
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
