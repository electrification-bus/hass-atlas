"""Fetch and parse HA device/entity/area registries into SpanDeviceTree."""

from __future__ import annotations

from typing import Any

from ha_atlas.ha_client import HAClient
from ha_atlas.models import HAArea, HADevice, HAEntity, SpanDeviceTree

DOMAIN = "span_ebus"

# Model strings used to classify child devices
MODEL_CIRCUIT = "Circuit"
MODEL_BATTERY = "Battery Storage"
MODEL_SOLAR = "Solar PV"
MODEL_EV_CHARGER = "EV Charger"
MODEL_SITE_METERING = "Site Metering"


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

    # Find panel devices (no via_device_id or via_device_id not in span_devices)
    panels: list[HADevice] = []
    children_by_parent: dict[str, list[HADevice]] = {}
    for device in span_devices.values():
        if device.via_device_id and device.via_device_id in span_devices:
            children_by_parent.setdefault(device.via_device_id, []).append(device)
        else:
            panels.append(device)

    # Build trees
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
            elif model == MODEL_SITE_METERING:
                tree.site_metering = child
            else:
                # Unknown child type — treat as circuit
                tree.circuits.append(child)
        trees.append(tree)

    return trees


async def fetch_registries(client: HAClient) -> tuple[list[HADevice], list[HAEntity], list[HAArea]]:
    """Fetch device, entity, and area registries from HA."""
    raw_devices = await client.send_command("config/device_registry/list")
    raw_entities = await client.send_command("config/entity_registry/list")
    raw_areas = await client.send_command("config/area_registry/list")

    devices = [_parse_device(d) for d in raw_devices]
    entities = [_parse_entity(e) for e in raw_entities]
    areas = [_parse_area(a) for a in raw_areas]

    return devices, entities, areas


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
    """Fetch energy dashboard preferences."""
    return await client.send_command("energy/get_prefs") or {}
