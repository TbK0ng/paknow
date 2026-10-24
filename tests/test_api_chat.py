"""Dialogue support in the local viewer API: rewrite, fallback, graph payload."""

from __future__ import annotations

import asyncio
import io

from fastapi import UploadFile
from PIL import Image

from apps.api import main


class FakeGateway:
    def __init__(self, *, query: str | None = None, error: Exception | None = None):
        self.query = query
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def chat_json(self, schema, messages, **kwargs):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return schema(query=self.query)


def test_no_history_skips_the_rewrite():
    assert main._standalone_query("双底是什么？", []) == ("双底是什么？", False)


def test_follow_up_is_rewritten_with_the_transcript(monkeypatch):
    fake = FakeGateway(query="双底失败之后通常会发生什么？")
    monkeypatch.setattr(main, "gateway", fake)
    history = [
        main.ChatTurn(role="user", content="双底失败有哪些变体？"),
        main.ChatTurn(role="assistant", content="通常有三种变体。"),
    ]

    standalone, rewritten = main._standalone_query("那之后呢？", history)

    assert standalone == "双底失败之后通常会发生什么？"
    assert rewritten is True
    assert "双底失败有哪些变体" in fake.calls[0][1]["content"]


def test_rewrite_failure_falls_back_to_the_raw_question(monkeypatch):
    monkeypatch.setattr(main, "gateway", FakeGateway(error=RuntimeError("boom")))
    history = [main.ChatTurn(role="user", content="前一个问题")]

    assert main._standalone_query("那它呢？", history) == ("那它呢？", False)


def test_identical_rewrite_is_not_flagged(monkeypatch):
    monkeypatch.setattr(main, "gateway", FakeGateway(query="那它呢？"))
    history = [main.ChatTurn(role="user", content="前一个问题")]

    assert main._standalone_query("那它呢？", history) == ("那它呢？", False)


def test_query_endpoint_uses_the_rewritten_question(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_query(query: str) -> dict:
        captured["query"] = query
        return {
            "answer": {"answer": "答", "evidence_ids": []},
            "rounds": 2,
            "graph_stats": {
                "enabled": True,
                "selected": 1,
                "candidates": 3,
                "seeds": 1,
                "tokens": 42,
            },
            "graph_evidence": [
                {
                    "evidence_id": "e1",
                    "relation": "PART_OF",
                    "from_label": "双底",
                    "text": "x" * 400,
                    "seen_index": None,
                }
            ],
        }

    monkeypatch.setattr(main, "run_query", fake_run_query)
    monkeypatch.setattr(main, "gateway", FakeGateway(query="独立问题"))

    payload = main.query(
        main.QueryRequest(
            query="那之后呢？", history=[{"role": "user", "content": "前文"}]
        )
    )

    assert captured["query"] == "独立问题"
    assert payload["query_used"] == "独立问题"
    assert payload["rewritten"] is True
    assert payload["rounds"] == 2
    assert payload["graph_stats"]["selected"] == 1
    assert payload["graph"][0]["from_label"] == "双底"
    assert len(payload["graph"][0]["text"]) == 240


def test_graph_disabled_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(main, "run_query", lambda q: {"answer": {"answer": "答"}})

    payload = main.query(main.QueryRequest(query="问题"))

    assert payload["graph_stats"] == {}
    assert payload["graph"] == []
    assert payload["rewritten"] is False


def test_only_the_charts_shape_carries_chart_read(monkeypatch):
    read = {
        "market_state": "trend_up",
        "levels": [
            {"price": 7526.75, "source": "annotation", "strip": 1, "basis": "箭头旁 2 @ 7526.75"},
            {"price": None, "source": "unknown", "strip": 2, "basis": "左上角数字被裁切"},
        ],
        "unreadable": ["周期未标注"],
    }
    monkeypatch.setattr(
        main,
        "run_query",
        lambda q, images=None: {
            "answer": {"answer": "答"},
            "chart_read": read,
            "chart_read_status": "structured",
        },
    )
    buf = io.BytesIO()
    Image.new("RGB", (800, 600)).save(buf, "PNG")
    upload = UploadFile(file=io.BytesIO(buf.getvalue()), filename="chart.png")

    chart_payload = asyncio.run(main.charts("哪些价位关键？", upload))
    text_payload = main.query(main.QueryRequest(query="双底是什么？"))

    assert chart_payload["chart_read"] == read
    assert "chart_read" not in text_payload
