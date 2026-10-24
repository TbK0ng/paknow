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


# --- layered answer: full level list, converged suggestions, cited evidence -- #

_LEVELS = [
    {"price": 7519.75, "source": "annotation", "strip": 2, "basis": "「2 @ 7519.75」箭头指向该位"},
    {"price": 7499.25, "source": "swing", "strip": 2, "basis": "前高转折处"},
    {"price": None, "source": "axis", "strip": 1, "basis": "右侧刻度被裁掉"},
    {"price": 7489.15, "source": "unknown", "strip": 3, "basis": "横线，未见标注"},
]

def _read(levels: list[dict[str, Any]] | None = None) -> Any:
    from paknow.chart import ChartRead

    return ChartRead(
        instrument="ES",
        timeframe="5m",
        market_state="trading_range",
        swings=[],
        levels=_LEVELS if levels is None else levels,
        # one focus only: layer 1 must still list every read level
        focus=[{"price": 7519.75, "why": "图上标注"}],
        patterns=["trading range"],
        annotations=["2 @ 7519.75"],
        indicators=["均线（参数未知）"],
        unreadable=["7499.75 与 7499.25 冲突，保留更极端的 7499.25"],
    )


def _compose(
    monkeypatch: pytest.MonkeyPatch,
    *,
    levels: list[dict[str, Any]] | None = None,
    mode: str = "chart",
) -> tuple[str, str, str]:
    read = _read(levels)
    seen: dict[str, Any] = {}

    def fake_answer(schema: Any, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        seen["messages"] = messages
        return generate_module.Answer(answer="答")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)
    images = [b"png"] if mode == "chart" else None
    generate_module.generate(
        "这张图上哪些价位是关键位？", [], images, mode=mode, chart_read=read.model_dump()
    )
    system = seen["messages"][0]["content"]
    user = seen["messages"][1]["content"]
    block = ""
    if "图上读数（结构化" in user:
        block = user.split("图上读数（结构化")[1].split("证据（引用编号")[0]
    return system, user, block


def test_composed_chart_prompt_lists_every_read_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _system, _user, block = _compose(monkeypatch)

    level_lines = [line for line in block.splitlines() if line.startswith("- ") and ". " in line]
    assert len(level_lines) == len(_LEVELS)
    for level in _LEVELS:
        price = "看不清" if level["price"] is None else f"{level['price']:g}"
        assert f" {price} " in block
        assert level["basis"] in block
        assert f"条带 {level['strip']} " in block


def test_composed_chart_prompt_states_the_fixed_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, user, _block = _compose(monkeypatch)

    assert generate_module._CHART_PRIORITY in system
    # the read block repeats it next to the focus list
    assert generate_module._CHART_PRIORITY in user


def test_read_block_never_carries_a_citation_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _system, user, block = _compose(monkeypatch)

    assert "[n]" not in block
    assert "证据（引用编号 [n]）" in user  # 资料依据 stays its own block


def test_read_text_with_bracketed_numbers_never_reads_as_a_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a screenshot of a previous answer carries [n] in its pixels; the read
    # content must not hand those back as citations (ADR-0002)
    levels = [
        {
            "price": 7519.75,
            "source": "annotation",
            "strip": 1,
            "basis": "抄自图上旧回答：前高 [3]",
        }
    ]
    _system, _user, block = _compose(monkeypatch, levels=levels)

    assert "[3]" not in block
    assert "前高" in block


def test_composed_chart_prompt_is_the_layered_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # layers, priority, conflict rule and moving-average rule live in the constant
    system, _user, _block = _compose(monkeypatch)

    assert system == generate_module._CHART_SYSTEM


def test_chart_without_readable_levels_does_not_fabricate_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _system, _user, block = _compose(monkeypatch, levels=[])

    assert "不要编造价位清单" in block
    assert not [line for line in block.splitlines() if line.startswith("- ") and ". " in line]


def test_text_prompts_do_not_grow_a_chart_layer() -> None:
    for system in (generate_module._ANSWER_SYSTEM, generate_module._SYNTHESIS_SYSTEM):
        assert "建议关注位" not in system
        assert "可读位" not in system


def test_extractive_composition_keeps_no_chart_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _system, user, _block = _compose(monkeypatch, mode="extractive")

    assert "可读位" not in user
    assert "建议关注位" not in user
    assert "证据（引用编号 [n]）" in user


def test_a_supplied_read_is_used_without_reading_the_image_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_answer(schema: Any, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        seen["user"] = messages[-1]["content"]
        return generate_module.Answer(answer="答")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)
    out = generate_module.generate(
        "哪些价位关键？", [], [b"png"], mode="chart", chart_read=_read()
    )

    assert out["chart_read_status"] == "structured"
    assert "7519.75" in seen["user"]
    # generate is never a reader: the graph's read node owns that call
    assert not hasattr(generate_module, "read_chart")


def test_a_failed_read_passed_in_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_answer(schema: Any, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        seen["system"] = messages[0]["content"]
        return generate_module.Answer(answer="降级回答")

    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_answer)

    out = generate_module.generate("哪些价位关键？", [], [b"png"], mode="chart", chart_read=None)

    assert out["chart_read"] is None
    assert out["chart_read_status"] == "unstructured"
    # no structured read: the single-stage prompt reads the picture itself
    assert seen["system"] == generate_module._CHART_FALLBACK_SYSTEM
