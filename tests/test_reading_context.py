"""Chunk parents are reading context: shown to the model, never a citation."""
from __future__ import annotations

from typing import Any

import pytest

from paknow.workflows import generate as generate_module
from paknow.workflows import graph as graph_module


def _unit(eid: str, *, text: str = "整段原文", parent: bool = False) -> dict[str, Any]:
    return {
        "evidence_id": eid,
        "chapter_path": ["第 1 章"],
        "pdf_page": 7,
        "text": text,
        "is_parent": parent,
        "is_adjacent": False,
        "provenance": "native_text",
    }


def test_parent_is_rendered_without_a_citation_number():
    lines = generate_module.render_reading_context(
        [_unit("p1", parent=True)], bound={"e1": 0}
    )

    assert lines == ["- 第 1 章 · PDF第7页 整段原文"]
    assert all("[0]" not in line and "[1]" not in line for line in lines)


def test_parent_already_numbered_is_not_repeated():
    assert generate_module.render_reading_context(
        [_unit("p1", parent=True)], bound={"p1": 3}
    ) == []


def test_generate_node_passes_only_parents_as_reading_context(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_generate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"answer": "", "evidence_ids": [], "figure_ids": [], "uncertainty": []}

    monkeypatch.setattr(graph_module, "generate", fake_generate)

    graph_module._generate_node(
        {
            "query": "q",
            "intent": "knowledge_qa",
            "citable_units": [_unit("e1")],
            "context_units": [_unit("p1", parent=True), _unit("n1")],
        }
    )

    assert [u["evidence_id"] for u in seen["reading_context"]] == ["p1"]


def test_citable_units_exclude_parents_but_keep_adjacent(monkeypatch):
    from paknow.retrieval import pipeline

    units = pipeline.citable_units(
        [_unit("e1")], [_unit("p1", parent=True), _unit("n1")]
    )

    assert [u["evidence_id"] for u in units] == ["e1", "n1"]
    assert pipeline.citation_binding(units) == {"e1": 0, "n1": 1}


def test_prompt_carries_the_reading_context_block():
    captured: dict[str, Any] = {}

    def fake_chat_json(schema, messages, **kwargs):
        captured["messages"] = messages
        return schema(answer="ok", cited_indices=[0])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(generate_module.gateway, "chat_json", fake_chat_json)
    try:
        generate_module.generate(
            "q",
            [_unit("e1", text="片段")],
            reading_context=[_unit("p1", parent=True)],
        )
    finally:
        monkeypatch.undo()

    prompt = captured["messages"][-1]["content"]
    assert "整段上下文" in prompt
    assert "不可引用" in prompt
    assert "整段原文" in prompt
    assert "[0] " in prompt  # the citable hit is still numbered
