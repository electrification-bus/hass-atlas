"""Fetch and parse HA device/entity/area registries into SpanDeviceTree."""

from __future__ import annotations

from typing import Any

from hass_atlas.ha_client import HAClient, HAClientError
from hass_atlas.models import HAArea, HADevice, HAEntity, SpanDeviceTree

DOMAIN = "span_ebus"

# Model strings used to classify child devices (span-hass 0.2.0)
MODEL_PANEL = "SPAN Panel"
MODEL_CIRCUIT = "Circuit"
MODEL_BATTERY = "Battery Storage"
MODEL_SOLAR = "Solar PV"
MODEL_EV_CHARGER = "EV Charger"
MODEL_LUGS = "Lugs"
MODEL_MID = "Microgrid Interconnect Device"


def _lug_direction(device: HADevice) -> str | None:
    """Return 'up' or 'dn' from a Lugs device's identifier suffix."""
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        if ident.endswith("-lugs-up"):
            return "up"
        if ident.endswith("-lugs-dn"):
            return "dn"
    return None


# unique_id suffixes of a branch circuit's breaker-space entity: ``info/spaces``
# (r202639 firmware) and ``info/space`` (earlier firmware).
_SPACE_SUFFIXES = ("_info_spaces", "_info_space")


def _has_space_entity(device: HADevice) -> bool:
    return any(e.unique_id.endswith(_SPACE_SUFFIXES) for e in device.entities)


def split_remote_cts(circuits: list[HADevice]) -> tuple[list[HADevice], list[HADevice]]:
    """Split a panel's Circuit-model devices into branch circuits and remote CTs.

    span-hass treats a circuit that declares no ``info/spaces`` as a meter
    outside the panel (``util.is_off_panel_circuit``), and creates no
    breaker-space entity for it.  hass-atlas sees the entities, not the
    declaration, so a circuit without a breaker-space entity is a remote CT
    only when another circuit on the same panel has one; that leaves panels
    whose integration creates no space entity at all classified as before.
    """
    if not any(_has_space_entity(c) for c in circuits):
        return list(circuits), []
    remote = [
        c for c in circuits if c.model == MODEL_CIRCUIT and not _has_space_entity(c)
    ]
    remote_ids = {c.id for c in remote}
    return [c for c in circuits if c.id not in remote_ids], remote


def _parse_device(raw: dict[str, Any]) -> HADevice:
    """Parse a raw device registry entry."""
    identifiers = [
        (pair[0], pair[1])
        for pair in raw.get("identifiers", [])
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]
    return HADevice(
        id=raw["id"],
        name=raw.get("name"),
        name_by_user=raw.get("name_by_user"),
        model=raw.get("model"),
        identifiers=identifiers,
        via_device_id=raw.get("via_device_id"),
        area_id=raw.get("area_id"),
    )


def _parse_entity(raw: dict[str, Any]) -> HAEntity:
    """Parse a raw entity registry entry."""
    return HAEntity(
        entity_id=raw["entity_id"],
        unique_id=raw["unique_id"],
        platform=raw.get("platform", ""),
        device_id=raw.get("device_id"),
        device_class=raw.get("device_class") or raw.get("original_device_class"),
        state_class=raw.get("state_class") or raw.get("original_state_class"),
        unit_of_measurement=(
            raw.get("unit_of_measurement") or raw.get("original_unit_of_measurement")
        ),
        name=raw.get("name"),
        original_name=raw.get("original_name"),
        disabled_by=raw.get("disabled_by"),
        entity_category=raw.get("entity_category"),
        has_entity_name=bool(raw.get("has_entity_name")),
    )


def _parse_area(raw: dict[str, Any]) -> HAArea:
    """Parse a raw area registry entry."""
    return HAArea(area_id=raw["area_id"], name=raw["name"])


def _is_span_device(device: HADevice) -> bool:
    """Check if a device belongs to the span_ebus integration."""
    return any(domain == DOMAIN for domain, _ in device.identifiers)


def _build_trees(
    devices: list[HADevice],
    entities: list[HAEntity],
) -> list[SpanDeviceTree]:
    """Build SpanDeviceTree(s) from devices and entities."""
    # Index entities by device_id
    entities_by_device: dict[str, list[HAEntity]] = {}
    for entity in entities:
        if entity.device_id:
            entities_by_device.setdefault(entity.device_id, []).append(entity)

    # Filter to span_ebus devices and attach entities
    span_devices: dict[str, HADevice] = {}
    for device in devices:
        if _is_span_device(device):
            device.entities = entities_by_device.get(device.id, [])
            span_devices[device.id] = device

    # Separate panel devices from sub-devices (circuits, battery, lugs, ...).
    # A SPAN Panel device is always a panel even if via_device_id points
    # to another panel (sub-panel in a daisy-chain).  Only non-panel
    # child devices are grouped under their parent.
    panels: list[HADevice] = []
    children_by_parent: dict[str, list[HADevice]] = {}
    for device in span_devices.values():
        if device.model == MODEL_PANEL or not (
            device.via_device_id and device.via_device_id in span_devices
        ):
            panels.append(device)
        else:
            children_by_parent.setdefault(device.via_device_id, []).append(device)

    # Build trees.  Walks panel-direct children, then recurses one level for the
    # MID (grandchild of BESS — synthesized per commissioned BESS).
    trees: list[SpanDeviceTree] = []
    for panel in panels:
        tree = SpanDeviceTree(panel=panel)
        for child in children_by_parent.get(panel.id, []):
            panel.children.append(child)
            model = child.model or ""
            if model == MODEL_CIRCUIT:
                tree.circuits.append(child)
            elif model == MODEL_BATTERY:
                tree.battery = child
            elif model == MODEL_SOLAR:
                tree.solar = child
            elif model == MODEL_EV_CHARGER:
                tree.ev_charger = child
            elif model == MODEL_LUGS:
                direction = _lug_direction(child)
                if direction == "up":
                    tree.lugs_up = child
                elif direction == "dn":
                    tree.lugs_dn = child
                # Lugs with no direction suffix — drop on the floor; would
                # indicate a span-hass identifier-format change worth surfacing.
            else:
                # Unknown child type — treat as circuit
                tree.circuits.append(child)
        tree.circuits, tree.remote_cts = split_remote_cts(tree.circuits)
        # Pick up the MID grandchild (via_device_id → BESS)
        if tree.battery is not None:
            for grandchild in children_by_parent.get(tree.battery.id, []):
                tree.battery.children.append(grandchild)
                if (grandchild.model or "") == MODEL_MID:
                    tree.mid = grandchild
        trees.append(tree)

    return trees


def enrich_entities_from_states(
    entities: list[HAEntity],
    states: dict[str, dict],
) -> None:
    """Populate device_class, state_class, and unit from entity states.

    The HA entity registry does NOT include device_class or state_class —
    these are runtime properties only available in entity states.
    Call this after fetching both registries and states.
    """
    for entity in entities:
        state_entry = states.get(entity.entity_id)
        if not state_entry:
            continue
        attrs = state_entry.get("attributes", {})
        if not entity.device_class and "device_class" in attrs:
            entity.device_class = attrs["device_class"]
        if not entity.state_class and "state_class" in attrs:
            entity.state_class = attrs["state_class"]
        if not entity.unit_of_measurement and "unit_of_measurement" in attrs:
            entity.unit_of_measurement = attrs["unit_of_measurement"]


def build_span_trees(
    devices: list[HADevice],
    entities: list[HAEntity],
) -> list[SpanDeviceTree]:
    """Build SPAN device trees from already-fetched registries."""
    span_entities = [e for e in entities if e.platform == DOMAIN]
    return _build_trees(devices, span_entities)


async def fetch_raw_registries(
    client: HAClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch raw device, entity, and area registry rows from HA."""
    raw_devices = await client.send_command("config/device_registry/list")
    raw_entities = await client.send_command("config/entity_registry/list")
    raw_areas = await client.send_command("config/area_registry/list")
    return raw_devices, raw_entities, raw_areas


def parse_registries(
    raw_devices: list[dict[str, Any]],
    raw_entities: list[dict[str, Any]],
    raw_areas: list[dict[str, Any]],
) -> tuple[list[HADevice], list[HAEntity], list[HAArea]]:
    """Parse raw registry rows into model objects."""
    devices = [_parse_device(d) for d in raw_devices]
    entities = [_parse_entity(e) for e in raw_entities]
    areas = [_parse_area(a) for a in raw_areas]
    return devices, entities, areas


def entity_area_ids(raw_entities: list[dict[str, Any]]) -> dict[str, str]:
    """Map entity_id to the area assigned on the entity itself (not via its device)."""
    return {e["entity_id"]: e["area_id"] for e in raw_entities if e.get("area_id")}


async def fetch_registries(client: HAClient) -> tuple[list[HADevice], list[HAEntity], list[HAArea]]:
    """Fetch device, entity, and area registries from HA."""
    return parse_registries(*await fetch_raw_registries(client))


async def fetch_span_trees(client: HAClient) -> list[SpanDeviceTree]:
    """Fetch registries and build SPAN device trees."""
    devices, entities, areas = await fetch_registries(client)
    # Filter entities to span_ebus platform
    span_entities = [e for e in entities if e.platform == DOMAIN]
    return _build_trees(devices, span_entities)


async def fetch_areas(client: HAClient) -> list[HAArea]:
    """Fetch area registry."""
    raw = await client.send_command("config/area_registry/list")
    return [_parse_area(a) for a in raw]


async def fetch_energy_prefs(client: HAClient) -> dict:
    """Fetch energy dashboard preferences.

    HA answers ``not_found`` ("No prefs") until the dashboard is first saved.
    """
    try:
        return await client.send_command("energy/get_prefs") or {}
    except HAClientError as err:
        if err.code == "not_found":
            return {}
        raise


async def fetch_entity_states(
    client: HAClient,
    entity_ids: set[str] | None = None,
) -> dict[str, dict]:
    """Fetch live entity states via get_states.

    Returns ``{entity_id: {"state": ..., "attributes": {...}}}``.
    If *entity_ids* is given, only those entities are included in the result.
    """
    raw_states: list[dict] = await client.send_command("get_states") or []
    result: dict[str, dict] = {}
    for entry in raw_states:
        eid = entry.get("entity_id", "")
        if entity_ids is not None and eid not in entity_ids:
            continue
        result[eid] = {
            "state": entry.get("state"),
            "attributes": entry.get("attributes", {}),
        }
    return result
