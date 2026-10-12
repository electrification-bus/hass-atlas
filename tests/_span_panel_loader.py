"""Turn span_panel fixtures into HA registry rows, model objects, and states.

``expected_entities/<stem>.json`` holds the devices (keyed by their
``span_panel`` identifier) and entities (keyed by unique_id) the integration
creates; ``topology/<stem>.json`` is the matching ``span_panel/panel_topology``
response.  The fixtures use each device's identifier as its HA device id, so
the loader does too: the panel device id then equals the topology's
``panel_device_id`` and each sub-device id equals its ``sub_devices`` key.

``synthetic/`` holds edited replays in the same layout plus
``states/<stem>.json`` (entity_id -> {state, attributes}), which supplies each
entity's recorded attributes in place of the ones derived from
``expected_entities``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hass_atlas.models import HADevice, HAEntity
from hass_atlas.registry import parse_registries

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "span_panel"
SYNTHETIC_DIR = FIXTURES_DIR / "synthetic"

STEMS = sorted(p.stem for p in (FIXTURES_DIR / "topology").glob("*.json"))
SYNTHETIC_STEMS = sorted(p.stem for p in (SYNTHETIC_DIR / "topology").glob("*.json"))


@dataclass
class SpanPanelFixture:
    """One fixture panel as HA would present it."""

    stem: str
    topology: dict[str, Any]
    raw_devices: list[dict[str, Any]]
    raw_entities: list[dict[str, Any]]
    states: dict[str, dict[str, Any]]

    def models(self) -> tuple[list[HADevice], list[HAEntity]]:
        devices, entities, _ = parse_registries(self.raw_devices, self.raw_entities, [])
        return devices, entities


def load_json(kind: str, stem: str, base: Path = FIXTURES_DIR) -> dict[str, Any]:
    return json.loads((base / kind / f"{stem}.json").read_text())


def build_fixture(
    stem: str,
    topology: dict[str, Any],
    expected: dict[str, Any],
    recorded_states: dict[str, Any] | None = None,
) -> SpanPanelFixture:
    raw_devices: list[dict[str, Any]] = []
    for ident, dev in expected["devices"].items():
        raw_devices.append({
            "id": ident,
            "name": dev.get("name"),
            "name_by_user": None,
            "model": dev.get("model"),
            "manufacturer": dev.get("manufacturer"),
            "identifiers": [["span_panel", ident]],
            "via_device_id": dev.get("via_device"),
            "area_id": None,
        })

    # config/entity_registry/list carries no device_class or state_class;
    # those come from states (see enrich_entities_from_states).
    raw_entities: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    for unique_id, ent in expected["entities"].items():
        raw_entities.append({
            "entity_id": ent["entity_id"],
            "unique_id": unique_id,
            "platform": "span_panel",
            "device_id": ent.get("device"),
            "original_name": ent.get("original_name"),
            "disabled_by": ent.get("disabled_by"),
            "entity_category": ent.get("entity_category"),
            "has_entity_name": True,
            "area_id": None,
        })
        if ent.get("disabled_by"):
            continue
        recorded = (recorded_states or {}).get(ent["entity_id"])
        if recorded is not None:
            states[ent["entity_id"]] = {
                "state": recorded.get("state"),
                "attributes": dict(recorded.get("attributes") or {}),
            }
            continue
        attrs = {
            k: ent[k]
            for k in ("device_class", "state_class", "unit_of_measurement")
            if ent.get(k) is not None
        }
        state = ent.get("state")
        states[ent["entity_id"]] = {
            "state": "unknown" if state is None else state,
            "attributes": attrs,
        }
    return SpanPanelFixture(stem, topology, raw_devices, raw_entities, states)


def _load(stem: str, base: Path, entity_prefix: str | None) -> SpanPanelFixture:
    def read(kind: str) -> dict[str, Any] | None:
        path = base / kind / f"{stem}.json"
        if not path.exists():
            return None
        text = path.read_text()
        if entity_prefix is not None:
            for domain in ("sensor", "binary_sensor", "switch", "select", "button"):
                text = text.replace(f'"{domain}.span_panel_', f'"{domain}.{entity_prefix}_')
        data: dict[str, Any] = json.loads(text)
        return data

    topology, expected = read("topology"), read("expected_entities")
    assert topology is not None and expected is not None, (base, stem)
    return build_fixture(stem, topology, expected, read("states"))


def load_fixture(stem: str) -> SpanPanelFixture:
    return _load(stem, FIXTURES_DIR, None)


def load_synthetic(stem: str) -> SpanPanelFixture:
    """Load an edited replay from ``synthetic/``, with its recorded state attributes."""
    return _load(stem, SYNTHETIC_DIR, None)


def renamed_fixture(
    stem: str, entity_prefix: str, base: Path = FIXTURES_DIR
) -> SpanPanelFixture:
    """Load a fixture with every ``sensor.span_panel_`` style entity id re-prefixed.

    All fixtures name their entities ``<domain>.span_panel_*``; combining two
    panels in one HA needs distinct entity ids, as HA would assign them.
    """
    return _load(stem, base, entity_prefix)


def drop_newer_fields(topology: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a topology payload shaped as the released 2.1.2 sends it."""
    old = copy.deepcopy(topology)
    for key in ("first_position", "last_position"):
        old.pop(key, None)
    for circuit in old["circuits"].values():
        for key in ("outside_panel", "is_generation", "shared_meter_group", "shared_relay_group"):
            circuit.pop(key, None)
    return old


class FakeClient:
    """Mock HA client answering registry, state, prefs, and panel_topology commands."""

    def __init__(
        self,
        fixtures: list[SpanPanelFixture],
        prefs: dict[str, Any] | None = None,
        extra_devices: list[dict[str, Any]] | None = None,
        extra_entities: list[dict[str, Any]] | None = None,
        topology_error: Exception | None = None,
    ) -> None:
        self.fixtures = fixtures
        self.prefs = prefs or {}
        self.extra_devices = extra_devices or []
        self.extra_entities = extra_entities or []
        self.topology_error = topology_error
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_command(self, msg_type: str, **kwargs: Any) -> Any:
        self.sent.append((msg_type, kwargs))
        if msg_type == "config/device_registry/list":
            return [d for f in self.fixtures for d in f.raw_devices] + self.extra_devices
        if msg_type == "config/entity_registry/list":
            return [e for f in self.fixtures for e in f.raw_entities] + self.extra_entities
        if msg_type == "config/area_registry/list":
            return []
        if msg_type == "get_states":
            return [
                {"entity_id": eid, **st} for f in self.fixtures for eid, st in f.states.items()
            ]
        if msg_type == "energy/get_prefs":
            return self.prefs
        if msg_type == "span_panel/panel_topology":
            if self.topology_error:
                raise self.topology_error
            for f in self.fixtures:
                if f.topology["panel_device_id"] == kwargs["device_id"]:
                    return f.topology
            raise AssertionError(f"unknown device {kwargs['device_id']}")
        raise AssertionError(f"unexpected command {msg_type}")
