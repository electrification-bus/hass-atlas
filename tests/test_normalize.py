"""Tests for the normalize command — entity ID renaming."""

from __future__ import annotations

import pytest

from ha_atlas.models import HADevice, HAEntity, SpanDeviceTree
from ha_atlas.normalize import _compute_renames, _expected_entity_id, _update_energy_prefs, slugify


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Server-Rack-1 & Spare", "server_rack_1_spare"),
        ("Kitchen", "kitchen"),
        ("Attic-Back-1 & Spare", "attic_back_1_spare"),
        ("Cooktop/Stove", "cooktop_stove"),
        ("Commissioned PV System", "commissioned_pv_system"),
        ("EV-Charger-Circuit-1", "ev_charger_circuit_1"),
        ("Battery Storage", "battery_storage"),
        ("Circuit 050299", "circuit_050299"),
        ("SPAN Panel nt-2026-c192x", "span_panel_nt_2026_c192x"),
        ("  Extra  Spaces  ", "extra_spaces"),
        ("café", "cafe"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


# ---------------------------------------------------------------------------
# _expected_entity_id
# ---------------------------------------------------------------------------

def _make_device(name: str, name_by_user: str | None = None) -> HADevice:
    return HADevice(
        id="dev-001",
        name=name,
        name_by_user=name_by_user,
        model="Circuit",
        identifiers=[("span_ebus", "serial_node")],
    )


def _make_entity(
    entity_id: str,
    original_name: str | None = "Power",
    has_entity_name: bool = True,
    device_id: str = "dev-001",
) -> HAEntity:
    return HAEntity(
        entity_id=entity_id,
        unique_id=f"serial_{entity_id}",
        platform="span_ebus",
        device_id=device_id,
        original_name=original_name,
        has_entity_name=has_entity_name,
    )


def test_expected_entity_id_basic() -> None:
    device = _make_device("Server-Rack-1 & Spare")
    entity = _make_entity("sensor.circuit_050299_power", original_name="Power")
    assert _expected_entity_id("sensor", device, entity) == "sensor.server_rack_1_spare_power"


def test_expected_entity_id_name_by_user() -> None:
    """name_by_user overrides the integration-provided name."""
    device = _make_device("Server-Rack-1 & Spare", name_by_user="My Server Rack")
    entity = _make_entity("sensor.circuit_050299_power", original_name="Power")
    assert _expected_entity_id("sensor", device, entity) == "sensor.my_server_rack_power"


def test_expected_entity_id_no_entity_name() -> None:
    """Entity with no original_name — just device name."""
    device = _make_device("Kitchen")
    entity = _make_entity("sensor.circuit_abc123", original_name=None)
    assert _expected_entity_id("sensor", device, entity) == "sensor.kitchen"


# ---------------------------------------------------------------------------
# _compute_renames
# ---------------------------------------------------------------------------

def _make_tree(devices: list[HADevice]) -> SpanDeviceTree:
    panel = HADevice(
        id="dev-panel",
        name="SPAN Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", "serial")],
        children=devices,
    )
    return SpanDeviceTree(panel=panel, circuits=devices)


def test_compute_renames_basic() -> None:
    """Entities with fallback names get renamed to match device name."""
    device = _make_device("Kitchen")
    device.entities = [
        _make_entity("sensor.circuit_abc123_power", original_name="Power"),
        _make_entity("switch.circuit_abc123_relay", original_name="Relay"),
    ]
    trees = [_make_tree([device])]
    all_ids = {"sensor.circuit_abc123_power", "switch.circuit_abc123_relay"}

    renames = _compute_renames(trees, all_ids)

    assert len(renames) == 2
    rename_map = {e.entity_id: new_id for _, e, new_id in renames}
    assert rename_map["sensor.circuit_abc123_power"] == "sensor.kitchen_power"
    assert rename_map["switch.circuit_abc123_relay"] == "switch.kitchen_relay"


def test_compute_renames_already_correct() -> None:
    """Entities already matching device name → no renames."""
    device = _make_device("Kitchen")
    device.entities = [
        _make_entity("sensor.kitchen_power", original_name="Power"),
    ]
    trees = [_make_tree([device])]
    all_ids = {"sensor.kitchen_power"}

    renames = _compute_renames(trees, all_ids)
    assert len(renames) == 0


def test_compute_renames_collision_with_existing() -> None:
    """Skip rename if target entity_id already exists (non-SPAN entity)."""
    device = _make_device("Kitchen")
    device.entities = [
        _make_entity("sensor.circuit_abc123_power", original_name="Power"),
    ]
    trees = [_make_tree([device])]
    # kitchen_power already exists from another integration
    all_ids = {"sensor.circuit_abc123_power", "sensor.kitchen_power"}

    renames = _compute_renames(trees, all_ids)
    assert len(renames) == 0


def test_compute_renames_collision_vacated() -> None:
    """Allow rename if target entity_id is being vacated by another rename."""
    device1 = _make_device("Kitchen")
    device1.id = "dev-001"
    device1.entities = [
        _make_entity("sensor.garage_power", original_name="Power", device_id="dev-001"),
    ]
    device2 = HADevice(
        id="dev-002",
        name="Garage",
        model="Circuit",
        identifiers=[("span_ebus", "serial_node2")],
    )
    device2.entities = [
        _make_entity("sensor.kitchen_power", original_name="Power", device_id="dev-002"),
    ]
    trees = [_make_tree([device1, device2])]
    all_ids = {"sensor.garage_power", "sensor.kitchen_power"}

    renames = _compute_renames(trees, all_ids)
    # Both should be renamed (swapping names)
    assert len(renames) == 2


def test_compute_renames_skips_no_entity_name() -> None:
    """Entities without has_entity_name are skipped."""
    device = _make_device("Kitchen")
    device.entities = [
        _make_entity(
            "sensor.circuit_abc123_power",
            original_name="Power",
            has_entity_name=False,
        ),
    ]
    trees = [_make_tree([device])]
    all_ids = {"sensor.circuit_abc123_power"}

    renames = _compute_renames(trees, all_ids)
    assert len(renames) == 0


# ---------------------------------------------------------------------------
# _update_energy_prefs
# ---------------------------------------------------------------------------

def test_update_energy_prefs_replaces_refs() -> None:
    """Stale entity_ids in energy prefs are replaced."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.circuit_abc_energy"}],
                "flow_to": [{"stat_energy_to": "sensor.circuit_abc_energy_returned"}],
            }
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.circuit_abc_energy"},
            {"stat_consumption": "sensor.already_correct"},
        ],
    }
    rename_map = {
        "sensor.circuit_abc_energy": "sensor.kitchen_energy",
        "sensor.circuit_abc_energy_returned": "sensor.kitchen_energy_returned",
    }
    new_prefs, count = _update_energy_prefs(prefs, rename_map)
    assert count == 2
    assert new_prefs["device_consumption"][0]["stat_consumption"] == "sensor.kitchen_energy"
    assert new_prefs["device_consumption"][1]["stat_consumption"] == "sensor.already_correct"
    assert new_prefs["energy_sources"][0]["flow_from"][0]["stat_energy_from"] == "sensor.kitchen_energy"
    assert new_prefs["energy_sources"][0]["flow_to"][0]["stat_energy_to"] == "sensor.kitchen_energy_returned"


def test_update_energy_prefs_no_changes() -> None:
    """No stale refs → zero count, prefs unchanged."""
    prefs = {"device_consumption": [{"stat_consumption": "sensor.kitchen_energy"}]}
    rename_map = {"sensor.circuit_xyz_power": "sensor.garage_power"}
    new_prefs, count = _update_energy_prefs(prefs, rename_map)
    assert count == 0
    assert new_prefs == prefs
