from __future__ import annotations

import pytest

from mock_paknow import MockPaknow
from paknow_mcp.client import PaknowClient
from paknow_mcp.config import Settings


@pytest.fixture
def mock_server():
    server = MockPaknow().start()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def settings(mock_server: MockPaknow) -> Settings:
    return Settings.from_env(
        base_url=mock_server.base_url,
        timeout_seconds=10.0,
    )


@pytest.fixture
def client(settings: Settings) -> PaknowClient:
    return PaknowClient(settings)
