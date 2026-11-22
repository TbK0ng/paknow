"""OpenAI-compatible structured text generation for PAknow."""
from __future__ import annotations

import json
import time
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from httpx import TimeoutException, TransportError
from openai import APIConnectionError, APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError

from paknow.config import settings

_SNIPPET = 120  # raw chars kept for the failure record; enough to recognise the reply


class ModelOutputError(ValueError):
    """A model reply that no JSON parse could turn into the schema object.

    ``reason`` speaks the chart read's vocabulary (``invalid_structure`` /
    ``no_content``) so a degraded answer is classified the same way everywhere.
    """

    def __init__(self, raw: str, detail: str) -> None:
        self.finish_reason = "unknown"
        self.content_chars = len(raw)
        self.snippet = raw[:_SNIPPET]
        self.reason = "no_content" if not raw.strip() else "invalid_structure"
        super().__init__(f"{detail}: {self.snippet!r}")


def failure_reason(exc: Exception) -> str:
    """Map SDK and streaming transport failures to the chart vocabulary."""
    if isinstance(exc, ModelOutputError):
        return exc.reason
    if isinstance(exc, (TimeoutError, TimeoutException, APITimeoutError)):
        return "timeout"
    if isinstance(exc, (TransportError, APIConnectionError)):
        return "connection"
    if isinstance(exc, (ValidationError, ValueError)):
        return "invalid_structure"
    return "no_content"


_JSON = json.JSONDecoder(strict=False)


def _first_json_object(raw: str) -> Any:
    """The first complete object in ``raw``; anything after it is ignored."""
    start = raw.find("{")
    if start < 0:
        raise ModelOutputError(raw, "no JSON object in reply")
    try:
        value, _end = _JSON.raw_decode(raw, start)
    except json.JSONDecodeError:
        raise ModelOutputError(raw, "reply is not valid JSON") from None
    return value


class ModelGateway:
    def __init__(self) -> None:
        self.profile = settings.cloud_profile
        self.model = self.profile.model
        self.effort = self.profile.effort
        host = urlsplit(self.profile.base_url).hostname
        self.effort_field = "reasoning_effort" if host in {"micuapi.ai", "www.micuapi.ai"} else "effort"
        self._client: OpenAI | None = None
        self._no_retry: OpenAI | None = None
        self._client_lock = Lock()

    def _new_client(self, **kwargs: Any) -> OpenAI:
        return OpenAI(
            api_key=self.profile.api_key,
            base_url=self.profile.base_url,
            **kwargs,
        )

    @property
    def client(self) -> OpenAI:
        """Lazily construct the SDK client so importing without a key is safe."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._new_client()
        return self._client

    @property
    def no_retry_client(self) -> OpenAI:
        """SDK retries off: one attempt must cost one timeout, not three."""
        if self._no_retry is None:
            with self._client_lock:
                if self._no_retry is None:
                    self._no_retry = self._new_client(max_retries=0)
        return self._no_retry

    def _create(self, *, attempts: int = 2, **kwargs: Any) -> Any:
        """Completion call with an optional retry budget (default: one retry).

        ``attempts=1`` uses a client with the SDK's internal retries disabled,
        so a 120s timeout cannot be paid three times over.
        """
        client = self.no_retry_client if attempts == 1 else self.client
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                if self.profile.provider == "terra":
                    return self._responses_text(client, **kwargs)
                return client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt + 1 < attempts:
                    time.sleep(1.5)
        raise last  # type: ignore[misc]

    @staticmethod
    def _responses_text(client: OpenAI, **kwargs: Any) -> tuple[str, str]:
        """Read the full Responses stream before accepting any structured result."""
        timeout = kwargs.get("timeout")
        deadline = time.monotonic() + timeout if timeout is not None else None
        final = None
        chunks: list[str] = []
        with client.responses.create(**kwargs) as stream:
            for event in stream:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Responses request exceeded its time budget")
                if event.type == "response.output_text.delta":
                    chunks.append(event.delta)
                elif event.type in {"response.completed", "response.incomplete", "response.failed"}:
                    final = event.response
                    break
                elif event.type == "error":
                    raise RuntimeError("Responses stream reported an error")
        if final is None or final.status == "failed":
            raise RuntimeError("Responses stream did not complete")
        raw = final.output_text or "".join(chunks)
        if final.status != "completed":
            error = ModelOutputError(raw, "Responses stream was incomplete")
            reason = getattr(getattr(final, "incomplete_details", None), "reason", None)
            if reason == "max_output_tokens":
                error.finish_reason = "length"
            elif reason == "content_filter":
                error.finish_reason = "content_filter"
            raise error
        return raw, "stop"

    def chat_json(
        self,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float | None = None,
        attempts: int = 2,
    ) -> BaseModel:
        """Structured output validated against a Pydantic schema."""
        msgs = list(messages)
        msgs.append(
            {
                "role": "system",
                "content": (
                    "Respond with a single JSON object only, no prose, no markdown "
                    "fences. Schema:\n"
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                ),
            }
        )
        if self.profile.provider == "terra":
            # Fold system instructions before the single user payload, as on Pi's
            # Responses route. Keep schema-guided JSON plus local validation.
            instructions = "\n\n".join(msg["content"] for msg in msgs if msg["role"] == "system")
            kwargs: dict[str, Any] = dict(
                model=self.model,
                input=[{"role": "developer", "content": instructions},
                       *(msg for msg in msgs if msg["role"] != "system")],
                max_output_tokens=max_tokens,
                text={"format": {"type": "json_object"}},
                store=False,
                stream=True,
            )
            if self.effort:
                kwargs["reasoning"] = {"effort": self.effort}
        else:
            kwargs = dict(model=self.model, messages=msgs, max_tokens=max_tokens,
                          temperature=temperature, response_format={"type": "json_object"})
            if self.effort:
                kwargs["extra_body"] = {self.effort_field: self.effort}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._create(attempts=attempts, **kwargs)
        if self.profile.provider == "terra":
            raw, finish_reason = resp
        else:
            if not resp.choices:
                raise ModelOutputError("", "completion returned no choices")
            choice = resp.choices[0]
            raw = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
        # models emit bare newlines/tabs inside strings and a second object
        # after the first; tolerate both before giving up on the reply
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(raw, strict=False)
            except json.JSONDecodeError:
                try:
                    data = _first_json_object(raw)
                except ModelOutputError as exc:
                    if finish_reason in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
                        exc.finish_reason = finish_reason
                    raise
        return schema.model_validate(data)


gateway = ModelGateway()
