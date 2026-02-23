"""Tests for registry fetching and tree building."""

from __future__ import annotations

from hass_atlas.registry import (
    _build_trees,
    _is_span_device,
    _parse_area,
    _parse_device,
    _parse_entity,
)
from tests.conftest import (
    CIRCUIT_1_DEVICE_ID,
    CIRCUIT_2_DEVICE_ID,
    PANEL_DEVICE_ID,
    SERIAL,
    SITE_METER_DEVICE_ID,
)


def test_parse_device(raw_devices: list[dict]) -> None:
    device = _parse_device(raw_devices[0])
    assert device.id == PANEL_DEVICE_ID
    assert device.name == "SPAN Panel"
    assert device.identifiers == [("span_ebus", SERIAL)]
    assert device.via_device_id is None


def test_parse_entity(raw_entities: list[dict]) -> None:
    entity = _parse_entity(raw_entities[0])
    assert entity.entity_id == "sensor.span_site_imported_energy"
    assert entity.platform == "span_ebus"
    assert entity.device_class == "energy"
    assert entity.state_class == "total_increasing"


def test_parse_area(raw_areas: list[dict]) -> None:
    area = _parse_area(raw_areas[0])
    assert area.area_id == "area-kitchen"
    assert area.name == "Kitchen"


def test_is_span_device(raw_devices: list[dict]) -> None:
    span_dev = _parse_device(raw_devices[0])
    other_dev = _parse_device(raw_devices[4])  # Hue device
    assert _is_span_device(span_dev) is True
    assert _is_span_device(other_dev) is False


def test_build_trees(raw_devices: list[dict], raw_entities: list[dict]) -> None:
    devices = [_parse_device(d) for d in raw_devices]
    entities = [_parse_entity(e) for e in raw_entities if e["platform"] == "span_ebus"]
    trees = _build_trees(devices, entities)

    assert len(trees) == 1
    tree = trees[0]

    # Panel
    assert tree.panel.id == PANEL_DEVICE_ID
    assert tree.serial == SERIAL

    # Site metering
    assert tree.site_metering is not None
    assert tree.site_metering.id == SITE_METER_DEVICE_ID
    assert len(tree.site_metering.entities) == 2

    # Circuits
    assert len(tree.circuits) == 2
    circuit_ids = {c.id for c in tree.circuits}
    assert CIRCUIT_1_DEVICE_ID in circuit_ids
    assert CIRCUIT_2_DEVICE_ID in circuit_ids

    # Kitchen circuit has 3 entities (energy + energy_returned + power)
    kitchen = next(c for c in tree.circuits if c.id == CIRCUIT_1_DEVICE_ID)
    assert len(kitchen.entities) == 3

    # Non-SPAN entities not attached
    for device in [tree.panel, tree.site_metering] + tree.circuits:
        for entity in device.entities:
            assert entity.platform == "span_ebus"


def test_build_trees_no_span_devices() -> None:
    devices = [_parse_device({
        "id": "other", "name": "Other", "identifiers": [["hue", "123"]],
    })]
    trees = _build_trees(devices, [])
    assert trees == []


def test_circuit_area_preserved(raw_devices: list[dict], raw_entities: list[dict]) -> None:
    """Verify area_id from registry is preserved on parsed devices."""
    devices = [_parse_device(d) for d in raw_devices]
    entities = [_parse_entity(e) for e in raw_entities if e["platform"] == "span_ebus"]
    trees = _build_trees(devices, entities)

    kitchen = next(c for c in trees[0].circuits if c.id == CIRCUIT_1_DEVICE_ID)
    garage = next(c for c in trees[0].circuits if c.id == CIRCUIT_2_DEVICE_ID)
    assert kitchen.area_id == "area-kitchen"
    assert garage.area_id is None
