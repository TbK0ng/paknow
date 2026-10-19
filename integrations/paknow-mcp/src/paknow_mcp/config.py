"""Runtime configuration for the PAknow MCP adapter.

The adapter never imports PAknow internals: Qdrant embedded mode permits one
process per index path, and the HTTP service owns that process. Everything here
is therefore connection and safety policy for the REST API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Connection settings, resolved from CLI overrides then environment."""

    base_url: str = "http://127.0.0.1:8000"
    token: str | None = None
    timeout_seconds: float = 300.0
    connect_timeout_seconds: float = 10.0
    max_image_bytes: int = 20 * 1024 * 1024
    max_text_chars: int = 45_000
    max_figure_images: int = 4
    trust_env: bool = False
    user_agent: str = "paknow-mcp/0.1.0"

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> "Settings":
        resolved_base = (
            base_url
            or os.getenv("PAKNOW_BASE_URL")
            or os.getenv("PAKNOW_URL")
            or "http://127.0.0.1:8000"
        ).strip().rstrip("/")
        resolved_token = token if token is not None else os.getenv("PAKNOW_TOKEN") or None
        return cls(
            base_url=resolved_base,
            token=resolved_token or None,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else _env_float("PAKNOW_TIMEOUT", 300.0)
            ),
            connect_timeout_seconds=_env_float("PAKNOW_CONNECT_TIMEOUT", 10.0),
            max_image_bytes=_env_int("PAKNOW_MAX_IMAGE_BYTES", 20 * 1024 * 1024),
            max_text_chars=_env_int("PAKNOW_MAX_TEXT_CHARS", 45_000),
            max_figure_images=_env_int("PAKNOW_MAX_FIGURE_IMAGES", 4),
            trust_env=os.getenv("PAKNOW_TRUST_ENV", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    @property
    def auth_headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path


def resolve_image_path(raw: str | None) -> Path | None:
    """Resolve a tool-provided image path, rejecting directories."""
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path
