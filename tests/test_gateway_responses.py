"""The default Responses transport is verified without a provider or credentials."""
import json

import httpx
import pytest
from openai import OpenAI
from pydantic import BaseModel

from paknow import gateway as gateway_module
from paknow.config import Settings
from paknow.gateway import ModelOutputError, failure_reason


class Answer(BaseModel):
    answer: str


def _delta(text):
    return {"type": "response.output_text.delta", "delta": text, "item_id": "msg-1",
            "output_index": 0, "content_index": 0, "sequence_number": 1}


def _terminal(status, *, reason=None, text=None):
    output = [] if text is None else [{"id": "msg-1", "type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": text}]}]
    return {"type": f"response.{status}", "sequence_number": 2,
            "response": {"id": "test-response", "object": "response", "status": status, "output": output,
                         "incomplete_details": {"reason": reason} if reason else None}}


def _ask(gw):
    return gw.chat_json(Answer, [{"role": "system", "content": "Explain the candidate."},
                                 {"role": "user", "content": "Frozen evidence"}],
                        attempts=1, timeout=150, max_tokens=24_576)


@pytest.fixture
def responses(monkeypatch):
    monkeypatch.setattr(gateway_module, "settings", Settings(_env_file=None, pa_cloud_provider="terra",
                        terra_base_url="https://terra.test/v1", terra_api_key="test-key", terra_model="gpt-6.1-terra",
                        terra_effort="medium"))
    clients = []

    def mock(*events):
        calls = []

        def handle(request):
            calls.append((request, json.loads(request.content)))
            return httpx.Response(200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                                  headers={"content-type": "text/event-stream"})

        def sdk(**kwargs):
            assert kwargs == {"max_retries": 0}
            client = OpenAI(api_key="test-key", base_url="https://terra.test/v1", **kwargs,
                            http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            clients.append(client)
            return client

        gw = gateway_module.ModelGateway()
        monkeypatch.setattr(gw, "_new_client", sdk)
        return gw, calls

    yield mock
    for client in clients:
        client.close()


def test_default_terra_sends_one_schema_guided_responses_request(responses):
    gw, calls = responses(_delta('{"answer":'), _delta('"valid"}'), _terminal("completed"))
    assert _ask(gw).answer == "valid"
    (request, payload), = calls
    assert str(request.url) == "https://terra.test/v1/responses"
    assert payload["model"] == "gpt-6.1-terra"
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_output_tokens"] == 24_576 and payload["stream"] and not payload["store"]
    assert payload["text"] == {"format": {"type": "json_object"}}
    instructions, user = payload["input"]
    assert instructions["role"] == "developer" and instructions["content"].startswith("Explain the candidate.")
    assert "Schema:" in instructions["content"] and '"answer"' in instructions["content"]
    assert user == {"role": "user", "content": "Frozen evidence"}
    assert not {"temperature", "messages", "reasoning_effort", "max_tokens"} & payload.keys()


def test_terminal_text_preferred_over_deltas(responses):
    gw, calls = responses(_delta('{"answer":"fragment"}'), _terminal("completed", text='{"answer":"final"}'))
    assert _ask(gw).answer == "final"
    assert len(calls) == 1


def test_incomplete_stream_never_accepts_even_valid_json(responses):
    gw, calls = responses(_delta('{"answer":"valid but truncated"}'),
                          _terminal("incomplete", reason="max_output_tokens"))
    with pytest.raises(ModelOutputError) as caught:
        _ask(gw)
    assert caught.value.finish_reason == "length"
    assert len(calls) == 1


@pytest.mark.parametrize("terminal", [(), (_terminal("failed"),)])
def test_missing_or_failed_terminal_does_not_produce_a_verdict(responses, terminal):
    gw, calls = responses(_delta('{"answer":"partial"}'), *terminal)
    with pytest.raises(RuntimeError, match="did not complete"):
        _ask(gw)
    assert len(calls) == 1


def test_streaming_transport_exceptions_use_existing_failure_states():
    assert failure_reason(httpx.ReadTimeout("late")) == "timeout"
    assert failure_reason(httpx.RemoteProtocolError("disconnected")) == "connection"
