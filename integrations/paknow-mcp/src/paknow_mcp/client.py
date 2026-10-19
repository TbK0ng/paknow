"""Async REST client for the PAknow service.

The adapter intentionally uses HTTP, not a Python import: PAknow's Qdrant
embedded mode allows a single client per index path, and the FastAPI process is
the owner. This module only translates MCP tool calls into stable HTTP calls.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from .config import Settings


class PaknowError(RuntimeError):
    """Base class for actionable PAknow failures."""


class PaknowUnavailable(PaknowError):
    def __init__(self, base_url: str, detail: str) -> None:
        super().__init__(
            f"PAknow is unreachable at {base_url}. Start the PAknow service first "
            f"(uvicorn apps.api.main:app --port 8000). Detail: {detail}"
        )


class PaknowTimeout(PaknowError):
    def __init__(self, base_url: str, timeout: float) -> None:
        super().__init__(
            f"PAknow did not respond at {base_url} within {timeout:.0f}s. "
            "The first query may be loading the local embedding model; retry once."
        )


class PaknowHTTPError(PaknowError):
    def __init__(self, status_code: int, detail: str, path: str) -> None:
        super().__init__(f"PAknow returned HTTP {status_code} for {path}: {detail}")
        self.status_code = status_code
        self.detail = detail


class PaknowResponseError(PaknowError):
    pass


@dataclass(frozen=True)
class ChartImage:
    filename: str
    data: bytes
    mime_type: str


class PaknowClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.connect_timeout_seconds,
            read=self.settings.timeout_seconds,
            write=60.0,
            pool=self.settings.timeout_seconds,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        retry_safe: bool,
        read_timeout: float | None = None,
    ) -> httpx.Response:
        url = self.settings.url(path)
        attempts = 2 if retry_safe else 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                timeout = self._timeout()
                if read_timeout is not None:
                    timeout = httpx.Timeout(
                        connect=self.settings.connect_timeout_seconds,
                        read=read_timeout,
                        write=60.0,
                        pool=read_timeout,
                    )
                async with httpx.AsyncClient(
                    timeout=timeout,
                    headers=self.settings.auth_headers,
                    follow_redirects=True,
                    trust_env=self.settings.trust_env,
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        json=json_body,
                        params=params,
                        files=files,
                    )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25)
                    continue
                raise PaknowTimeout(
                    self.settings.base_url, self.settings.timeout_seconds
                ) from exc
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25)
                    continue
                raise PaknowUnavailable(self.settings.base_url, str(exc)) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25)
                    continue
                raise PaknowError(f"PAknow request failed: {exc}") from exc

            if response.status_code >= 500 and attempt + 1 < attempts:
                await asyncio.sleep(0.25)
                continue
            if response.status_code >= 400:
                raise PaknowHTTPError(
                    response.status_code, _error_detail(response), path
                )
            return response

        raise PaknowError(f"PAknow request failed: {last_exc}")

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        retry_safe: bool,
    ) -> dict:
        response = await self._request(
            method,
            path,
            json_body=json_body,
            params=params,
            retry_safe=retry_safe,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaknowResponseError(
                f"PAknow returned non-JSON for {path}: {response.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise PaknowResponseError(
                f"PAknow returned an unexpected JSON shape for {path}"
            )
        return payload

    async def _list(
        self,
        path: str,
        *,
        params: dict | None = None,
        retry_safe: bool = True,
    ) -> list:
        response = await self._request(
            "GET", path, params=params, retry_safe=retry_safe
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaknowResponseError(
                f"PAknow returned non-JSON for {path}: {response.text[:300]}"
            ) from exc
        if not isinstance(payload, list):
            raise PaknowResponseError(
                f"PAknow returned an unexpected JSON shape for {path}"
            )
        return payload

    async def _bytes(self, path: str, *, params: dict | None = None) -> tuple[bytes, str]:
        response = await self._request(
            "GET", path, params=params, retry_safe=True
        )
        data = response.content
        if len(data) > self.settings.max_image_bytes:
            raise PaknowResponseError(
                f"Image from {path} is {len(data)} bytes; limit is "
                f"{self.settings.max_image_bytes} bytes"
            )
        return data, response.headers.get("content-type", "application/octet-stream")

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    async def health(self) -> dict:
        started = time.perf_counter()
        payload = await self._json("GET", "/health", retry_safe=True)
        payload = dict(payload)
        payload["_latency_ms"] = int((time.perf_counter() - started) * 1000)
        return payload

    async def ask(self, question: str) -> dict:
        return await self._json(
            "POST", "/query", json_body={"query": question}, retry_safe=False
        )

    async def chart(
        self, question: str, image: ChartImage, cutoff: float | None
    ) -> dict:
        params: dict[str, str] = {"query": question}
        if cutoff is not None:
            params["cutoff"] = str(cutoff)
        response = await self._request(
            "POST",
            "/charts",
            params=params,
            files={"file": (image.filename, image.data, image.mime_type)},
            retry_safe=False,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaknowResponseError(
                f"PAknow returned non-JSON for /charts: {response.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise PaknowResponseError("PAknow returned an unexpected /charts shape")
        return payload

    async def search(
        self,
        query: str,
        top_k: int,
        doc_id: str | None = None,
        block_type: str | None = None,
    ) -> dict:
        body: dict = {"query": query, "top_k": top_k}
        if doc_id:
            body["doc_id"] = doc_id
        if block_type:
            body["block_type"] = block_type
        return await self._json(
            "POST", "/search", json_body=body, retry_safe=True
        )

    async def evidence(self, evidence_id: str) -> dict:
        return await self._json(
            "GET", f"/evidence/{evidence_id}", retry_safe=True
        )

    async def documents(self) -> list[dict]:
        return await self._list("/documents")

    async def source(self, doc_id: str) -> dict:
        return await self._json(
            "GET", f"/sources/{doc_id}", retry_safe=True
        )

    async def figure_image(self, figure_id: str) -> tuple[bytes, str]:
        return await self._bytes(f"/figures/{figure_id}/image")

    async def page_image(
        self, doc_id: str, page: int, scale: float
    ) -> tuple[bytes, str]:
        return await self._bytes(
            f"/sources/{doc_id}/page/{page}/image", params={"scale": str(scale)}
        )

    async def run(self, run_id: str) -> dict:
        return await self._json("GET", f"/runs/{run_id}", retry_safe=True)

    async def sops(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else None
        return await self._list("/sops", params=params)


def _error_detail(response: httpx.Response) -> str:
    text = response.text.strip()
    try:
        payload = response.json()
    except ValueError:
        return text[:2000] or response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)[:2000]
    return str(payload)[:2000]
