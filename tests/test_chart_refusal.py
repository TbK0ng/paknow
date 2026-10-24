"""Ticket 06: a non-chart upload is refused, not turned into a price list.

The read names the picture's text and layout; the answer opens with the
refusal; retrieval keeps one unit. A genuine chart and a text question keep
today's prompt and retrieval exactly.
"""
from __future__ import annotations

from typing import Any

import pytest

from paknow import chart as chart_module
from paknow.chart import ChartRead, is_non_chart
from paknow.retrieval.pipeline import RetrievalResult
from paknow.workflows import generate as generate_module
from paknow.workflows import graph as graph_module
from paknow.workflows.router import QueryPlan


def _read(**overrides: Any) -> ChartRead:
    fields: dict[str, Any] = {
        "instrument": None,
        "timeframe": None,
        "market_state": "trading_range",
        "swings": [],
        "levels": [
            {
                "price": 7519.75,
                "source": "annotation",
                "strip": 1,
                "basis": "图上标注 2 @ 7519.75",
            }
        ],
        "focus": [{"price": 7519.75, "why": "图上标注"}],
        "patterns": ["trading range"],
        "annotations": ["2 @ 7519.75"],
        "indicators": [],
        "unreadable": [],
    }
    fields.update(overrides)
    return ChartRead(**fields)


def _non_chart(**overrides: Any) -> ChartRead:
    return _read(
        market_state="unclear",
        levels=[],
        focus=[],
        swings=[],
        patterns=[],
        annotations=["第 3 章 市场结构", "下单", "止损放在前低下方"],
        **overrides,
    )


def _compose(
    monkeypatch: pytest.MonkeyPatch, read: ChartRead
) -> dict[str, Any]:
    monkeypatch.setattr(generate_module, "read_chart", lambda images, **_k: (read, None))
    seen: dict[str, Any] = {}

    def fake_answer(schema: Any, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        seen["system"], seen["user"] = messages[0]["content"], messages[1]["content"]
        return generate_module.Answer(answer="答")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)
    generate_module.generate("这张图说的是什么？", [], [b"png"], mode="chart")
    return seen


# --- read prompt: the non-chart shape is explicit ------------------------- #


def test_read_prompt_names_the_non_chart_inputs_and_requires_empty_fields() -> None:
    prompt = chart_module.CHART_READ_SYSTEM

    for kind in ("纯文字页", "产品界面", "无关照片"):
        assert kind in prompt
    assert "market_state 一律写 unclear" in prompt
    assert "levels、focus、swings 全部返回空" in prompt
    assert "annotations" in prompt  # visible text/layout is still read
    assert "编造价位" in prompt


def test_is_non_chart_is_the_read_contract_not_just_missing_levels() -> None:
    assert is_non_chart(_non_chart().model_dump()) is True
    assert is_non_chart(_read().model_dump()) is False
    # an unclear chart that still shows swings/focus is a hard read, not a non-chart
    assert (
        is_non_chart(
            _read(market_state="unclear", levels=[]).model_dump()
        )
        is False
    )
    assert is_non_chart(None) is False


# --- answer prompt: refusal first, visible-text summary, no level list ---- #


def test_non_chart_answer_prompt_opens_with_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _compose(monkeypatch, _non_chart())

    assert "这不是行情图表" in seen["system"]
    assert "可见的文字" in seen["system"]
    assert "第一句" in seen["system"]
    assert "不写「关键位」" in seen["system"]
    assert "资料依据" in seen["system"]  # only to forbid the essay, not to request it
    # the read block carries the visible text and offers no level list
    assert "第 3 章 市场结构" in seen["user"]
    assert not [
        line
        for line in seen["user"].splitlines()
        if line.startswith("- ") and ". " in line
    ]


def test_genuine_chart_prompt_never_carries_the_refusal_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _compose(monkeypatch, _read())

    assert "这不是行情图表" not in seen["system"]
    assert "7519.75" in seen["user"]


# --- retrieval: at most one evidence unit, only for a non-chart read ------ #


def _unit(index: int) -> dict[str, Any]:
    return {
        "evidence_id": f"e{index}",
        "doc_id": "d1",
        "text": f"textbook paragraph {index}",
        "provenance": "native_text",
    }


def _retrieve(
    monkeypatch: pytest.MonkeyPatch, *, intent: str, chart_read: dict[str, Any] | None
) -> dict[str, Any]:
    def fake_retrieve(query: str, queries: list[str], **_kwargs: Any) -> RetrievalResult:
        units = [_unit(i) for i in range(4)]
        return RetrievalResult(
            text_hits=units,
            citable_units=[dict(u) for u in units],
            citation_index={u["evidence_id"]: i for i, u in enumerate(units)},
            acc={},
            graph_stats={},
        )

    monkeypatch.setattr(graph_module, "retrieve_evidence", fake_retrieve)
    state: dict[str, Any] = {
        "query": "这张图说的是什么？",
        "plan": QueryPlan(
            intent=intent,
            routes=["hybrid_text"],
            evidence_needs=["context"],
            sub_queries=[],
            max_search_rounds=3,
        ),
        "intent": intent,
        "chart_read": chart_read,
    }
    return graph_module._retrieve_node(state)


def test_non_chart_read_keeps_one_evidence_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _retrieve(monkeypatch, intent="chart_analysis", chart_read=_non_chart().model_dump())

    assert [u["evidence_id"] for u in state["citable_units"]] == ["e0"]
    assert state["citation_index"] == {"e0": 0}


def test_genuine_chart_read_keeps_every_evidence_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _retrieve(monkeypatch, intent="chart_analysis", chart_read=_read().model_dump())

    assert [u["evidence_id"] for u in state["citable_units"]] == ["e0", "e1", "e2", "e3"]
    assert state["citation_index"] == {"e0": 0, "e1": 1, "e2": 2, "e3": 3}


def test_text_question_retrieval_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _retrieve(monkeypatch, intent="knowledge_qa", chart_read=None)

    assert [u["evidence_id"] for u in state["citable_units"]] == ["e0", "e1", "e2", "e3"]
    assert state["citation_index"] == {"e0": 0, "e1": 1, "e2": 2, "e3": 3}
