"""Stateful in-memory Home Assistant for atlas tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Self

from hass_atlas.ha_client import HAClientError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "atlas"

_LISTS = {
    "config/floor_registry/list": "floors",
    "config/label_registry/list": "labels",
    "config/area_registry/list": "areas",
}


def load_fixture(name: str = "small") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


class FakeHA:
    """Answers registry list commands from its state and records every command.

    Any command it does not know fails the test.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = copy.deepcopy(data if data is not None else load_fixture())
        self.ha_version: str | None = data.get("ha_version")
        self.registries: dict[str, list[dict[str, Any]]] = {
            name: data.get(name, []) for name in _LISTS.values()
        }
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, HAClientError] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_command(self, msg_type: str, **kwargs: Any) -> Any:
        self.sent.append((msg_type, kwargs))
        if msg_type in self.fail:
            raise self.fail[msg_type]
        if msg_type in _LISTS:
            return copy.deepcopy(self.registries[_LISTS[msg_type]])
        raise AssertionError(f"unexpected command {msg_type}")
