"""Chart questions answer from the image; text questions keep the evidence prompt."""
from __future__ import annotations

from typing import Any

import pytest

from paknow.workflows import graph as graph_module
from paknow.workflows import generate as generate_module
from paknow.workflows import router as router_module


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_generate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        seen["mode"] = kwargs.get("mode")
        return {"answer": "", "evidence_ids": [], "figure_ids": [], "uncertainty": []}

    monkeypatch.setattr(graph_module, "generate", fake_generate)
    return seen


def _state(intent: str, is_framework: bool = False) -> dict[str, Any]:
    return {
        "query": "这张图上哪些价位是关键位？",
        "intent": intent,
        "is_framework": is_framework,
        "citable_units": [],
        "graph_evidence": [],
        "figures": [],
    }


def test_chart_intent_uses_chart_mode(captured: dict[str, Any]) -> None:
    graph_module._generate_node(_state("chart_analysis"))

    assert captured["mode"] == "chart"


def test_knowledge_question_keeps_evidence_mode(captured: dict[str, Any]) -> None:
    graph_module._generate_node(_state("knowledge_qa"))

    assert captured["mode"] == "extractive"


def test_chart_mode_prompt_answers_from_the_image_first() -> None:
    chart = generate_module._CHART_SYSTEM

    assert "看不清" in chart
    assert "只依据提供的证据回答" not in chart
    assert "ADR-0002" in chart  # a read is never a citation


def test_router_fallback_keeps_the_picture_in_play(monkeypatch: pytest.MonkeyPatch) -> None:
    # a truncated/failed route must not turn an image question into a text one
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("finish_reason=length")

    monkeypatch.setattr(router_module.gateway, "chat_json", boom)

    with_image = router_module.route("这张图上哪些价位是关键位？", images=[b"png"])
    text_only = router_module.route("什么是紧密交易区间？")

    assert with_image.intent == "chart_analysis"
    assert "visual_page" in with_image.routes
    assert text_only.intent == "knowledge_qa"
