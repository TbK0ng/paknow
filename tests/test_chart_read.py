"""Structured chart read: one uncitable read call feeds the answer and the API."""
from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest
from fastapi import UploadFile
from PIL import Image

from apps.api import main
from paknow import chart as chart_module
from paknow.chart import ChartRead, read_chart
from paknow.workflows import generate as generate_module
from paknow.workflows import graph as graph_module


def _png(size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 20, 20)).save(buf, "PNG")
    return buf.getvalue()


def _read(**overrides: Any) -> ChartRead:
    fields: dict[str, Any] = {
        "instrument": None,
        "timeframe": None,
        "market_state": "trading_range",
        "swings": [],
        "levels": [],
        "focus": [],
        "patterns": [],
        "annotations": ["2 @ 7519.75"],
        "indicators": [],
        "unreadable": [],
    }
    fields.update(overrides)
    return ChartRead(**fields)


class FakeClient:
    def __init__(self, out: ChartRead | None = None, error: Exception | None = None):
        self.out = out
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, schema, messages, **kwargs):
        self.calls.append({"schema": schema, "messages": messages, "kwargs": kwargs})
        if self.error:
            raise self.error
        assert self.out is not None
        return self.out


def _system_text(call: dict[str, Any]) -> str:
    return "\n".join(m["content"] for m in call["messages"])


# --- read_chart: the read prompt is structural and cannot cite ------------ #


def test_read_prompt_declares_the_actual_strip_count() -> None:
    client = FakeClient(out=_read())

    result, failure = read_chart([_png((400, 4000))], client=client)

    assert failure is None and isinstance(result, ChartRead)
    assert len(client.calls) == 1
    prompt = _system_text(client.calls[0])
    assert "共 3 张" in prompt
    assert "条带 1..3" in prompt


def test_single_strip_is_declared_as_strip_one() -> None:
    client = FakeClient(out=_read())

    read_chart([_png((800, 600))], client=client)

    prompt = _system_text(client.calls[0])
    assert "共 1 张" in prompt
    assert "strip 一律为 1" in prompt


def test_read_prompt_carries_no_evidence_blocks() -> None:
    client = FakeClient(out=_read())

    read_chart([_png((800, 600))], client=client)

    prompt = _system_text(client.calls[0])
    assert "证据（引用编号" not in prompt
    assert "图谱扩展" not in prompt
    assert "[0]" not in prompt
    assert "资料依据" not in prompt


def test_read_call_uses_one_attempt_under_the_120s_budget() -> None:
    client = FakeClient(out=_read())

    read_chart([_png((800, 600))], client=client)

    kwargs = client.calls[0]["kwargs"]
    assert kwargs["timeout"] == 120
    assert kwargs["attempts"] == 1
    assert kwargs["images"]
    # reasoning tokens share max_tokens; 4096 truncated every dense page
    assert kwargs["max_tokens"] == 8192


# --- read_chart: failures are classified, never raised -------------------- #


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("slow"), "timeout"),
        (json.JSONDecodeError("bad", "{}", 0), "invalid_structure"),
        (ValueError("schema"), "invalid_structure"),
    ],
)
def test_read_failure_is_classified_not_raised(error: Exception, reason: str) -> None:
    client = FakeClient(error=error)

    result, failure = read_chart([_png((800, 600))], client=client)

    assert result is None
    assert failure is not None and failure["reason"] == reason
    assert failure["detail"]


def test_read_without_images_is_no_content() -> None:
    result, failure = read_chart([], client=FakeClient(out=_read()))

    assert result is None
    assert failure is not None and failure["reason"] == "no_content"


# --- generate: renders the read the graph already made --------------------- #


def test_chart_generate_renders_the_structured_read(monkeypatch: pytest.MonkeyPatch) -> None:
    read = _read()
    seen: dict[str, Any] = {}

    def fake_answer(schema, messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return generate_module.Answer(answer="图上 7519.75 有标注。")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)

    out = generate_module.generate(
        "哪些价位关键？", [], [_png((800, 600))], mode="chart", chart_read=read.model_dump()
    )

    assert out["chart_read"] == read.model_dump()
    assert out["chart_read_status"] == "structured"
    assert "7519.75" in seen["user"]  # the answer is written from the read


def test_chart_generate_degrades_when_there_is_no_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generate_module.gateway,
        "chat_json",
        lambda schema, messages, **kwargs: generate_module.Answer(answer="降级回答"),
    )

    out = generate_module.generate("哪些价位关键？", [], [_png((800, 600))], mode="chart")

    assert out["chart_read"] is None
    assert out["chart_read_status"] == "unstructured"
    assert out["answer"] == "降级回答"


def test_text_generate_renders_no_chart_read(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_answer(schema, messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return generate_module.Answer(answer="答")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)

    out = generate_module.generate("双底是什么？", [], mode="extractive")

    assert out["chart_read"] is None
    assert out["chart_read_status"] == "unstructured"
    assert "图上读数" not in seen["user"]


# --- run_query: the read lands in the run output --------------------------- #


def test_run_query_surfaces_the_chart_read(monkeypatch: pytest.MonkeyPatch) -> None:
    read = _read().model_dump()

    class FakeGraph:
        def invoke(self, _state):
            return {
                "run_id": "r1",
                "answer": {
                    "answer": "答",
                    "chart_read": read,
                    "chart_read_status": "structured",
                },
                "failures": [],
            }

    monkeypatch.setattr(graph_module, "_graph", FakeGraph())

    result = graph_module.run_query("哪些价位关键？", images=[b"png"])

    assert result["chart_read"] == read
    assert result["chart_read_status"] == "structured"


# --- API: /charts exposes the read, /query keeps the text shape ------------ #


def _upload() -> UploadFile:
    return UploadFile(file=io.BytesIO(_png((800, 600))), filename="chart.png")


def test_charts_endpoint_exposes_the_chart_read(monkeypatch: pytest.MonkeyPatch) -> None:
    read = _read().model_dump()
    monkeypatch.setattr(
        main,
        "run_query",
        lambda q, images=None: {
            "answer": {"answer": "答"},
            "chart_read": read,
            "chart_read_status": "structured",
        },
    )

    payload = asyncio.run(main.charts("哪些价位关键？", _upload()))

    assert payload["chart_read"] == read
    assert payload["chart_read_status"] == "structured"


def test_charts_endpoint_reports_an_unstructured_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main,
        "run_query",
        lambda q, images=None: {
            "answer": {"answer": "降级回答"},
            "chart_read": None,
            "chart_read_status": "unstructured",
        },
    )

    payload = asyncio.run(main.charts("哪些价位关键？", _upload()))

    assert payload["chart_read"] is None
    assert payload["chart_read_status"] == "unstructured"


def test_text_query_endpoint_has_no_chart_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main,
        "run_query",
        lambda q: {"answer": {"answer": "答"}, "chart_read": None, "chart_read_status": "unstructured"},
    )

    payload = main.query(main.QueryRequest(query="双底是什么？"))

    assert "chart_read" not in payload
    assert "chart_read_status" not in payload


def test_chart_module_exports_the_contract() -> None:
    for name in ("ChartRead", "Swing", "Level", "Focus", "read_chart", "CHART_READ_SYSTEM"):
        assert hasattr(chart_module, name)
