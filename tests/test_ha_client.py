"""Tests for the HA WebSocket client."""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import InvalidProxy, ProxyError

from hass_atlas import ha_client
from hass_atlas.ha_client import HAClient, HAClientError, redact_proxy


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
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=auth_ok_ws)
        async with HAClient("http://ha.local:8123", "test-token") as client:
            assert client is not None
        # Verify auth was sent
        sent = json.loads(auth_ok_ws._sent[0])
        assert sent["type"] == "auth"
        assert sent["access_token"] == "test-token"


@pytest.mark.asyncio
async def test_ha_version_from_auth_ok(auth_ok_ws: FakeWebSocket) -> None:
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=auth_ok_ws)
        async with HAClient("http://ha.local:8123", "test-token") as client:
            assert client.ha_version == "2024.12.0"


@pytest.mark.asyncio
async def test_auth_failure(auth_fail_ws: FakeWebSocket) -> None:
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
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
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
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
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
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
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
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
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=ws)
        async with HAClient("http://ha.local:8123", "token") as client:
            await client.send_command("config/device_registry/list")
            await client.send_command("config/entity_registry/list")
            sent1 = json.loads(ws._sent[1])
            sent2 = json.loads(ws._sent[2])
            assert sent1["id"] == 1
            assert sent2["id"] == 2


_PROXY_ENV = (
    "ws_proxy", "wss_proxy", "socks_proxy", "https_proxy", "http_proxy", "all_proxy", "no_proxy",
)


@pytest.fixture(autouse=True)
def _isolated_proxy_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read proxies from the environment only, starting from an empty one."""
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(
        urllib.request, "proxy_bypass", lambda host: urllib.request.proxy_bypass_environment(host)
    )


async def _connect_proxy(client: HAClient, auth_ok_ws: FakeWebSocket) -> object:
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(return_value=auth_ok_ws)
        async with client:
            pass
        return mock_ws.connect.call_args.kwargs["proxy"]


@pytest.mark.asyncio
async def test_no_proxy_configured_connects_directly(auth_ok_ws: FakeWebSocket) -> None:
    assert await _connect_proxy(HAClient("http://ha.local:8123", "t"), auth_ok_ws) is None


@pytest.mark.asyncio
async def test_explicit_proxy_reaches_connect(auth_ok_ws: FakeWebSocket) -> None:
    client = HAClient("http://ha.local:8123", "t", proxy="http://proxy:3128")
    assert await _connect_proxy(client, auth_ok_ws) == "http://proxy:3128"


@pytest.mark.asyncio
async def test_explicit_proxy_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch, auth_ok_ws: FakeWebSocket
) -> None:
    monkeypatch.setenv("https_proxy", "http://env:3128")
    client = HAClient("http://ha.local:8123", "t", proxy="http://cli:3128")
    assert await _connect_proxy(client, auth_ok_ws) == "http://cli:3128"


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "ALL_PROXY", "all_proxy", "ws_proxy"])
@pytest.mark.asyncio
async def test_environment_proxy_reaches_connect(
    monkeypatch: pytest.MonkeyPatch, auth_ok_ws: FakeWebSocket, name: str
) -> None:
    monkeypatch.setenv(name, "http://env:3128")
    client = HAClient("http://ha.local:8123", "t")
    assert await _connect_proxy(client, auth_ok_ws) == "http://env:3128"


@pytest.mark.asyncio
async def test_no_proxy_bypasses_all_proxy(
    monkeypatch: pytest.MonkeyPatch, auth_ok_ws: FakeWebSocket
) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5h://env:1080")
    monkeypatch.setenv("NO_PROXY", "ha.local")
    client = HAClient("http://ha.local:8123", "t")
    assert await _connect_proxy(client, auth_ok_ws) is None


@pytest.mark.parametrize("scheme", ["socks5h", "SOCKS5H", "Socks4"])
@pytest.mark.asyncio
async def test_socks_proxy_without_python_socks(
    monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    monkeypatch.setattr(ha_client.importlib.util, "find_spec", lambda name: None)
    client = HAClient("http://ha.local:8123", "t", proxy=f"{scheme}://user:secret@proxy:1080")
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock()
        with pytest.raises(
            HAClientError, match=r"pip install '\.\[socks\]' \(or poetry install -E socks\)"
        ) as info:
            async with client:
                pass
        mock_ws.connect.assert_not_called()
    assert "secret" not in str(info.value)


@pytest.mark.asyncio
async def test_proxy_error_is_client_error() -> None:
    client = HAClient("http://ha.local:8123", "t", proxy="http://user:secret@proxy:3128")
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(side_effect=ProxyError("proxy rejected connection"))
        with pytest.raises(HAClientError, match="through proxy http://user:\\*\\*\\*@proxy:3128"):
            async with client:
                pass


@pytest.mark.asyncio
async def test_invalid_proxy_is_client_error() -> None:
    client = HAClient("http://ha.local:8123", "t", proxy="ftp://proxy:21")
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(side_effect=InvalidProxy("ftp://proxy:21", "scheme ftp"))
        with pytest.raises(HAClientError, match="Invalid proxy ftp://proxy:21: scheme ftp"):
            async with client:
                pass


@pytest.mark.parametrize(("proxy", "shown"), [
    ("socks5h://localhost:1080", "socks5h://localhost:1080"),
    ("http://user:pw@proxy:3128", "http://user:***@proxy:3128"),
    ("http://user@proxy:3128", "http://user@proxy:3128"),
])
def test_redact_proxy(proxy: str, shown: str) -> None:
    assert redact_proxy(proxy) == shown


@pytest.mark.asyncio
async def test_send_on_closed_connection_is_connection_lost() -> None:
    from websockets.exceptions import ConnectionClosed

    class ClosedWebSocket(FakeWebSocket):
        async def send(self, data: str) -> None:
            if self._sent:
                raise ConnectionClosed(None, None)
            self._sent.append(data)

    ws = ClosedWebSocket([
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
    ])
    with patch("hass_atlas.ha_client.websockets.connect", AsyncMock(return_value=ws)):
        async with HAClient("http://ha.local:8123", "token") as client:
            with pytest.raises(ha_client.HAConnectionLost, match="Connection lost during"):
                await client.send_command("config/area_registry/list")
