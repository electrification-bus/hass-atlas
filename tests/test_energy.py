"""Tests for the energy command logic."""

from __future__ import annotations

from hass_atlas.energy import (
    apply_topology_prefs,
    build_energy_config,
    extract_energy_entity_ids,
    find_stale_references,
    merge_prefs,
    remove_stale_references,
)
from hass_atlas.models import SpanDeviceTree
from hass_atlas.topology import EnergyRole, EnergyTopology


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

    # Circuit consumption (uses exported-energy = consumption in SPAN convention)
    stats = {d["stat_consumption"] for d in consumption}
    assert "sensor.span_kitchen_energy" in stats
    assert "sensor.span_garage_energy" in stats


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


# ---------------------------------------------------------------------------
# extract_energy_entity_ids
# ---------------------------------------------------------------------------


def test_extract_energy_entity_ids_all_sections() -> None:
    """Extract entity_ids from all sections of energy prefs."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
                "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
            },
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
            {
                "type": "battery",
                "stat_energy_from": "sensor.batt_discharge",
                "stat_energy_to": "sensor.batt_charge",
            },
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
            {"stat_consumption": "sensor.garage_energy"},
        ],
    }
    ids = extract_energy_entity_ids(prefs)
    assert ids == {
        "sensor.grid_import",
        "sensor.grid_export",
        "sensor.solar_energy",
        "sensor.batt_discharge",
        "sensor.batt_charge",
        "sensor.kitchen_energy",
        "sensor.garage_energy",
    }


def test_extract_energy_entity_ids_empty() -> None:
    """Empty prefs return empty set."""
    assert extract_energy_entity_ids({}) == set()


# ---------------------------------------------------------------------------
# find_stale_references
# ---------------------------------------------------------------------------


def test_find_stale_no_stale() -> None:
    """All references exist — no stale entries."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
        ],
    }
    all_ids = {"sensor.solar_energy", "sensor.kitchen_energy", "sensor.other"}
    assert find_stale_references(prefs, all_ids) == {}


def test_find_stale_deleted_integration() -> None:
    """Detect stale refs from a deleted integration (e.g. Flume)."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
            {"stat_consumption": "sensor.flume_water_usage"},
        ],
    }
    all_ids = {"sensor.solar_energy", "sensor.kitchen_energy"}
    stale = find_stale_references(prefs, all_ids)
    assert "device_consumption" in stale
    assert stale["device_consumption"] == ["sensor.flume_water_usage"]


def test_find_stale_grid_flow() -> None:
    """Detect stale grid flow references."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.grid_import"},
                    {"stat_energy_from": "sensor.old_grid_import"},
                ],
                "flow_to": [],
            },
        ],
        "device_consumption": [],
    }
    all_ids = {"sensor.grid_import"}
    stale = find_stale_references(prefs, all_ids)
    assert "grid (grid import)" in stale
    assert stale["grid (grid import)"] == ["sensor.old_grid_import"]


def test_find_stale_multiple_sections() -> None:
    """Stale entries across multiple sections."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.dead_solar"},
            {"type": "battery", "stat_energy_from": "sensor.batt_ok", "stat_energy_to": "sensor.dead_batt"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.dead_circuit"},
        ],
    }
    all_ids = {"sensor.batt_ok"}
    stale = find_stale_references(prefs, all_ids)
    assert len(stale) == 3
    assert stale["solar"] == ["sensor.dead_solar"]
    assert stale["battery"] == ["sensor.dead_batt"]
    assert stale["device_consumption"] == ["sensor.dead_circuit"]


# ---------------------------------------------------------------------------
# remove_stale_references
# ---------------------------------------------------------------------------


def test_remove_stale_device_consumption() -> None:
    """Remove stale device_consumption entries."""
    prefs = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
            {"stat_consumption": "sensor.flume_water"},
            {"stat_consumption": "sensor.garage_energy"},
        ],
    }
    cleaned = remove_stale_references(prefs, {"sensor.flume_water"})
    stats = [d["stat_consumption"] for d in cleaned["device_consumption"]]
    assert stats == ["sensor.kitchen_energy", "sensor.garage_energy"]


def test_remove_stale_solar_source() -> None:
    """Remove a solar source whose entity is stale."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.dead_solar"},
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid"}], "flow_to": []},
        ],
        "device_consumption": [],
    }
    cleaned = remove_stale_references(prefs, {"sensor.dead_solar"})
    assert len(cleaned["energy_sources"]) == 1
    assert cleaned["energy_sources"][0]["type"] == "grid"


def test_remove_stale_grid_flow() -> None:
    """Remove stale grid flow entry but keep the grid source."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.grid_import"},
                    {"stat_energy_from": "sensor.old_import"},
                ],
                "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
            },
        ],
        "device_consumption": [],
    }
    cleaned = remove_stale_references(prefs, {"sensor.old_import"})
    grid = cleaned["energy_sources"][0]
    assert len(grid["flow_from"]) == 1
    assert grid["flow_from"][0]["stat_energy_from"] == "sensor.grid_import"
    assert len(grid["flow_to"]) == 1  # untouched


def test_remove_stale_does_not_mutate_original() -> None:
    """Removal returns a new dict, original is unchanged."""
    prefs = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.stale"},
            {"stat_consumption": "sensor.ok"},
        ],
    }
    original_len = len(prefs["device_consumption"])
    cleaned = remove_stale_references(prefs, {"sensor.stale"})
    assert len(prefs["device_consumption"]) == original_len
    assert len(cleaned["device_consumption"]) == 1


# ---------------------------------------------------------------------------
# apply_topology_prefs
# ---------------------------------------------------------------------------


def _make_topo(
    preferred: list[EnergyRole] | None = None,
    skipped: list[EnergyRole] | None = None,
) -> EnergyTopology:
    """Helper to build a minimal EnergyTopology with given role assignments."""
    assignments = list(preferred or []) + list(skipped or [])
    return EnergyTopology(
        panels=[],
        integrations=[],
        circuit_roles=[],
        role_assignments=assignments,
        warnings=[],
    )


def test_apply_topology_removes_unwanted_consumption() -> None:
    """Return energy entries should be removed, wanted ones kept."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},  # wanted
            {"stat_consumption": "sensor.kitchen_energy_returned"},  # unwanted
            {"stat_consumption": "sensor.garage_energy"},  # wanted
            {"stat_consumption": "sensor.garage_energy_returned"},  # unwanted
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True, "ok"),
            EnergyRole("device_consumption", "sensor.garage_energy", "span_ebus", True, "ok"),
        ],
        skipped=[
            EnergyRole("device_consumption", "sensor.kitchen_energy_returned", "span_ebus", False, "CT noise"),
            EnergyRole("device_consumption", "sensor.garage_energy_returned", "span_ebus", False, "CT noise"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    stats = {d["stat_consumption"] for d in result["device_consumption"]}
    assert stats == {"sensor.kitchen_energy", "sensor.garage_energy"}


def test_apply_topology_preserves_user_consumption() -> None:
    """Non-SPAN entries not in wanted or skipped are preserved."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
            {"stat_consumption": "sensor.tasmota_desk"},  # user-added, not in topology
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True, "ok"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    stats = {d["stat_consumption"] for d in result["device_consumption"]}
    assert "sensor.tasmota_desk" in stats
    assert "sensor.kitchen_energy" in stats


def test_apply_topology_adds_missing_consumption() -> None:
    """New wanted entries are added if not present."""
    current = {
        "energy_sources": [],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True, "ok"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    assert len(result["device_consumption"]) == 1
    assert result["device_consumption"][0]["stat_consumption"] == "sensor.kitchen_energy"


def test_apply_topology_removes_skipped_sources() -> None:
    """Sources with entity_ids in the skipped set are removed."""
    current = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.enphase_production"},
            {"type": "solar", "stat_energy_from": "sensor.span_pv_returned"},
        ],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("solar", "sensor.span_pv_returned", "span_ebus", True, "PV IN_PANEL"),
        ],
        skipped=[
            EnergyRole("solar", "sensor.enphase_production", "enphase_envoy", False, "overlap"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    assert len(result["energy_sources"]) == 1
    assert result["energy_sources"][0]["stat_energy_from"] == "sensor.span_pv_returned"


def test_apply_topology_preserves_source_extra_fields() -> None:
    """Existing source objects keep their optional fields (stat_cost, etc.)."""
    current = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.pw_import", "stat_cost": "sensor.pw_cost"},
                ],
                "flow_to": [
                    {"stat_energy_to": "sensor.pw_export", "stat_compensation": None},
                ],
                "cost_adjustment_day": 0.0,
            },
        ],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("grid_import", "sensor.pw_import", "powerwall", True, "ok"),
            EnergyRole("grid_export", "sensor.pw_export", "powerwall", True, "ok"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    assert len(result["energy_sources"]) == 1
    grid = result["energy_sources"][0]
    # Extra fields preserved
    assert grid["cost_adjustment_day"] == 0.0
    assert grid["flow_from"][0]["stat_cost"] == "sensor.pw_cost"


def test_apply_topology_preserves_user_sources() -> None:
    """Non-grid/solar/battery sources the user configured are preserved."""
    current = {
        "energy_sources": [
            {"type": "gas", "stat_energy_from": "sensor.gas_meter"},
        ],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("grid_import", "sensor.pw_import", "powerwall", True, "ok"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    source_types = {s["type"] for s in result["energy_sources"]}
    assert "gas" in source_types
    assert "grid" in source_types


def test_apply_topology_does_not_mutate_current() -> None:
    """Original prefs dict is not modified."""
    current = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.old_solar"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.stale_returned"},
        ],
    }
    original_src_len = len(current["energy_sources"])
    original_con_len = len(current["device_consumption"])
    topo = _make_topo(
        skipped=[
            EnergyRole("solar", "sensor.old_solar", "enphase", False, "overlap"),
            EnergyRole("device_consumption", "sensor.stale_returned", "span_ebus", False, "CT noise"),
        ],
    )
    apply_topology_prefs(current, topo)
    assert len(current["energy_sources"]) == original_src_len
    assert len(current["device_consumption"]) == original_con_len


def test_apply_topology_preserves_extra_keys() -> None:
    """Extra top-level keys like device_consumption_water are preserved."""
    current = {
        "energy_sources": [],
        "device_consumption": [],
        "device_consumption_water": [{"stat_consumption": "sensor.water"}],
    }
    topo = _make_topo()
    result = apply_topology_prefs(current, topo)
    assert result["device_consumption_water"] == [{"stat_consumption": "sensor.water"}]


# ---------------------------------------------------------------------------
# included_in_stat (Sankey hierarchy)
# ---------------------------------------------------------------------------


def test_build_topology_config_included_in_stat() -> None:
    """build_topology_aware_config emits included_in_stat from parent_entity_id."""
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.panel_energy", "span_ebus", True,
                       "Panel total", parent_entity_id=None),
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", parent_entity_id="sensor.panel_energy"),
            EnergyRole("device_consumption", "sensor.garage_energy", "span_ebus", True,
                       "ok", parent_entity_id="sensor.panel_energy"),
        ],
    )
    from hass_atlas.energy import build_topology_aware_config
    config = build_topology_aware_config(topo)
    consumption = config["device_consumption"]

    panel = next(e for e in consumption if e["stat_consumption"] == "sensor.panel_energy")
    assert "included_in_stat" not in panel

    kitchen = next(e for e in consumption if e["stat_consumption"] == "sensor.kitchen_energy")
    assert kitchen["included_in_stat"] == "sensor.panel_energy"

    garage = next(e for e in consumption if e["stat_consumption"] == "sensor.garage_energy")
    assert garage["included_in_stat"] == "sensor.panel_energy"


def test_apply_topology_adds_included_in_stat() -> None:
    """Existing consumption entries get included_in_stat from topology."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.panel_energy", "span_ebus", True,
                       "Panel total", parent_entity_id=None),
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", parent_entity_id="sensor.panel_energy"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    kitchen = next(e for e in result["device_consumption"]
                   if e["stat_consumption"] == "sensor.kitchen_energy")
    assert kitchen["included_in_stat"] == "sensor.panel_energy"

    panel = next(e for e in result["device_consumption"]
                 if e["stat_consumption"] == "sensor.panel_energy")
    assert "included_in_stat" not in panel


def test_apply_topology_preserves_user_no_parent() -> None:
    """Non-SPAN user entries don't get included_in_stat."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.tasmota_desk"},
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", parent_entity_id="sensor.panel_energy"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    tasmota = next(e for e in result["device_consumption"]
                   if e["stat_consumption"] == "sensor.tasmota_desk")
    assert "included_in_stat" not in tasmota


# ---------------------------------------------------------------------------
# stat_rate (power sensors for Now tab)
# ---------------------------------------------------------------------------


def test_build_topology_config_stat_rate() -> None:
    """build_topology_aware_config emits stat_rate from rate_entity_id."""
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", rate_entity_id="sensor.kitchen_power"),
            EnergyRole("device_consumption", "sensor.garage_energy", "span_ebus", True,
                       "ok", rate_entity_id="sensor.garage_power"),
            EnergyRole("device_consumption", "sensor.tasmota_energy", "span_ebus", True,
                       "ok"),  # no power sensor
        ],
    )
    from hass_atlas.energy import build_topology_aware_config
    config = build_topology_aware_config(topo)
    consumption = config["device_consumption"]

    kitchen = next(e for e in consumption if e["stat_consumption"] == "sensor.kitchen_energy")
    assert kitchen["stat_rate"] == "sensor.kitchen_power"

    garage = next(e for e in consumption if e["stat_consumption"] == "sensor.garage_energy")
    assert garage["stat_rate"] == "sensor.garage_power"

    tasmota = next(e for e in consumption if e["stat_consumption"] == "sensor.tasmota_energy")
    assert "stat_rate" not in tasmota


def test_build_topology_config_solar_stat_rate() -> None:
    """build_topology_aware_config emits stat_rate on solar source."""
    topo = _make_topo(
        preferred=[
            EnergyRole("solar", "sensor.pv_energy", "span_ebus", True,
                       "PV IN_PANEL", rate_entity_id="sensor.pv_power"),
        ],
    )
    from hass_atlas.energy import build_topology_aware_config
    config = build_topology_aware_config(topo)
    solar = next(s for s in config["energy_sources"] if s["type"] == "solar")
    assert solar["stat_rate"] == "sensor.pv_power"


def test_build_topology_config_battery_stat_rate() -> None:
    """build_topology_aware_config emits stat_rate on battery source."""
    topo = _make_topo(
        preferred=[
            EnergyRole("battery_discharge", "sensor.batt_discharge", "span_ebus", True,
                       "ok", rate_entity_id="sensor.batt_power"),
            EnergyRole("battery_charge", "sensor.batt_charge", "span_ebus", True,
                       "ok", rate_entity_id="sensor.batt_power"),
        ],
    )
    from hass_atlas.energy import build_topology_aware_config
    config = build_topology_aware_config(topo)
    battery = next(s for s in config["energy_sources"] if s["type"] == "battery")
    assert battery["stat_rate"] == "sensor.batt_power"


def test_apply_topology_adds_stat_rate() -> None:
    """Existing consumption entries get stat_rate from topology."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", rate_entity_id="sensor.kitchen_power"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    kitchen = next(e for e in result["device_consumption"]
                   if e["stat_consumption"] == "sensor.kitchen_energy")
    assert kitchen["stat_rate"] == "sensor.kitchen_power"


def test_apply_topology_preserves_user_stat_rate() -> None:
    """User-configured entries keep their own stat_rate untouched."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.tasmota_energy", "stat_rate": "sensor.tasmota_power"},
        ],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", rate_entity_id="sensor.kitchen_power"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    tasmota = next(e for e in result["device_consumption"]
                   if e["stat_consumption"] == "sensor.tasmota_energy")
    assert tasmota["stat_rate"] == "sensor.tasmota_power"


def test_apply_topology_new_entries_get_stat_rate() -> None:
    """Newly added consumption entries include stat_rate."""
    current = {
        "energy_sources": [],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", rate_entity_id="sensor.kitchen_power"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    kitchen = result["device_consumption"][0]
    assert kitchen["stat_consumption"] == "sensor.kitchen_energy"
    assert kitchen["stat_rate"] == "sensor.kitchen_power"
