from __future__ import annotations

import base64
import json
import os
import sys
from contextlib import asynccontextmanager

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from mock_paknow import PNG_1PX, MockPaknow


@asynccontextmanager
async def mcp_client(mock_server: MockPaknow):
    env = {
        **os.environ,
        "PAKNOW_BASE_URL": mock_server.base_url,
        "PAKNOW_TIMEOUT": "10",
        "PYTHONUNBUFFERED": "1",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "paknow_mcp"],
        env=env,
    )
    async with Client(params) as client:
        yield client


def text_of(result) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def image_blocks(result) -> list:
    return [block for block in result.content if getattr(block, "type", "") == "image"]


async def test_list_tools(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.list_tools()
        tools = {tool.name: tool for tool in result.tools}
    expected = {
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
    assert expected == set(tools)
    ask = tools["paknow_ask"]
    assert ask.annotations.read_only_hint is True
    assert ask.annotations.idempotent_hint is False
    assert "question" in ask.input_schema["properties"]
    assert "citations" in ask.output_schema["properties"]
    assert "answer_display" in ask.output_schema["properties"]


async def test_ask_normalizes_citations(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool(
            "paknow_ask", {"question": "双底低点必须一样吗？"}
        )
    assert result.is_error is False
    data = result.structured_content
    assert data["run_id"].startswith("run-mock-")
    assert data["answer_raw"].startswith("低点")
    assert "[1]" in data["answer_display"]
    assert "[2]" in data["answer_display"]
    assert data["citation_index_base"] == 0
    assert [c["display_index"] for c in data["citations"]] == [1, 2]
    assert "uncertainty" in data
    text = text_of(result)
    assert "[1]" in text and "[2]" in text
    assert "evidence_id=ev-001" in text
    assert "citation_index_base=0" in text


async def test_ask_include_figures(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool(
            "paknow_ask",
            {"question": "给我看图", "include_figures": True},
        )
    assert result.is_error is False
    assert len(image_blocks(result)) == 2
    assert len(result.structured_content["figures"]) == 2


async def test_chart_with_base64_and_cutoff(mock_server: MockPaknow) -> None:
    payload = base64.b64encode(PNG_1PX).decode()
    async with mcp_client(mock_server) as client:
        result = await client.call_tool(
            "paknow_chart",
            {
                "question": "这张图是什么形态？",
                "image_base64": f"data:image/png;base64,{payload}",
                "cutoff": 0.3,
            },
        )
    assert result.is_error is False
    assert result.structured_content["intent"] == "chart_analysis"
    assert "cutoff=0.3" in result.structured_content["answer_raw"]
    recorded = mock_server.last_request("/charts")
    assert recorded is not None
    assert recorded["query"]["cutoff"] == ["0.3"]
    assert "multipart/form-data" in (recorded["content_type"] or "")


async def test_chart_requires_image(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool("paknow_chart", {"question": "看图"})
    assert result.is_error is True
    assert "image_path or image_base64" in text_of(result)


async def test_search_and_empty(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool(
            "paknow_search",
            {
                "query": "双底",
                "top_k": 5,
                "doc_id": "doc-1",
                "block_type": "paragraph",
            },
        )
        recorded = mock_server.last_request("/search")
        empty = await client.call_tool("paknow_search", {"query": "nomatch"})
    assert result.is_error is False
    assert result.structured_content["count"] == 2
    assert result.structured_content["hits"][0]["evidence_id"] == "ev-001"
    assert recorded is not None
    body = json.loads(recorded["body"].decode("utf-8"))
    assert body["doc_id"] == "doc-1"
    assert body["block_type"] == "paragraph"
    assert empty.structured_content["count"] == 0
    assert "没有命中" in text_of(empty)


async def test_evidence_and_missing(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool("paknow_evidence", {"evidence_id": "ev-001"})
        missing = await client.call_tool(
            "paknow_evidence", {"evidence_id": "missing"}
        )
    assert result.is_error is False
    assert result.structured_content["doc_title"] == "测试书"
    assert result.structured_content["figures"][0]["figure_id"] == "fig-001"
    assert missing.is_error is True
    assert "404" in text_of(missing)
    assert "evidence not found" in text_of(missing)


async def test_documents_and_source(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        docs = await client.call_tool("paknow_documents", {})
        source = await client.call_tool("paknow_source", {"doc_id": "doc-1"})
    assert docs.structured_content["count"] == 2
    assert docs.structured_content["documents"][0]["doc_id"] == "doc-1"
    assert source.structured_content["evidence_units"] == 500
    assert source.structured_content["figures"] == 12


async def test_images_and_audit(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        page = await client.call_tool(
            "paknow_page_image", {"doc_id": "doc-1", "page": 10, "scale": 1.5}
        )
        figure = await client.call_tool(
            "paknow_figure_image", {"figure_id": "fig-001"}
        )
        run = await client.call_tool("paknow_run", {"run_id": "run-mock-1"})
        sops = await client.call_tool("paknow_sops", {"status": "shadow"})
        health = await client.call_tool("paknow_health", {})
    assert page.structured_content["kind"] == "page"
    assert page.structured_content["mime_type"] == "image/png"
    assert len(image_blocks(page)) == 1
    assert figure.structured_content["kind"] == "figure"
    assert len(image_blocks(figure)) == 1
    assert run.structured_content["evidence_ids"] == ["ev-001", "ev-002"]
    assert sops.structured_content["count"] == 1
    assert sops.structured_content["sops"][0]["status"] == "shadow"
    assert health.structured_content["status"] == "ok"
    assert "PAknow: ok" in text_of(health)


async def test_schema_validation_is_error(mock_server: MockPaknow) -> None:
    async with mcp_client(mock_server) as client:
        result = await client.call_tool("paknow_search", {"query": "x", "top_k": 99})
    assert result.is_error is True
    assert "top_k" in text_of(result)
