"""Ticket 07: one read before retrieval, feeding the chart sub-queries.

The read runs between ``route`` and ``retrieve``; retrieval merges wording
derived from it under the rewrite cap; without a read the text path is
byte-identical. No model, no Qdrant, no DB: every chain step is stubbed.
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from PIL import Image

from paknow.chart import ChartRead, chart_queries
from paknow.retrieval.pipeline import RetrievalResult
from paknow.workflows import graph as graph_module
from paknow.workflows.router import QueryPlan, retrieval_queries

QUESTION = "这张图是什么形态？下一步应该观察什么？"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (10, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


def _read(**overrides: Any) -> ChartRead:
    fields: dict[str, Any] = {
        "instrument": None,
        "timeframe": None,
        "market_state": "trend_up",
        "swings": [],
        "levels": [
            {
                "price": 7519.75,
                "source": "annotation",
                "strip": 1,
                "basis": "图上标注 2 @ 7519.75",
            }
        ],
        "focus": [
            {"price": 7519.75, "why": "图上标注"},
            {"price": 7531.0, "why": "前高"},
        ],
        "patterns": ["上升通道"],
        "annotations": ["2 @ 7519.75"],
        "indicators": [],
        "unreadable": [],
    }
    fields.update(overrides)
    return ChartRead(**fields)


def _plan(intent: str = "chart_analysis") -> QueryPlan:
    return QueryPlan(
        intent=intent,
        routes=["hybrid_text"],
        evidence_needs=["context"],
        step_back="结构 处理",
        sub_queries=["子问题A", "子问题B"],
        max_search_rounds=3,
    )


def _checks(sequence: Iterator[bool]):
    def fake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        sufficient = next(sequence)
        next_query = None if sufficient else "缺口：区间边缘怎么处理"
        return SimpleNamespace(
            next_query=next_query,
            model_dump=lambda: {"sufficient": sufficient, "next_query": next_query},
        )

    return fake


def _stub_chain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    intent: str = "chart_analysis",
    read_result: tuple[ChartRead | None, dict[str, str] | None] | None = None,
    checks: tuple[bool, ...] = (True,),
) -> tuple[list[list[bytes]], list[dict[str, Any]], list[dict[str, Any]]]:
    reads: list[list[bytes]] = []
    retrievals: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = []

    def fake_read(images, **_kwargs):
        if read_result is None:
            raise AssertionError("this run must not read a chart")
        reads.append(list(images))
        return read_result

    def fake_retrieve(query, queries, **kwargs):
        retrievals.append({"query": query, "queries": list(queries), **kwargs})
        return RetrievalResult(
            acc={},
            graph_stats={"enabled": kwargs.get("expand_graph", True)},
        )

    def fake_generate(*args, **kwargs):
        generated.append({"args": args, **kwargs})
        read = kwargs.get("chart_read")
        return {
            "answer": "答",
            "evidence_ids": [],
            "figure_ids": [],
            "uncertainty": [],
            "chart_read": read,
            "chart_read_status": "structured" if read else "unstructured",
        }

    monkeypatch.setattr(graph_module, "read_chart", fake_read)
    monkeypatch.setattr(graph_module, "route", lambda query, images=None: _plan(intent))
    monkeypatch.setattr(graph_module, "select_sop", lambda intent: None)
    monkeypatch.setattr(graph_module, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(graph_module, "check_sufficiency", _checks(iter(checks)))
    monkeypatch.setattr(graph_module, "generate", fake_generate)
    monkeypatch.setattr(graph_module, "save_trace", lambda *a, **k: None)
    monkeypatch.setattr(graph_module, "_maybe_auto_optimize", lambda: None)
    return reads, retrievals, generated


# --- the read runs exactly once, before retrieval -------------------------- #


def test_chart_run_reads_once_and_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(_read(), None)
    )

    result = graph_module.run_query(QUESTION, images=[_png()])

    assert len(reads) == 1
    assert reads[0]  # the upload reached the read
    assert result["chart_read"] == _read().model_dump()
    assert result["chart_read_status"] == "structured"
    assert len(retrievals) == 1


def test_gap_round_retrieves_again_without_a_second_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(_read(), None), checks=(False, True)
    )

    graph_module.run_query(QUESTION, images=[_png()])

    assert len(reads) == 1
    assert len(retrievals) == 2


def test_answer_mode_is_still_chart_and_receives_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads, _retrievals, generated = _stub_chain(
        monkeypatch, read_result=(_read(), None)
    )

    graph_module.run_query(QUESTION, images=[_png()])

    assert generated[0]["mode"] == "chart"
    assert generated[0]["chart_read"] == _read().model_dump()


# --- the read feeds retrieval; no read leaves retrieval untouched ---------- #


def test_read_derived_terms_join_the_sub_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    _reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(_read(), None)
    )

    graph_module.run_query(QUESTION, images=[_png()])

    queries = retrievals[0]["queries"]
    assert queries[0] == QUESTION  # the user's words keep the first slot
    assert any("上升通道" in q for q in queries)  # patterns
    assert any("交易区间" in q or "上升趋势" in q for q in queries)  # market_state
    assert any("7519.75" in q and "7531" in q for q in queries)  # focus prices
    assert any("2 @ 7519.75" in q for q in queries)  # annotation terms
    assert len(queries) <= 6  # the rewrite cap still holds


def test_chart_question_without_a_read_keeps_todays_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(None, {"reason": "timeout", "detail": "timed out"})
    )

    graph_module.run_query(QUESTION, images=[_png()])

    assert retrievals[0]["queries"] == retrieval_queries(_plan(), QUESTION)


def test_empty_levels_keeps_todays_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    _reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(_read(levels=[]), None)
    )

    graph_module.run_query(QUESTION, images=[_png()])

    assert retrievals[0]["queries"] == retrieval_queries(_plan(), QUESTION)


def test_text_question_reads_nothing_and_keeps_graph_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "双底是什么？"
    reads, retrievals, generated = _stub_chain(monkeypatch, intent="knowledge_qa")

    result = graph_module.run_query(question)

    assert reads == []
    assert retrievals[0]["queries"] == retrieval_queries(_plan("knowledge_qa"), question)
    assert retrievals[0]["expand_graph"] is True
    assert generated[0]["chart_read"] is None
    assert result["chart_read"] is None


def test_chart_question_keeps_graph_expansion_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reads, retrievals, _generated = _stub_chain(
        monkeypatch, read_result=(_read(), None)
    )

    result = graph_module.run_query(QUESTION, images=[_png()])

    assert retrievals[0]["expand_graph"] is False
    assert result["graph_stats"]["enabled"] is False


# --- a failed read degrades, it never breaks the run ----------------------- #


def test_failed_read_continues_and_records_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads, retrievals, generated = _stub_chain(
        monkeypatch, read_result=(None, {"reason": "timeout", "detail": "timed out"})
    )

    result = graph_module.run_query(QUESTION, images=[_png()])

    assert len(reads) == 1
    assert result["chart_read"] is None
    assert result["chart_read_status"] == "unstructured"
    assert "chart_read: timeout" in result["failures"]
    assert result["answer"]["answer"] == "答"
    assert retrievals and generated


# --- the derivation itself -------------------------------------------------- #


def test_chart_queries_are_bounded_and_skip_empty_reads() -> None:
    assert chart_queries(None) == []
    assert chart_queries(_read(levels=[]).model_dump()) == []
    assert len(chart_queries(_read().model_dump())) <= 3
    # a single-character tool label is not an annotation term
    assert not any(
        "图上标注" in q
        for q in chart_queries(_read(annotations=["2"]).model_dump())
    )
    # nor is a whole chart-title block
    assert not any(
        "图上标注" in q
        for q in chart_queries(
            _read(annotations=["TradeStation Chart Analysis - ESM08.D - 3 min [CME] E-mini S&P 500"]).model_dump()
        )
    )


def test_chart_queries_are_empty_when_every_field_is() -> None:
    bare = _read(
        market_state="unclear",
        levels=[{"price": None, "source": "unknown", "strip": 1, "basis": "无"}],
        focus=[],
        patterns=[],
        annotations=[],
    )

    assert chart_queries(bare.model_dump()) == ["走势结构 如何处理"]
