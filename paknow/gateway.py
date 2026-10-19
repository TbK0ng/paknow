"""Unified cloud model gateway (plan §11.1, §11.2).

Single entry point for answer generation, routing, extraction and chart
observation. DeepSeek is OpenAI-compatible; the client stays swappable so a
local model can be substituted without touching call sites.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from openai import OpenAI
from pydantic import BaseModel

from paknow.config import settings


def _encode_image(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


class ModelGateway:
    def __init__(self) -> None:
        self.model = settings.deepseek_model
        self.effort = settings.deepseek_effort
        self.vision = settings.deepseek_vision
        self._client = OpenAI(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        )

    def _create(self, **kwargs: Any) -> Any:
        """Completion call with one retry for transient failures."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                return self._client.chat.completions.create(**kwargs)
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
        resp = self._create(**kwargs)
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
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
                    parts.append(
                        {"type": "image_url", "image_url": {"url": _encode_image(img)}}
                    )
                out[i] = {"role": "user", "content": parts}
                break
        return out


gateway = ModelGateway()
