"""Manual ordinary-bar lookup uses only scoped original text."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow.db import Base
from paknow.db.models import DocumentRow, EvidenceUnitRow
from paknow.retrieval import pipeline, search
from paknow.retrieval.qdrant_store import StoreLockedError


def _point(eid, text=None, score=1.0, doc="a"):
    return SimpleNamespace(payload={"evidence_id": eid, "text": text or eid, "doc_id": doc}, score=score)


def _ids(result):
    return [row["evidence_id"] for row in result.citable_units]


def test_each_source_reranks_against_its_own_queries_with_sealed_filter(monkeypatch):
    calls = []
    options = {"first": [_point("a"), _point("b")], "second": [_point("b"), _point("c")]}

    def recall(query, **kwargs):
        calls.append(query)
        assert kwargs["top_k"] == 40
        assert kwargs["query_filter"].must[0].match.any == ["a"]
        return options[query]

    def rank(rows, queries, *, fallback_query, top_k):
        assert queries == {"a": ["first"], "b": ["first", "second"], "c": ["second"]}
        assert fallback_query == "first" and top_k == 80
        return rows[::-1]

    monkeypatch.setattr(pipeline, "hybrid_search", recall)
    monkeypatch.setattr(pipeline, "rerank_best", rank)
    monkeypatch.setattr(pipeline, "adjacent_evidence", lambda ids: ["adjacent"])
    result = pipeline.retrieve_text(["first", "", "first", "second"], doc_ids=frozenset({"a"}))
    assert calls == ["first", "second"]
    assert _ids(result) == ["c", "b", "a", "adjacent"]
    assert not result.failures


def test_recall_rerank_or_adjacent_failure_keeps_available_text(monkeypatch):
    def fail(*_args, **_kwargs):
        raise ValueError("not ready")

    monkeypatch.setattr(pipeline, "hybrid_search", lambda q, **kwargs: fail() if q == "broken" else [_point("a"), _point("b")])
    monkeypatch.setattr(pipeline, "rerank_best", fail)
    monkeypatch.setattr(pipeline, "adjacent_evidence", fail)
    result = pipeline.retrieve_text(["broken", "good"], doc_ids=None)
    assert _ids(result) == ["a", "b"]
    assert result.failures == ["recall:ValueError", "rerank:ValueError", "adjacency:ValueError"]


def test_store_lock_conflict_is_not_silently_degraded(monkeypatch, tmp_path):
    def locked(*_args, **_kwargs):
        raise StoreLockedError(tmp_path / "lock", "other process")

    monkeypatch.setattr(pipeline, "hybrid_search", locked)
    with pytest.raises(StoreLockedError):
        pipeline.retrieve_text(["price action"], doc_ids=None)


def test_diversity_limits_document_and_duplicate_text(monkeypatch):
    many = [_point(f"a{i}", doc="a") for i in range(15)]
    many += [_point("b0", "a0", doc="b"), _point("b1", doc="b"), _point("b2", doc="b")]
    monkeypatch.setattr(pipeline, "hybrid_search", lambda *_args, **_kwargs: many)
    monkeypatch.setattr(pipeline, "rerank_best", lambda docs, *_args, **_kwargs: docs)
    monkeypatch.setattr(pipeline, "adjacent_evidence", lambda ids: [])
    assert _ids(pipeline.retrieve_text(["range"], doc_ids=None)) == [*(f"a{i}" for i in range(8)), "b1", "b2"]


def test_adjacency_uses_reading_order_without_parent_or_duplicate(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(search, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(DocumentRow(doc_id="d", source_path="book.pdf", format="pdf", content_hash="hash"))
        for eid, neighbors in (("a", ["parent", "b", "c", "d"]), ("b", ["a", "c", "e", "f"])):
            db.add(EvidenceUnitRow(evidence_id=eid, doc_id="d", text=eid, text_hash=eid, locator={},
                                   parent_id="parent", adjacent_ids=neighbors))
    assert search.adjacent_evidence(["a", "b"]) == ["c", "d", "e", "f"]
    engine.dispose()
