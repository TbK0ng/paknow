"""Dialogue support in the local viewer API: rewrite, fallback, graph payload."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image

from apps.api import main
from paknow.retrieval.qdrant_store import StoreLockedError


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


def test_degraded_answer_is_marked_in_the_response(monkeypatch):
    degradation = {"reason": "invalid_structure", "detail": "JSONDecodeError: bad"}
    monkeypatch.setattr(
        main,
        "run_query",
        lambda q: {
            "answer": {
                "answer": "本次生成失败：模型输出被截断或结构非法，没有可展示的答案。请重试本问题。",
                "evidence_ids": [],
                "degraded": True,
                "degradation": degradation,
            }
        },
    )

    payload = main.query(main.QueryRequest(query="双底是什么？"))

    assert payload["degraded"] is True
    assert payload["degradation"] == degradation
    assert "本次生成失败" in payload["answer"]


def test_a_short_real_answer_is_not_flagged(monkeypatch):
    monkeypatch.setattr(main, "run_query", lambda q: {"answer": {"answer": "资料未说明"}})

    payload = main.query(main.QueryRequest(query="双底是什么？"))

    assert payload["degraded"] is False
    assert payload["degradation"] is None


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


def test_store_lock_conflict_returns_503_with_a_readable_hint(monkeypatch):
    def locked(_query, **_kwargs):
        raise StoreLockedError(Path("data/objects/qdrant.lock"), "pid=4242, cmd=eval.py")

    monkeypatch.setattr(main, "run_query", locked)

    response = TestClient(main.app).post("/query", json={"query": "双底是什么？"})

    assert response.status_code == 503
    assert "稍后重试" in response.json()["detail"]
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# optional debug block
# ---------------------------------------------------------------------------

_QUERY_KEYS = {
    "run_id",
    "intent",
    "is_framework",
    "routes",
    "answer",
    "uncertainty",
    "citations",
    "figures",
    "rounds",
    "graph_stats",
    "graph",
    "degraded",
    "degradation",
    "query_used",
    "rewritten",
}


def _full_result() -> dict:
    return {
        "run_id": "r1",
        "intent": "knowledge_qa",
        "is_framework": False,
        "routes": ["hybrid_text"],
        "answer": {
            "answer": "答",
            "uncertainty": [],
            "evidence_ids": [],
            # the generate failure row must be classified from this marker
            "degraded": True,
            "degradation": {"reason": "invalid_structure", "detail": "JSONDecodeError: bad"},
        },
        "figures": [],
        "rounds": 2,
        "graph_stats": {"enabled": True, "selected": 1},
        "graph_evidence": [{"evidence_id": "g1", "from_label": "双底", "text": "x"}],
        "hits": [
            {"evidence_id": "e1", "score": 0.8, "block_type": "paragraph", "text": "长" * 800},
            {"evidence_id": "e2", "score": 0.4, "block_type": "caption", "text": "短"},
        ],
        "citable_ids": ["e1", "e2"],
        "context_unit_counts": {"total": 3, "parents": 1, "adjacent": 2},
        "sufficiency": {
            "sufficient": False,
            "missing": ["定义"],
            "conflicting": [],
            "next_query": "双底 定义",
            "reason": "缺定义",
        },
        "failures": [
            "retrieve: RuntimeError: index offline",
            "chart_read: timeout (page render timed out)",
            "generate: invalid_structure: JSONDecodeError: bad",
        ],
    }


def test_the_debug_block_is_opt_in_and_leaves_the_other_keys_alone(monkeypatch):
    monkeypatch.setattr(main, "run_query", lambda q: _full_result())

    plain = main.query(main.QueryRequest(query="双底是什么？"))
    verbose = main.query(main.QueryRequest(query="双底是什么？", debug=True))

    assert "debug" not in plain
    assert set(plain) == _QUERY_KEYS
    # nothing else appears or disappears when the block is requested
    assert set(verbose) == _QUERY_KEYS | {"debug"}


def test_a_requested_debug_block_shows_the_chain_internals(monkeypatch):
    monkeypatch.setattr(main, "run_query", lambda q: _full_result())

    block = main.query(main.QueryRequest(query="双底是什么？", debug=True))["debug"]

    assert block["hits"] == [
        {"evidence_id": "e1", "score": 0.8, "block_type": "paragraph"},
        {"evidence_id": "e2", "score": 0.4, "block_type": "caption"},
    ]
    assert block["citable_ids"] == ["e1", "e2"]
    assert block["context_unit_counts"] == {"total": 3, "parents": 1, "adjacent": 2}
    assert block["sufficiency"]["sufficient"] is False
    assert block["sufficiency"]["missing"] == ["定义"]
    assert block["rounds"] == 2
    assert block["graph_stats"] == {"enabled": True, "selected": 1}
    assert block["failures"] == [
        {"stage": "retrieve", "category": "error", "detail": "RuntimeError: index offline"},
        {"stage": "chart_read", "category": "timeout", "detail": "page render timed out"},
        {"stage": "generate", "category": "invalid_structure", "detail": "JSONDecodeError: bad"},
    ]


def test_the_generate_failure_is_classified_from_the_degradation_marker():
    """The flattened trace string is not re-parsed for the category."""
    event = main._failure_event(
        "generate: no_content: ValueError: stale wording",
        {"reason": "timeout", "detail": "TimeoutError: slow"},
    )

    assert event == {
        "stage": "generate",
        "category": "timeout",
        "detail": "TimeoutError: slow",
    }


def test_a_degraded_run_is_visible_in_the_debug_failures(monkeypatch):
    result = _full_result()
    result["answer"] = {
        "answer": "本次生成失败：模型输出被截断或结构非法，没有可展示的答案。请重试本问题。",
        "evidence_ids": [],
        "degraded": True,
        "degradation": {"reason": "invalid_structure", "detail": "JSONDecodeError: bad"},
    }
    monkeypatch.setattr(main, "run_query", lambda q: result)

    payload = main.query(main.QueryRequest(query="双底是什么？", debug=True))

    assert payload["degraded"] is True
    assert {
        "stage": "generate",
        "category": "invalid_structure",
        "detail": "JSONDecodeError: bad",
    } in payload["debug"]["failures"]
