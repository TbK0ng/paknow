"""Deterministic mock of the PAknow REST API for adapter tests and release checks.

No third-party dependencies. It records requests so tests can assert that filters,
multipart uploads, and query parameters were forwarded correctly.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

DOCUMENTS = [
    {
        "doc_id": "doc-1",
        "title": "测试书",
        "author": "作者甲",
        "translator": None,
        "language": "zh",
        "format": "pdf",
        "edition": "1",
        "page_count": 100,
        "entry_count": 500,
    },
    {
        "doc_id": "doc-2",
        "title": "Second Book",
        "author": "Author B",
        "translator": None,
        "language": "en",
        "format": "epub",
        "edition": None,
        "page_count": None,
        "entry_count": 120,
    },
]

CITATIONS = [
    {
        "evidence_id": "ev-001",
        "citation": "《测试书》 → 第1章 → 图1.1 → PDF 第10页（章内印刷页 3）",
        "text": "The two lows should be about the same price, but they do not have to be exact.",
        "doc_id": "doc-1",
    },
    {
        "evidence_id": "ev-002",
        "citation": "《测试书》 → 第2章 → PDF 第20页",
        "text": "Context matters: a strong trend changes how a pullback should be read.",
        "doc_id": "doc-1",
    },
]

FIGURES = [
    {
        "figure_id": "fig-001",
        "figure_label": "1.1",
        "rel_path": "pdf/doc-1/000010_1.png",
        "width_px": 100,
        "height_px": 80,
    },
    {
        "figure_id": "fig-002",
        "figure_label": "2.3",
        "rel_path": "pdf/doc-1/000020_2.png",
        "width_px": 120,
        "height_px": 90,
    },
]

SOPS = [
    {
        "sop_id": "sop-001",
        "version": 2,
        "status": "shadow",
        "intent_scope": "knowledge_qa",
        "steps": ["retrieve", "verify", "answer"],
        "validation": {"wins": 3, "losses": 1, "ties": 0},
    },
    {
        "sop_id": "sop-002",
        "version": 1,
        "status": "candidate",
        "intent_scope": "synthesis",
        "steps": ["step_back", "retrieve"],
        "validation": {},
    },
]


def ask_payload(
    question: str,
    *,
    run_id: str,
    intent: str = "knowledge_qa",
    is_framework: bool = False,
    routes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "intent": intent,
        "is_framework": is_framework,
        "routes": routes or ["hybrid_text"],
        "wants_examples": False,
        "answer": "低点不必完全相同[0]；但背景条件很重要[1]。",
        "uncertainty": ["价差阈值资料未说明"],
        "citations": CITATIONS,
        "figures": FIGURES,
        "question_echo": question,
    }


class MockPaknow:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.mock = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self._counter = 0

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockPaknow":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def next_run_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"run-mock-{self._counter}"

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    def last_request(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self.requests):
                if item["path"] == path:
                    return item
        return None


class _Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # ------------------------------------------------------------------ #
    def _read(self) -> tuple[str, dict[str, list[str]], bytes]:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = parse_qs(parsed.query)
        self.server.mock.record(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": parsed.path,
                "query": query,
                "content_type": self.headers.get("Content-Type"),
                "body_size": len(body),
                "body": body,
            }
        )
        return parsed.path, query, body

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1PX)))
        self.end_headers()
        self.wfile.write(PNG_1PX)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        path, query, _ = self._read()

        if path == "/health":
            return self._json(200, {"status": "ok", "version": "mock-1"})
        if path == "/documents":
            return self._json(200, DOCUMENTS)
        if path == "/sops":
            status = (query.get("status") or [None])[0]
            rows = [s for s in SOPS if not status or s["status"] == status]
            return self._json(200, rows)
        if path.startswith("/evidence/"):
            evidence_id = path.rsplit("/", 1)[-1]
            if evidence_id == "missing":
                return self._json(404, {"detail": "evidence not found"})
            return self._json(
                200,
                {
                    "evidence_id": evidence_id,
                    "text": "Full evidence text for " + evidence_id,
                    "block_type": "paragraph",
                    "chapter_path": ["第1章"],
                    "pdf_page": 10,
                    "printed_page": "3",
                    "figure_label": "1.1",
                    "epub_href": None,
                    "doc_id": "doc-1",
                    "doc_title": "测试书",
                    "figures": [
                        {
                            "figure_id": "fig-001",
                            "rel_path": "pdf/doc-1/000010_1.png",
                            "label": "1.1",
                        }
                    ],
                },
            )
        if path.startswith("/sources/") and path.endswith("/image"):
            return self._png()
        if path.startswith("/sources/"):
            doc_id = path.rsplit("/", 1)[-1]
            if doc_id == "missing":
                return self._json(404, {"detail": "source not found"})
            return self._json(
                200,
                {
                    **DOCUMENTS[0],
                    "doc_id": doc_id,
                    "evidence_units": 500,
                    "figures": 12,
                },
            )
        if path.startswith("/figures/") and path.endswith("/image"):
            figure_id = path.split("/")[2]
            if figure_id == "missing":
                return self._json(404, {"detail": "figure not found"})
            return self._png()
        if path.startswith("/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            if run_id == "missing":
                return self._json(404, {"detail": "run not found"})
            return self._json(
                200,
                {
                    "run_id": run_id,
                    "intents": ["knowledge_qa"],
                    "routes": ["hybrid_text"],
                    "evidence_ids": ["ev-001", "ev-002"],
                    "output": {"answer": "ok"},
                    "failures": [],
                    "elapsed_ms": 1234,
                },
            )
        return self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path, query, body = self._read()
        mock: MockPaknow = self.server.mock  # type: ignore[attr-defined]

        if path == "/query":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"detail": "invalid json"})
            question = str(payload.get("query") or "")
            return self._json(
                200,
                ask_payload(question, run_id=mock.next_run_id()),
            )

        if path == "/charts":
            question = (query.get("query") or [""])[0]
            cutoff = (query.get("cutoff") or [""])[0]
            payload = ask_payload(
                question,
                run_id=mock.next_run_id(),
                intent="chart_analysis",
                routes=["hybrid_text", "visual_page"],
            )
            payload["answer"] = f"图表分析（cutoff={cutoff or '0'}）：可见结构[0]。"
            return self._json(200, payload)

        if path == "/search":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"detail": "invalid json"})
            query_text = str(payload.get("query") or "")
            if "nomatch" in query_text.lower():
                hits: list[dict[str, Any]] = []
            else:
                hits = [
                    {
                        "evidence_id": "ev-001",
                        "doc_id": "doc-1",
                        "text": "The two lows should be about the same price.",
                        "score": 0.91,
                        "block_type": "paragraph",
                        "chapter_path": ["第1章"],
                        "pdf_page": 10,
                        "figure_label": "1.1",
                        "epub_href": None,
                        "figure_ids": ["fig-001"],
                    },
                    {
                        "evidence_id": "ev-002",
                        "doc_id": "doc-1",
                        "text": "Context matters for pullbacks.",
                        "score": 0.82,
                        "block_type": "paragraph",
                        "chapter_path": ["第2章"],
                        "pdf_page": 20,
                        "figure_label": None,
                        "epub_href": None,
                        "figure_ids": [],
                    },
                ]
            return self._json(
                200, {"query": query_text, "count": len(hits), "hits": hits}
            )

        return self._json(404, {"detail": "not found"})
