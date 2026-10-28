"""CLI-level behavior that does not need a Home Assistant connection."""

from __future__ import annotations

import urllib.request
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from hass_atlas import context
from hass_atlas.cli import cli

_PROXY_ENV = (
    "ws_proxy", "wss_proxy", "socks_proxy", "https_proxy", "http_proxy", "all_proxy", "no_proxy",
)


@pytest.fixture(autouse=True)
def _isolated_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no proxy in the environment or OS settings."""
    for name in (*_PROXY_ENV, "hass_atlas_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    monkeypatch.setattr(
        urllib.request, "proxy_bypass", lambda host: urllib.request.proxy_bypass_environment(host)
    )


@pytest.mark.parametrize("args", [["--help"], ["audit", "--help"], ["energy", "--help"]])
def test_help_does_not_discover(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    """Help output never runs mDNS discovery."""

    def fail() -> list:
        raise AssertionError("mDNS discovery ran")

    monkeypatch.setattr(context, "discover_ha", fail)
    monkeypatch.delenv("HA_URL", raising=False)
    result = CliRunner().invoke(cli, ["--token", "t", *args])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_url_resolved_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class _HA:
        location_name = "Home"
        host = "h"
        url = "http://h:8123"
        version = "2026.9.4"

    def discover() -> list:
        calls.append(1)
        return [_HA()]

    monkeypatch.setattr(context, "discover_ha", discover)
    ctx = context.Context(url=None, token="t", dry_run=False)
    assert calls == []
    assert ctx.url == "http://h:8123"
    assert ctx.url == "http://h:8123"
    assert calls == [1]


def _block_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list:
        raise AssertionError("mDNS discovery ran")

    monkeypatch.setattr(context, "discover_ha", fail)


@pytest.mark.parametrize("via_env", [False, True])
def test_proxy_option_reaches_connect(monkeypatch: pytest.MonkeyPatch, via_env: bool) -> None:
    _block_discovery(monkeypatch)
    args = ["--token", "t", "--url", "http://ha.local:8123"]
    if via_env:
        monkeypatch.setenv("HASS_ATLAS_PROXY", "http://proxy:3128")
    else:
        args += ["--proxy", "http://proxy:3128"]
    with patch("hass_atlas.ha_client.websockets") as mock_ws:
        mock_ws.connect = AsyncMock(side_effect=OSError("refused"))
        result = CliRunner().invoke(cli, [*args, "audit"])
    assert result.exit_code == 1
    assert "through proxy http://proxy:3128" in result.output
    assert mock_ws.connect.call_args.kwargs["proxy"] == "http://proxy:3128"


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr()
    return " ".join((out.out + out.err).split())


def test_proxy_without_url_skips_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_discovery(monkeypatch)
    monkeypatch.delenv("HA_URL", raising=False)
    ctx = context.Context(url=None, token="t", dry_run=False, proxy="socks5h://localhost:1080")
    with pytest.raises(SystemExit):
        _ = ctx.url
    assert "mDNS discovery cannot run through a proxy" in _output(capsys)


def test_environment_proxy_warns_before_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5h://u:pw@localhost:1080")
    monkeypatch.setattr(context, "discover_ha", list)
    with pytest.raises(SystemExit):
        _ = context.Context(url=None, token="t", dry_run=False).url
    text = _output(capsys)
    assert "mDNS discovery only finds Home Assistant on the local network" in text
    assert "pw" not in text


@pytest.mark.parametrize("source", ["os", "no_proxy_star"])
def test_no_discovery_warning_without_applicable_env_proxy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    if source == "os":
        # An OS (e.g. macOS system) proxy shows up in getproxies() only.
        monkeypatch.setattr(
            urllib.request, "getproxies", lambda: {"https": "http://sysproxy:3128"}
        )
    else:
        monkeypatch.setenv("https_proxy", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(context, "discover_ha", list)
    with pytest.raises(SystemExit):
        _ = context.Context(url=None, token="t", dry_run=False).url
    assert "local network" not in _output(capsys)
