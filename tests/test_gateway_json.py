"""Structured-output parsing: what the gateway accepts and what it refuses."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from paknow.gateway import ModelGateway


class _Answer(BaseModel):
    answer: str


def _gateway(monkeypatch, content: str) -> ModelGateway:
    gw = ModelGateway()
    monkeypatch.setattr(
        gw,
        "_create",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        ),
    )
    return gw


def test_raw_control_characters_inside_strings_still_parse(monkeypatch):
    # observed in production: the answer JSON contained a literal newline
    gw = _gateway(monkeypatch, '{"answer": "第一行\n第二行"}')

    out = gw.chat_json(_Answer, [{"role": "user", "content": "q"}])

    assert out.answer == "第一行\n第二行"


def test_broken_structure_is_still_rejected(monkeypatch):
    gw = _gateway(monkeypatch, '{"answer": "truncated')

    with pytest.raises(ValueError):
        gw.chat_json(_Answer, [{"role": "user", "content": "q"}])


def test_missing_field_fails_schema_validation(monkeypatch):
    gw = _gateway(monkeypatch, '{"other": 1}')

    with pytest.raises(ValueError):
        gw.chat_json(_Answer, [{"role": "user", "content": "q"}])
