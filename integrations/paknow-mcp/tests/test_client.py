from __future__ import annotations

import json

import pytest

from paknow_mcp.client import ChartImage, PaknowClient, PaknowHTTPError, PaknowUnavailable
from paknow_mcp.config import Settings


async def test_health(client: PaknowClient) -> None:
    data = await client.health()
    assert data["status"] == "ok"
    assert data["_latency_ms"] >= 0


async def test_ask(client: PaknowClient) -> None:
    data = await client.ask("双底低点必须一样吗？")
    assert data["answer"].startswith("低点")
    assert len(data["citations"]) == 2
    assert data["question_echo"] == "双底低点必须一样吗？"


async def test_chart_forwards_cutoff(client: PaknowClient, mock_server) -> None:
    data = await client.chart(
        "这张图是什么形态？",
        ChartImage(filename="chart.png", data=b"fake-png", mime_type="image/png"),
        0.3,
    )
    assert data["intent"] == "chart_analysis"
    recorded = mock_server.last_request("/charts")
    assert recorded is not None
    assert recorded["query"]["cutoff"] == ["0.3"]
    assert "multipart/form-data" in (recorded["content_type"] or "")


async def test_search_forwards_filters(client: PaknowClient, mock_server) -> None:
    await client.search("双底", 5, doc_id="doc-1", block_type="paragraph")
    recorded = mock_server.last_request("/search")
    assert recorded is not None
    body = json.loads(recorded["body"].decode("utf-8"))
    assert body["doc_id"] == "doc-1"
    assert body["block_type"] == "paragraph"


async def test_evidence_404_is_actionable(client: PaknowClient) -> None:
    with pytest.raises(PaknowHTTPError) as excinfo:
        await client.evidence("missing")
    assert excinfo.value.status_code == 404
    assert "evidence not found" in str(excinfo.value)


async def test_unreachable_is_actionable() -> None:
    # Port 1 is not expected to host a service in the test environment.
    client = PaknowClient(
        Settings.from_env(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    )
    with pytest.raises(PaknowUnavailable) as excinfo:
        await client.health()
    assert "Start the PAknow service" in str(excinfo.value)
