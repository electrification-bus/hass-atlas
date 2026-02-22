"""Tests for the energy command logic."""

from __future__ import annotations

from ha_atlas.energy import build_energy_config, merge_prefs
from ha_atlas.models import SpanDeviceTree


def test_build_energy_config_full(span_tree: SpanDeviceTree) -> None:
    """Full tree with site metering, solar, battery, circuits."""
    config = build_energy_config([span_tree])

    sources = config["energy_sources"]
    consumption = config["device_consumption"]

    # Grid source from site metering
    grid = next(s for s in sources if s["type"] == "grid")
    assert len(grid["flow_from"]) == 1
    assert grid["flow_from"][0]["stat_energy_from"] == "sensor.span_site_imported_energy"
    assert len(grid["flow_to"]) == 1
    assert grid["flow_to"][0]["stat_energy_to"] == "sensor.span_site_exported_energy"

    # Solar source
    solar = next(s for s in sources if s["type"] == "solar")
    assert solar["stat_energy_from"] == "sensor.span_solar_imported_energy"

    # Battery source
    battery = next(s for s in sources if s["type"] == "battery")
    assert battery["stat_energy_from"] == "sensor.span_battery_imported_energy"
    assert battery["stat_energy_to"] == "sensor.span_battery_exported_energy"

    # Circuit consumption
    stats = {d["stat_consumption"] for d in consumption}
    assert "sensor.span_kitchen_imported_energy" in stats
    assert "sensor.span_garage_imported_energy" in stats


def test_build_energy_config_grid_only(span_tree: SpanDeviceTree) -> None:
    """Tree with only site metering and circuits, no solar/battery."""
    span_tree.solar = None
    span_tree.battery = None
    config = build_energy_config([span_tree])

    source_types = {s["type"] for s in config["energy_sources"]}
    assert "grid" in source_types
    assert "solar" not in source_types
    assert "battery" not in source_types
    assert len(config["device_consumption"]) == 2


def test_build_energy_config_no_site_meter(span_tree: SpanDeviceTree) -> None:
    """Without site metering, fall back to panel for grid."""
    span_tree.site_metering = None
    # Panel has no energy entities by default, so no grid source
    config = build_energy_config([span_tree])
    grid_sources = [s for s in config["energy_sources"] if s["type"] == "grid"]
    assert len(grid_sources) == 0


def test_merge_prefs_empty_current() -> None:
    """Merging into empty prefs should produce proposed config."""
    proposed = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen"},
        ],
    }
    merged = merge_prefs({}, proposed)
    assert len(merged["energy_sources"]) == 1
    assert len(merged["device_consumption"]) == 1


def test_merge_prefs_no_duplicates() -> None:
    """Don't add entries that already exist."""
    current = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen"},
        ],
    }
    proposed = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
            {"type": "solar", "stat_energy_from": "sensor.solar"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen"},
            {"stat_consumption": "sensor.garage"},
        ],
    }
    merged = merge_prefs(current, proposed)
    assert len(merged["energy_sources"]) == 2  # grid (existing) + solar (new)
    assert len(merged["device_consumption"]) == 2  # kitchen (existing) + garage (new)


def test_merge_prefs_preserves_user_config() -> None:
    """User-configured sources (gas, water) should not be removed."""
    current = {
        "energy_sources": [
            {"type": "gas", "stat_energy_from": "sensor.gas_meter"},
        ],
        "device_consumption": [],
    }
    proposed = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen"},
        ],
    }
    merged = merge_prefs(current, proposed)
    source_types = {s["type"] for s in merged["energy_sources"]}
    assert "gas" in source_types  # preserved
    assert "grid" in source_types  # added
    assert len(merged["device_consumption"]) == 1


def test_merge_prefs_deep_copy() -> None:
    """Merging should not mutate the current prefs dict."""
    current = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
        ],
        "device_consumption": [],
    }
    proposed = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen"},
        ],
    }
    original_len = len(current["energy_sources"])
    merge_prefs(current, proposed)
    assert len(current["energy_sources"]) == original_len  # not mutated
