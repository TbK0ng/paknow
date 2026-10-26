"""Unified cloud model gateway (plan §11.1, §11.2).

Single entry point for answer generation, routing, extraction and chart
observation. DeepSeek is OpenAI-compatible; the client stays swappable so a
local model can be substituted without touching call sites.
"""
from __future__ import annotations

import base64
import io
import json
import time
from typing import Any

from openai import OpenAI
from PIL import Image
from pydantic import BaseModel, ValidationError

from paknow.config import settings

_MAX_SIDE = 1568  # vision encoder keeps this much detail; larger is waste
_MAX_TILES = 8  # strips per upload, so one image cannot flood the prompt
_JPEG_QUALITY = 90
_SNIPPET = 120  # raw chars kept for the failure record; enough to recognise the reply


class ModelOutputError(ValueError):
    """A model reply that no JSON parse could turn into the schema object.

    ``reason`` speaks the chart read's vocabulary (``invalid_structure`` /
    ``no_content``) so a degraded answer is classified the same way everywhere.
    """

    def __init__(self, raw: str, detail: str) -> None:
        self.snippet = raw[:_SNIPPET]
        self.reason = "no_content" if not raw.strip() else "invalid_structure"
        super().__init__(f"{detail}: {self.snippet!r}")


def failure_reason(exc: Exception) -> str:
    """The one failure vocabulary: ``timeout`` / ``invalid_structure`` / ``no_content``."""
    if isinstance(exc, ModelOutputError):
        return exc.reason
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError":
        return "timeout"
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


def _strip_step(height: int) -> int:
    return max(_MAX_SIDE, -(-height // _MAX_TILES))  # ceil, so no strip is dropped


def strip_count(data: bytes) -> int:
    """Strips :func:`_prepare_image` would cut from this upload, without encoding."""
    _w, h = Image.open(io.BytesIO(data)).size
    return -(-h // _strip_step(h))


def _prepare_image(data: bytes) -> list[str]:
    """One upload -> the data URIs the model should see.

    Phone screenshots run thousands of pixels tall; downscaling one to a single
    encoder frame erases the price labels, so tall images are cut into
    full-resolution strips instead. Everything is re-encoded as JPEG: a raw
    RGBA PNG of the same chart exceeds the provider's 32 MB per-image limit.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    step = _strip_step(h)
    uris: list[str] = []
    for y in range(0, h, step):
        strip = img.crop((0, y, w, min(h, y + step)))
        if max(strip.size) > _MAX_SIDE:
            strip.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        strip.save(buf, "JPEG", quality=_JPEG_QUALITY)
        uris.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
    return uris


class ModelGateway:
    def __init__(self) -> None:
        self.model = settings.deepseek_model
        self.effort = settings.deepseek_effort
        self.vision = settings.deepseek_vision
        self._client: OpenAI | None = None
        self._no_retry: OpenAI | None = None

    def _new_client(self, **kwargs: Any) -> OpenAI:
        return OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            **kwargs,
        )

    @property
    def client(self) -> OpenAI:
        """Lazily construct the SDK client so importing without a key is safe."""
        if self._client is None:
            self._client = self._new_client()
        return self._client

    @property
    def no_retry_client(self) -> OpenAI:
        """SDK retries off: one attempt must cost one timeout, not three."""
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
                return client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt == 0:
                    time.sleep(1.5)
        raise last  # type: ignore[misc]

    def chat(
        self,
        messages: list[dict[str, str]],
        images: list[bytes] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> str:
        """Text (and optionally image) chat. Returns assistant content."""
        msgs = self._attach_images(messages, images)
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if self.effort:
            kwargs["extra_body"] = {"effort": self.effort}
        resp = self._create(**kwargs)
        return resp.choices[0].message.content or ""

    def chat_json(
        self,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        images: list[bytes] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float | None = None,
        attempts: int = 2,
    ) -> BaseModel:
        """Structured output validated against a Pydantic schema."""
        msgs = self._attach_images(messages, images)
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
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        if self.effort:
            kwargs["extra_body"] = {"effort": self.effort}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._create(attempts=attempts, **kwargs)
        raw = resp.choices[0].message.content or ""
        # models emit bare newlines/tabs inside strings and a second object
        # after the first; tolerate both before giving up on the reply
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(raw, strict=False)
            except json.JSONDecodeError:
                data = _first_json_object(raw)
        return schema.model_validate(data)

    # ------------------------------------------------------------------ #
    def _attach_images(
        self, messages: list[dict[str, str]], images: list[bytes] | None
    ) -> list[dict[str, Any]]:
        if not images:
            return list(messages)
        if not self.vision:
            raise RuntimeError("cloud model does not accept image input (vision disabled)")
        out: list[dict[str, Any]] = list(messages)
        # attach images to the last user message as content parts
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "user":
                text = out[i]["content"]
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
                for img in images:
                    parts.extend(
                        {"type": "image_url", "image_url": {"url": uri}}
                        for uri in _prepare_image(img)
                    )
                out[i] = {"role": "user", "content": parts}
                break
        return out


gateway = ModelGateway()
