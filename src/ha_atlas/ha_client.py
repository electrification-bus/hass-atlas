"""Home Assistant WebSocket API client."""

from __future__ import annotations

import asyncio
import json
from types import TracebackType
from typing import Any

import websockets
from websockets import ClientConnection


class HAClientError(Exception):
    """Error from the HA WebSocket API."""


class HAClient:
    """Async context manager for the HA WebSocket API.

    Usage::

        async with HAClient("http://ha.local:8123", "token") as client:
            devices = await client.send_command("config/device_registry/list")
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._ws: ClientConnection | None = None
        self._msg_id = 0

    @property
    def _ws_url(self) -> str:
        base = self._url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/api/websocket"

    async def __aenter__(self) -> HAClient:
        self._ws = await websockets.connect(self._ws_url, max_size=16 * 1024 * 1024)
        # HA sends auth_required on connect
        auth_required = json.loads(await self._ws.recv())
        if auth_required.get("type") != "auth_required":
            raise HAClientError(f"Expected auth_required, got: {auth_required.get('type')}")

        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_result = json.loads(await self._ws.recv())
        if auth_result.get("type") != "auth_ok":
            msg = auth_result.get("message", "Unknown auth error")
            raise HAClientError(f"Auth failed: {msg}")

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send_command(self, msg_type: str, **kwargs: Any) -> Any:
        """Send a command and return the result payload."""
        if not self._ws:
            raise HAClientError("Not connected")

        self._msg_id += 1
        msg = {"id": self._msg_id, "type": msg_type, **kwargs}
        await self._ws.send(json.dumps(msg))

        # Read responses until we get one matching our ID
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
            response = json.loads(raw)
            if response.get("id") == self._msg_id:
                if not response.get("success"):
                    error = response.get("error", {})
                    raise HAClientError(
                        f"{msg_type} failed: {error.get('message', 'Unknown error')}"
                    )
                return response.get("result")
