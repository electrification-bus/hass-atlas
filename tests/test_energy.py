"""Tests for the energy command logic."""

from __future__ import annotations


from hass_atlas.energy import (
    apply_remap_to_prefs,
    apply_topology_prefs,
    build_energy_config,
    extract_energy_entity_ids,
    find_stale_references,
    merge_prefs,
    remap_stale_references,
    remove_stale_references,
)
from hass_atlas.models import SpanDeviceTree
from hass_atlas.topology import EnergyRole, EnergyTopology

from tests.conftest import make_panel_tree_with_lugs


def test_build_energy_config_grid_and_circuits(span_tree: SpanDeviceTree) -> None:
    """Non-topology path emits grid (from lugs_up) + one consumption entry per circuit.

    In span-hass 0.2.0 the BESS device only exposes ``info`` and ``soc``
    capabilities, and the PV device only exposes ``info`` — neither has the
    ``meter`` capability the legacy ``build_energy_config`` looks for.
    The non-topology path therefore proposes grid + circuit consumption only;
    solar / battery sources are surfaced by the ``--topology`` path instead,
    which routes them to the dedicated DER integration (Powerwall, Enphase).
    """
    config = build_energy_config([span_tree])
    sources = config["energy_sources"]
    consumption = config["device_consumption"]

    # Grid source — single, from tree.lugs_up
    grid = next(s for s in sources if s["type"] == "grid")
    assert len(grid["flow_from"]) == 1
    grid_import_eid = grid["flow_from"][0]["stat_energy_from"]
    assert "lugs" in grid_import_eid and "imported_energy" in grid_import_eid

    assert len(grid["flow_to"]) == 1
    grid_export_eid = grid["flow_to"][0]["stat_energy_to"]
    assert "lugs" in grid_export_eid and "exported_energy" in grid_export_eid

    # No solar / battery in the non-topology path (see docstring).
    assert all(s["type"] != "solar" for s in sources)
    assert all(s["type"] != "battery" for s in sources)

    # One consumption entry per circuit.
    assert len(consumption) == len(span_tree.circuits)


def test_build_energy_config_def34_sub_panel(span_tree_def34: SpanDeviceTree) -> None:
    """Same shape on the sub-panel fixture (19 circuits, includes a PV device)."""
    config = build_energy_config([span_tree_def34])
    sources = config["energy_sources"]

    # Even though def34 has both BESS and PV devices, the non-topology path
    # only proposes grid (BESS/PV have no meter capability in 0.2.0).
    assert {s["type"] for s in sources} == {"grid"}
    assert len(config["device_consumption"]) == 19


def test_build_energy_config_no_lugs_up(span_tree: SpanDeviceTree) -> None:
    """Without an upstream-lugs device, no grid source is proposed."""
    span_tree.lugs_up = None
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


def test_apply_topology_updates_source_stat_rate() -> None:
    """Existing solar source gets stat_rate updated when topology proposes a new one."""
    current = {
        "energy_sources": [
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv_energy",
                "stat_rate": "sensor.pv_active_power",
                "stat_cost": {"entity_id": "sensor.energy_cost"},
            },
        ],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("solar", "sensor.pv_energy", "span_ebus", True,
                       "PV IN_PANEL", rate_entity_id="sensor.pv_generation_power"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    solar = next(s for s in result["energy_sources"] if s["type"] == "solar")
    # stat_rate updated to generation-power
    assert solar["stat_rate"] == "sensor.pv_generation_power"
    # User fields preserved
    assert solar["stat_cost"] == {"entity_id": "sensor.energy_cost"}


def test_apply_topology_adds_panel_entries_for_all_panels() -> None:
    """When topology proposes 3 panel entries and only 1 exists, the other 2 are added."""
    current = {
        "energy_sources": [],
        "device_consumption": [
            # Only lead panel exists; sub-panels missing
            {"stat_consumption": "sensor.lead_upstream_energy"},
            # Some circuits already present
            {"stat_consumption": "sensor.kitchen_energy",
             "included_in_stat": "sensor.lead_upstream_energy"},
        ],
    }
    topo = _make_topo(
        preferred=[
            # 3 panel-level entries
            EnergyRole("device_consumption", "sensor.lead_upstream_energy", "span_ebus", True,
                       "Panel total energy — Sankey hierarchy parent",
                       parent_entity_id=None,
                       rate_entity_id="sensor.lead_upstream_power"),
            EnergyRole("device_consumption", "sensor.mid_upstream_energy", "span_ebus", True,
                       "Panel total energy — Sankey hierarchy parent",
                       parent_entity_id="sensor.lead_upstream_energy",
                       rate_entity_id="sensor.mid_upstream_power"),
            EnergyRole("device_consumption", "sensor.tail_upstream_energy", "span_ebus", True,
                       "Panel total energy — Sankey hierarchy parent",
                       parent_entity_id="sensor.mid_upstream_energy",
                       rate_entity_id="sensor.tail_upstream_power"),
            # Circuit
            EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True,
                       "ok", parent_entity_id="sensor.lead_upstream_energy"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    stats = {d["stat_consumption"] for d in result["device_consumption"]}

    # All 3 panel entries present
    assert "sensor.lead_upstream_energy" in stats
    assert "sensor.mid_upstream_energy" in stats
    assert "sensor.tail_upstream_energy" in stats

    # Mid and tail have correct included_in_stat and stat_rate
    mid = next(e for e in result["device_consumption"]
               if e["stat_consumption"] == "sensor.mid_upstream_energy")
    assert mid["included_in_stat"] == "sensor.lead_upstream_energy"
    assert mid["stat_rate"] == "sensor.mid_upstream_power"

    tail = next(e for e in result["device_consumption"]
                if e["stat_consumption"] == "sensor.tail_upstream_energy")
    assert tail["included_in_stat"] == "sensor.mid_upstream_energy"
    assert tail["stat_rate"] == "sensor.tail_upstream_power"

    # Lead panel has no included_in_stat
    lead = next(e for e in result["device_consumption"]
                if e["stat_consumption"] == "sensor.lead_upstream_energy")
    assert "included_in_stat" not in lead

    # Circuit still present
    assert "sensor.kitchen_energy" in stats


def test_apply_topology_preserves_source_stat_rate_when_same() -> None:
    """Existing source stat_rate is unchanged when topology matches."""
    current = {
        "energy_sources": [
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv_energy",
                "stat_rate": "sensor.pv_generation_power",
            },
        ],
        "device_consumption": [],
    }
    topo = _make_topo(
        preferred=[
            EnergyRole("solar", "sensor.pv_energy", "span_ebus", True,
                       "PV IN_PANEL", rate_entity_id="sensor.pv_generation_power"),
        ],
    )
    result = apply_topology_prefs(current, topo)
    solar = result["energy_sources"][0]
    assert solar["stat_rate"] == "sensor.pv_generation_power"
    # Original not mutated
    assert current["energy_sources"][0]["stat_rate"] == "sensor.pv_generation_power"


# ---------------------------------------------------------------------------
# remap_stale_references — SHT-58b orphan cleanup
# ---------------------------------------------------------------------------


def test_remap_finds_underscore_2_sibling() -> None:
    """Stale ref with a _2 sibling in the registry is remapped to it."""
    tree = make_panel_tree_with_lugs("nt-0000-tst1", num_circuits=1)
    stale = {"sensor.smoke_detectors_energy"}
    all_ids = {"sensor.smoke_detectors_energy_2", "sensor.other_energy"}
    remap, drop = remap_stale_references(stale, all_ids, [tree])

    assert remap == {"sensor.smoke_detectors_energy": "sensor.smoke_detectors_energy_2"}
    assert drop == []


def test_remap_panel_upstream_to_lugs() -> None:
    """Old flat-model panel-upstream entity_id is remapped to the live Lugs entity_id."""
    tree = make_panel_tree_with_lugs("nt-2026-c192x", num_circuits=1)
    stale = {
        "sensor.span_panel_nt_2026_c192x_upstream_imported_energy",
        "sensor.span_panel_nt_2026_c192x_upstream_exported_energy",
        "sensor.span_panel_nt_2026_c192x_upstream_active_power",
    }
    # Build the all_entity_ids set from the synthetic tree's lugs_up entities
    all_ids = {e.entity_id for e in tree.lugs_up.entities}
    remap, drop = remap_stale_references(stale, all_ids, [tree])

    assert drop == []
    assert remap["sensor.span_panel_nt_2026_c192x_upstream_imported_energy"].endswith("_imported_energy")
    assert remap["sensor.span_panel_nt_2026_c192x_upstream_exported_energy"].endswith("_exported_energy")
    assert remap["sensor.span_panel_nt_2026_c192x_upstream_active_power"].endswith("_active_power")


def test_remap_unmatchable_goes_to_drop() -> None:
    """Stale refs with no live equivalent end up in the drop list."""
    tree = make_panel_tree_with_lugs("nt-0000-tst1", num_circuits=1)
    stale = {
        "sensor.span_panel_nt_2143_c1akc_circuit_f71a4c_energy_exported",
        "sensor.totally_unknown_thing",
    }
    all_ids = {"sensor.unrelated_entity"}
    remap, drop = remap_stale_references(stale, all_ids, [tree])

    assert remap == {}
    assert set(drop) == stale


def test_remap_panel_upstream_skips_wrong_panel() -> None:
    """A stale ref pointing at panel X is not remapped to panel Y's Lugs entity."""
    panel_x = make_panel_tree_with_lugs("nt-2026-c192x", num_circuits=1)
    # Stale ref references c192x but we only have panel_y in the tree set
    panel_y = make_panel_tree_with_lugs("nt-2143-c1akc", num_circuits=1)
    stale = {"sensor.span_panel_nt_2026_c192x_upstream_imported_energy"}
    all_ids = {e.entity_id for e in panel_y.lugs_up.entities}
    remap, drop = remap_stale_references(stale, all_ids, [panel_y])

    assert remap == {}  # no panel_x in tree set
    assert drop == sorted(stale)

    # But succeeds when panel_x IS in the tree set
    all_ids_x = {e.entity_id for e in panel_x.lugs_up.entities}
    remap2, drop2 = remap_stale_references(stale, all_ids_x, [panel_x])
    assert remap2 != {} and drop2 == []


def test_apply_remap_rewrites_all_reference_fields() -> None:
    """apply_remap_to_prefs covers sources, flows, included_in_stat, and stat_rate."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.old_grid_import"}],
                "flow_to": [{"stat_energy_to": "sensor.old_grid_export"}],
                "stat_rate": "sensor.old_grid_now",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.old_solar",
                "stat_rate": "sensor.old_solar_power",
            },
        ],
        "device_consumption": [
            {
                "stat_consumption": "sensor.old_kitchen",
                "included_in_stat": "sensor.old_panel",
                "stat_rate": "sensor.old_kitchen_power",
            },
            {"stat_consumption": "sensor.unchanged"},
        ],
    }
    remap = {
        "sensor.old_grid_import": "sensor.new_grid_import",
        "sensor.old_grid_export": "sensor.new_grid_export",
        "sensor.old_grid_now": "sensor.new_grid_now",
        "sensor.old_solar": "sensor.new_solar",
        "sensor.old_solar_power": "sensor.new_solar_power",
        "sensor.old_kitchen": "sensor.new_kitchen",
        "sensor.old_panel": "sensor.new_panel",
        "sensor.old_kitchen_power": "sensor.new_kitchen_power",
    }
    cleaned = apply_remap_to_prefs(prefs, remap)

    grid = cleaned["energy_sources"][0]
    assert grid["flow_from"][0]["stat_energy_from"] == "sensor.new_grid_import"
    assert grid["flow_to"][0]["stat_energy_to"] == "sensor.new_grid_export"
    assert grid["stat_rate"] == "sensor.new_grid_now"

    solar = cleaned["energy_sources"][1]
    assert solar["stat_energy_from"] == "sensor.new_solar"
    assert solar["stat_rate"] == "sensor.new_solar_power"

    kitchen = cleaned["device_consumption"][0]
    assert kitchen["stat_consumption"] == "sensor.new_kitchen"
    assert kitchen["included_in_stat"] == "sensor.new_panel"
    assert kitchen["stat_rate"] == "sensor.new_kitchen_power"

    # Unchanged entry pass-through
    assert cleaned["device_consumption"][1]["stat_consumption"] == "sensor.unchanged"

    # Original prefs not mutated
    assert prefs["energy_sources"][0]["flow_from"][0]["stat_energy_from"] == "sensor.old_grid_import"


def test_find_stale_includes_included_in_stat_and_stat_rate() -> None:
    """find_stale_references catches stale included_in_stat and stat_rate refs."""
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar", "stat_rate": "sensor.dead_rate"},
        ],
        "device_consumption": [
            {
                "stat_consumption": "sensor.kitchen",
                "included_in_stat": "sensor.dead_parent",
                "stat_rate": "sensor.dead_circuit_rate",
            },
        ],
    }
    all_ids = {"sensor.solar", "sensor.kitchen"}
    stale = find_stale_references(prefs, all_ids)

    assert "sensor.dead_rate" in stale.get("solar (stat_rate)", [])
    assert "sensor.dead_parent" in stale.get("device_consumption (included_in_stat)", [])
    assert "sensor.dead_circuit_rate" in stale.get("device_consumption (stat_rate)", [])


def test_remove_stale_clears_included_in_stat_keeps_entry() -> None:
    """Stale included_in_stat is cleared but the device_consumption entry stays."""
    prefs = {
        "energy_sources": [],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen", "included_in_stat": "sensor.dead_parent"},
        ],
    }
    cleaned = remove_stale_references(prefs, {"sensor.dead_parent"})

    assert len(cleaned["device_consumption"]) == 1  # entry preserved
    assert cleaned["device_consumption"][0]["stat_consumption"] == "sensor.kitchen"
    assert "included_in_stat" not in cleaned["device_consumption"][0]


def test_apply_remap_dedupes_device_consumption() -> None:
    """If remap causes two entries to point at the same stat_consumption, dedupe."""
    prefs = {
        "energy_sources": [],
        "device_consumption": [
            # This entry will get its stat_consumption rewritten to one that
            # already exists below — should be dropped after dedupe.
            {
                "stat_consumption": "sensor.old_panel_imported",
                "included_in_stat": "sensor.parent",
                "stat_rate": "sensor.old_panel_power",
            },
            # Pre-existing entry with the post-remap target stat_consumption.
            {
                "stat_consumption": "sensor.new_panel_imported",
                "included_in_stat": "sensor.parent",
                "stat_rate": "sensor.new_panel_power",
            },
            # A circuit entry that's unaffected.
            {"stat_consumption": "sensor.kitchen", "included_in_stat": "sensor.parent"},
        ],
    }
    remap = {"sensor.old_panel_imported": "sensor.new_panel_imported"}
    cleaned = apply_remap_to_prefs(prefs, remap)

    stats = [d["stat_consumption"] for d in cleaned["device_consumption"]]
    # No duplicates
    assert len(stats) == len(set(stats))
    # First-occurrence wins — the OLD entry (rewritten) is kept; the original
    # second entry is dropped because its stat now collides.
    new_panel_entry = next(d for d in cleaned["device_consumption"]
                           if d["stat_consumption"] == "sensor.new_panel_imported")
    assert new_panel_entry["stat_rate"] == "sensor.old_panel_power"  # kept from first
    # Unaffected entry survives
    assert any(d["stat_consumption"] == "sensor.kitchen" for d in cleaned["device_consumption"])
