"""Probe an installed PAknow MCP server over stdio.

Run this with the release virtualenv's Python, with ``PAKNOW_BASE_URL`` pointing
at a mock or live PAknow service. It prints a JSON report on stdout and exits
nonzero if any check fails.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

EXPECTED_TOOLS = {
    "paknow_ask",
    "paknow_chart",
    "paknow_search",
    "paknow_evidence",
    "paknow_documents",
    "paknow_source",
    "paknow_page_image",
    "paknow_figure_image",
    "paknow_run",
    "paknow_sops",
    "paknow_health",
}


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, object]] = []
        self.failures = 0

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
            self.checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.checks.append(
                {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )

    def as_dict(self, tools: list[str]) -> dict[str, object]:
        return {
            "ok": self.failures == 0,
            "tools": tools,
            "checks": self.checks,
        }


def text_of(result) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def image_blocks(result) -> list:
    return [block for block in result.content if getattr(block, "type", "") == "image"]


async def run() -> dict[str, object]:
    env = {
        **os.environ,
        "PAKNOW_BASE_URL": os.environ["PAKNOW_BASE_URL"],
        "PAKNOW_TIMEOUT": os.environ.get("PAKNOW_TIMEOUT", "15"),
        "PYTHONUNBUFFERED": "1",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "paknow_mcp"],
        env=env,
    )
    report = Report()
    tools: list[str] = []

    async with Client(params) as client:
        listed = await client.list_tools()
        tool_map = {tool.name: tool for tool in listed.tools}
        tools = sorted(tool_map)

        report.check(
            "tool_set",
            lambda: sorted(EXPECTED_TOOLS) == tools,
        )
        report.check(
            "ask_schema",
            lambda: "question" in tool_map["paknow_ask"].input_schema["properties"]
            and "citations" in tool_map["paknow_ask"].output_schema["properties"],
        )
        report.check(
            "ask_annotations",
            lambda: tool_map["paknow_ask"].annotations.idempotent_hint is False
            and tool_map["paknow_search"].annotations.idempotent_hint is True,
        )
        report.check(
            "search_top_k_bound",
            lambda: tool_map["paknow_search"].input_schema["properties"]["top_k"].get(
                "maximum"
            )
            == 20,
        )

        ask = await client.call_tool(
            "paknow_ask", {"question": "双底低点必须一样吗？"}
        )
        report.check(
            "ask_success",
            lambda: ask.is_error is False and ask.structured_content["run_id"],
        )
        report.check(
            "ask_citation_normalization",
            lambda: "[1]" in ask.structured_content["answer_display"]
            and ask.structured_content["citation_index_base"] == 0
            and [c["display_index"] for c in ask.structured_content["citations"]]
            == [1, 2],
        )
        report.check(
            "ask_text_locator",
            lambda: "evidence_id=ev-001" in text_of(ask),
        )

        ask_figures = await client.call_tool(
            "paknow_ask", {"question": "给我看图", "include_figures": True}
        )
        report.check(
            "ask_images",
            lambda: ask_figures.is_error is False
            and len(image_blocks(ask_figures)) == 2
            and len(ask_figures.structured_content["figures"]) == 2,
        )

        chart = await client.call_tool(
            "paknow_chart",
            {
                "question": "这张图是什么形态？",
                "image_base64": "data:image/png;base64,"
                + base64.b64encode(PNG_1PX).decode(),
                "cutoff": 0.25,
            },
        )
        report.check(
            "chart_success",
            lambda: chart.is_error is False
            and chart.structured_content["intent"] == "chart_analysis"
            and "cutoff=0.25" in chart.structured_content["answer_raw"],
        )

        search = await client.call_tool(
            "paknow_search",
            {
                "query": "双底",
                "top_k": 5,
                "doc_id": "doc-1",
                "block_type": "paragraph",
            },
        )
        report.check(
            "search_success",
            lambda: search.is_error is False
            and search.structured_content["count"] == 2
            and search.structured_content["hits"][0]["evidence_id"] == "ev-001",
        )

        evidence = await client.call_tool(
            "paknow_evidence", {"evidence_id": "ev-001"}
        )
        report.check(
            "evidence_success",
            lambda: evidence.is_error is False
            and evidence.structured_content["doc_title"] == "测试书"
            and evidence.structured_content["figures"][0]["figure_id"] == "fig-001",
        )

        documents = await client.call_tool("paknow_documents", {})
        report.check(
            "documents_success",
            lambda: documents.is_error is False
            and documents.structured_content["count"] == 2,
        )

        source = await client.call_tool("paknow_source", {"doc_id": "doc-1"})
        report.check(
            "source_success",
            lambda: source.is_error is False
            and source.structured_content["evidence_units"] == 500,
        )

        page = await client.call_tool(
            "paknow_page_image", {"doc_id": "doc-1", "page": 10, "scale": 1.5}
        )
        report.check(
            "page_image_success",
            lambda: page.is_error is False
            and len(image_blocks(page)) == 1
            and page.structured_content["mime_type"] == "image/png",
        )

        figure = await client.call_tool(
            "paknow_figure_image", {"figure_id": "fig-001"}
        )
        report.check(
            "figure_image_success",
            lambda: figure.is_error is False and len(image_blocks(figure)) == 1,
        )

        run = await client.call_tool("paknow_run", {"run_id": "run-mock-1"})
        report.check(
            "run_success",
            lambda: run.is_error is False
            and run.structured_content["evidence_ids"] == ["ev-001", "ev-002"],
        )

        sops = await client.call_tool("paknow_sops", {"status": "shadow"})
        report.check(
            "sops_success",
            lambda: sops.is_error is False and sops.structured_content["count"] == 1,
        )

        health = await client.call_tool("paknow_health", {})
        report.check(
            "health_success",
            lambda: health.is_error is False
            and health.structured_content["status"] == "ok",
        )

        missing = await client.call_tool(
            "paknow_evidence", {"evidence_id": "missing"}
        )
        report.check(
            "missing_evidence_is_error",
            lambda: missing.is_error is True and "404" in text_of(missing),
        )

        no_image = await client.call_tool("paknow_chart", {"question": "看图"})
        report.check(
            "chart_requires_image",
            lambda: no_image.is_error is True
            and "image_path or image_base64" in text_of(no_image),
        )

        invalid = await client.call_tool(
            "paknow_search", {"query": "x", "top_k": 99}
        )
        report.check(
            "schema_validation_error",
            lambda: invalid.is_error is True and "top_k" in text_of(invalid),
        )

    return report.as_dict(tools)


def main() -> None:
    if "PAKNOW_BASE_URL" not in os.environ:
        print("PAKNOW_BASE_URL is required", file=sys.stderr)
        raise SystemExit(2)
    try:
        report = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"probe crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
