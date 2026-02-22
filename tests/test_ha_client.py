"""Tests for the HA WebSocket client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ha_atlas.ha_client import HAClient, HAClientError


class FakeWebSocket:
    """Fake WebSocket connection for testing."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._sent: list[str] = []

    async def recv(self) -> str:
        if not self._messages:
            raise RuntimeError("No more messages")
        return self._messages.pop(0)

    async def send(self, data: str) -> None:
        self._sent.append(data)

    async def close(self) -> None:
        pass


@pytest.fixture
def auth_ok_ws() -> FakeWebSocket:
    """WebSocket that completes auth successfully."""
    return FakeWebSocket([
        json.dumps({"type": "auth_required", "ha_version": "2024.12.0"}),
        json.dumps({"type": "auth_ok", "ha_version": "2024.12.0"}),
    ])


@pytest.fixture
def auth_fail_ws() -> FakeWebSocket:
    return FakeWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_invalid", "message": "Invalid access token"}),
    ])


@pytest.mark.asyncio
async def test_connect_and_auth(auth_ok_ws: FakeWebSocket) -> None:
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=auth_ok_ws)
        async with HAClient("http://ha.local:8123", "test-token") as client:
            assert client is not None
        # Verify auth was sent
        sent = json.loads(auth_ok_ws._sent[0])
        assert sent["type"] == "auth"
        assert sent["access_token"] == "test-token"


@pytest.mark.asyncio
async def test_auth_failure(auth_fail_ws: FakeWebSocket) -> None:
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=auth_fail_ws)
        with pytest.raises(HAClientError, match="Auth failed"):
            async with HAClient("http://ha.local:8123", "bad-token"):
                pass


@pytest.mark.asyncio
async def test_send_command() -> None:
    ws = FakeWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True, "result": [{"id": "dev1"}]}),
    ])
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=ws)
        async with HAClient("http://ha.local:8123", "token") as client:
            result = await client.send_command("config/device_registry/list")
            assert result == [{"id": "dev1"}]
            sent = json.loads(ws._sent[1])  # [0] is auth
            assert sent["id"] == 1
            assert sent["type"] == "config/device_registry/list"


@pytest.mark.asyncio
async def test_send_command_error() -> None:
    ws = FakeWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({
            "id": 1, "type": "result", "success": False,
            "error": {"code": "not_found", "message": "Entity not found"},
        }),
    ])
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=ws)
        async with HAClient("http://ha.local:8123", "token") as client:
            with pytest.raises(HAClientError, match="Entity not found"):
                await client.send_command("config/entity_registry/get", entity_id="bogus")


@pytest.mark.asyncio
async def test_send_command_with_kwargs() -> None:
    ws = FakeWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True, "result": {"area_id": "new"}}),
    ])
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=ws)
        async with HAClient("http://ha.local:8123", "token") as client:
            result = await client.send_command(
                "config/area_registry/create", name="Garage"
            )
            assert result == {"area_id": "new"}
            sent = json.loads(ws._sent[1])
            assert sent["name"] == "Garage"


@pytest.mark.asyncio
async def test_ws_url_conversion() -> None:
    client = HAClient("http://ha.local:8123", "token")
    assert client._ws_url == "ws://ha.local:8123/api/websocket"

    client_ssl = HAClient("https://ha.local:8123", "token")
    assert client_ssl._ws_url == "wss://ha.local:8123/api/websocket"


@pytest.mark.asyncio
async def test_auto_increment_ids() -> None:
    ws = FakeWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True, "result": []}),
        json.dumps({"id": 2, "type": "result", "success": True, "result": []}),
    ])
    with patch("ha_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=ws)
        async with HAClient("http://ha.local:8123", "token") as client:
            await client.send_command("config/device_registry/list")
            await client.send_command("config/entity_registry/list")
            sent1 = json.loads(ws._sent[1])
            sent2 = json.loads(ws._sent[2])
            assert sent1["id"] == 1
            assert sent2["id"] == 2
