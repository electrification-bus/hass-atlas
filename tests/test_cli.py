"""CLI-level behavior that does not need a Home Assistant connection."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from hass_atlas import context
from hass_atlas.cli import cli


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
