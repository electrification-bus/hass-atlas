"""Home Assistant WebSocket API client."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import urllib.parse
import urllib.request
from types import TracebackType
from typing import Any

import websockets
from websockets import ClientConnection
from websockets.exceptions import InvalidProxy, ProxyError
from websockets.proxy import get_proxy
from websockets.uri import parse_uri


class HAClientError(Exception):
    """Error from the HA WebSocket API.

    ``code`` carries the error code HA returned for a failed command (for
    example ``unauthorized``), or None for connection and protocol errors.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class HAConnectionLost(HAClientError):
    """A command timed out or the connection dropped, so whether HA ran it is unknown."""


def resolve_proxy(ws_url: str, proxy: str | None = None) -> str | None:
    """Return the proxy to reach *ws_url* through, or None to connect directly.

    An explicit *proxy* wins. Otherwise the environment and operating system
    configuration are read the way websockets reads them, and then
    ``ALL_PROXY``, which websockets ignores; ``NO_PROXY`` applies to both.
    """
    if proxy:
        return proxy
    uri = parse_uri(ws_url)
    found = get_proxy(uri)
    if found is None and not urllib.request.proxy_bypass(f"{uri.host}:{uri.port}"):
        found = urllib.request.getproxies().get("all")
    return found


def redact_proxy(proxy: str) -> str:
    """Return *proxy* with any password removed, for messages."""
    parsed = urllib.parse.urlsplit(proxy)
    userinfo, sep, hostport = parsed.netloc.rpartition("@")
    if not sep or ":" not in userinfo:
        return proxy
    user = userinfo.split(":", 1)[0]
    return urllib.parse.urlunsplit(parsed._replace(netloc=f"{user}:***@{hostport}"))


class HAClient:
    """Async context manager for the HA WebSocket API.

    Usage::

        async with HAClient("http://ha.local:8123", "token") as client:
            devices = await client.send_command("config/device_registry/list")
    """

    def __init__(self, url: str, token: str, proxy: str | None = None) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._proxy = proxy
        self._ws: ClientConnection | None = None
        self._msg_id = 0
        # Home Assistant's version, from the auth_ok message.
        self.ha_version: str | None = None

    @property
    def _ws_url(self) -> str:
        base = self._url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/api/websocket"

    async def __aenter__(self) -> HAClient:
        proxy = resolve_proxy(self._ws_url, self._proxy)
        via = f" through proxy {redact_proxy(proxy)}" if proxy else ""
        if (
            proxy
            and urllib.parse.urlsplit(proxy).scheme.startswith("socks")
            and importlib.util.find_spec("python_socks") is None
        ):
            raise HAClientError(
                f"SOCKS proxy {redact_proxy(proxy)} needs python-socks. In the hass-atlas "
                "checkout, run: pip install '.[socks]' (or poetry install -E socks)"
            )
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._ws_url, max_size=16 * 1024 * 1024, proxy=proxy),
                timeout=10.0,
            )
        except TimeoutError:
            raise HAClientError(
                f"Connection timed out: {self._url}{via}. Is Home Assistant running?"
            ) from None
        except InvalidProxy as exc:
            raise HAClientError(f"Invalid proxy {redact_proxy(exc.proxy)}: {exc.msg}") from None
        except ProxyError as exc:
            cause = f": {exc.__cause__}" if exc.__cause__ else ""
            raise HAClientError(f"Cannot connect to {self._url}{via}: {exc}{cause}") from None
        except OSError as exc:
            raise HAClientError(f"Cannot connect to {self._url}{via}: {exc}") from None

        try:
            # HA sends auth_required on connect
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            auth_required = json.loads(raw)
            if auth_required.get("type") != "auth_required":
                raise HAClientError(
                    f"Expected auth_required, got: {auth_required.get('type')}"
                )

            await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            auth_result = json.loads(raw)
            if auth_result.get("type") != "auth_ok":
                msg = auth_result.get("message", "Unknown auth error")
                raise HAClientError(f"Auth failed: {msg}")
            self.ha_version = auth_result.get("ha_version")
        except TimeoutError:
            await self._ws.close()
            raise HAClientError(
                "Auth handshake timed out — Home Assistant may be overloaded"
            ) from None

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

        try:
            await self._ws.send(json.dumps(msg))
            # Read responses until we get one matching our ID
            while True:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
                response = json.loads(raw)
                if response.get("id") == self._msg_id:
                    if not response.get("success"):
                        error = response.get("error", {})
                        raise HAClientError(
                            f"{msg_type} failed: {error.get('message', 'Unknown error')}",
                            code=error.get("code"),
                        )
                    return response.get("result")
        except HAClientError:
            raise
        except TimeoutError:
            raise HAConnectionLost(
                f"Command '{msg_type}' timed out after 30s"
            ) from None
        except (websockets.exceptions.ConnectionClosed, OSError):
            raise HAConnectionLost(
                f"Connection lost during '{msg_type}'"
            ) from None
