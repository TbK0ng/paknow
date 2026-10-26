"""``chat_json`` parses what models actually emit, and fails in a classified way."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from paknow import gateway as gateway_module


class Answer(BaseModel):
    answer: str


def _gateway(monkeypatch: pytest.MonkeyPatch, raw: str) -> gateway_module.ModelGateway:
    """A gateway whose completion call returns ``raw`` verbatim."""
    gw = gateway_module.ModelGateway()
    monkeypatch.setattr(
        gw,
        "_create",
        lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw))]
        ),
    )
    return gw


def _ask(gw: gateway_module.ModelGateway) -> Answer:
    return gw.chat_json(Answer, [{"role": "user", "content": "问"}])  # type: ignore[return-value]


def test_bare_control_characters_still_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = '{"answer": "第一行\n第二行\t结尾"}'

    assert _ask(_gateway(monkeypatch, raw)).answer == "第一行\n第二行\t结尾"


def test_two_concatenated_objects_yield_the_first_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"answer": "第一个"}{"answer": "第二个"}'

    assert _ask(_gateway(monkeypatch, raw)).answer == "第一个"


def test_truncated_body_raises_a_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = '{"answer": "' + "很长的答案" * 200
    gw = _gateway(monkeypatch, raw)

    with pytest.raises(gateway_module.ModelOutputError) as caught:
        _ask(gw)

    err = caught.value
    assert err.reason == "invalid_structure"
    assert err.snippet == raw[:120]
    assert raw[:120] in str(err)


def test_empty_content_is_no_content(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(gateway_module.ModelOutputError) as caught:
        _ask(_gateway(monkeypatch, ""))

    assert caught.value.reason == "no_content"


def test_missing_field_is_still_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        _ask(_gateway(monkeypatch, '{"other": 1}'))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("slow"), "timeout"),
        (ValueError("schema"), "invalid_structure"),
        (RuntimeError("upstream"), "no_content"),
    ],
)
def test_failure_reason_is_the_chart_read_vocabulary(
    error: Exception, reason: str
) -> None:
    assert gateway_module.failure_reason(error) == reason


def test_a_parse_error_keeps_its_own_classification() -> None:
    err = gateway_module.ModelOutputError("", "no JSON object in reply")

    assert gateway_module.failure_reason(err) == "no_content"


def test_a_classified_error_is_a_value_error_for_callers() -> None:
    # chart.read_chart already treats ValueError as a structural failure
    assert isinstance(gateway_module.ModelOutputError("x", "d"), ValueError)
