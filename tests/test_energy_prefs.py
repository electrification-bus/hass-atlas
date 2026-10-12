"""Grid sources in Home Assistant's unified (2026.3+) and legacy energy prefs formats."""

from __future__ import annotations

import copy

import pytest

from hass_atlas.energy import (
    apply_remap_to_prefs,
    apply_topology_prefs,
    build_topology_aware_config,
    extract_energy_entity_ids,
    find_stale_references,
    merge_prefs,
    remove_stale_references,
)
from hass_atlas.energy_prefs import (
    LEGACY,
    UNIFIED,
    GridConnection,
    convert_sources,
    grid_format,
    legacy_to_unified,
    merge_grid_sources,
)
from hass_atlas.topology import EnergyRole, EnergyTopology
from tests._ha_energy_schema import required_key_errors

IMP, EXP = "sensor.lugs_imported_energy", "sensor.lugs_exported_energy"


def _topo(*assignments: EnergyRole) -> EnergyTopology:
    return EnergyTopology(
        panels=[], integrations=[], circuit_roles=[], role_assignments=list(assignments),
        warnings=[],
    )


def _grid_topo(rate: str | None = None) -> EnergyTopology:
    return _topo(
        EnergyRole("grid_import", IMP, "span_ebus", True, "ok", rate_entity_id=rate,
                   grid_connection="p1"),
        EnergyRole("grid_export", EXP, "span_ebus", True, "ok", grid_connection="p1"),
        EnergyRole("device_consumption", "sensor.kitchen_energy", "span_ebus", True, "ok"),
    )


def _flow_from(stat: str, **costs: object) -> dict:
    """A legacy flow_from entry as HA stores it, with every price key."""
    return {"stat_energy_from": stat, "stat_cost": None, "entity_energy_price": None,
            "number_energy_price": None, **costs}


def _legacy(flow_from: list[dict], flow_to: list[dict], **extra: object) -> dict:
    return {"type": "grid", "flow_from": flow_from, "flow_to": flow_to,
            "cost_adjustment_day": 0.0, **extra}


# ---------------------------------------------------------------------------
# Format choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2026.9.4", UNIFIED),
        ("2026.3.0", UNIFIED),
        ("2026.3.0b1", UNIFIED),
        ("2026.10.0", UNIFIED),
        ("2026.2.3", LEGACY),
        ("2025.12.1", LEGACY),
    ],
)
def test_grid_format_from_version(version: str, expected: str) -> None:
    assert grid_format(version) == expected


def test_grid_format_version_wins_over_shape() -> None:
    legacy_prefs = {"energy_sources": [_legacy([{"stat_energy_from": IMP}], [])]}
    assert grid_format("2026.9.4", legacy_prefs) == UNIFIED


def test_grid_format_from_shape_without_version() -> None:
    legacy_prefs = {"energy_sources": [_legacy([{"stat_energy_from": IMP}], [])]}
    unified_prefs = {"energy_sources": [
        {"type": "grid", "stat_energy_from": IMP, "cost_adjustment_day": 0.0}
    ]}
    assert grid_format(None, legacy_prefs) == LEGACY
    assert grid_format("dev", unified_prefs) == UNIFIED
    assert grid_format(None, {}) == UNIFIED
    assert grid_format(None, {"energy_sources": [{"type": "solar", "stat_energy_from": "x"}]}) == UNIFIED


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def test_build_config_unified() -> None:
    config = build_topology_aware_config(_grid_topo(rate="sensor.lugs_power"))
    assert config["energy_sources"] == [{
        "type": "grid",
        "stat_energy_from": IMP,
        "stat_energy_to": EXP,
        "stat_rate": "sensor.lugs_power",
        "cost_adjustment_day": 0.0,
    }]
    assert required_key_errors(config, legacy=False) == []


def test_build_config_legacy() -> None:
    config = build_topology_aware_config(_grid_topo(rate="sensor.lugs_power"), LEGACY)
    assert config["energy_sources"] == [_legacy(
        [_flow_from(IMP)], [{"stat_energy_to": EXP}]
    )]
    assert required_key_errors(config, legacy=True) == []


def test_build_config_one_unified_source_per_connection() -> None:
    topo = _topo(
        EnergyRole("grid_import", "sensor.a_imp", "span_ebus", True, "", grid_connection="a"),
        EnergyRole("grid_export", "sensor.a_exp", "span_ebus", True, "", grid_connection="a"),
        EnergyRole("grid_import", "sensor.b_imp", "span_ebus", True, "", grid_connection="b"),
    )
    unified = build_topology_aware_config(topo)["energy_sources"]
    assert [(s.get("stat_energy_from"), s.get("stat_energy_to")) for s in unified] == [
        ("sensor.a_imp", "sensor.a_exp"), ("sensor.b_imp", None),
    ]
    legacy = build_topology_aware_config(topo, LEGACY)["energy_sources"]
    assert len(legacy) == 1
    assert [f["stat_energy_from"] for f in legacy[0]["flow_from"]] == ["sensor.a_imp", "sensor.b_imp"]


def test_apply_topology_unified_preserves_cost_fields() -> None:
    current = {"energy_sources": [{
        "type": "grid", "stat_energy_from": IMP, "stat_energy_to": None,
        "stat_cost": None, "entity_energy_price": None, "number_energy_price": 0.31,
        "stat_compensation": None, "entity_energy_price_export": None,
        "number_energy_price_export": None, "cost_adjustment_day": 1.25,
    }], "device_consumption": []}
    result = apply_topology_prefs(current, _grid_topo())
    grids = [s for s in result["energy_sources"] if s["type"] == "grid"]
    assert len(grids) == 1
    assert grids[0]["stat_energy_to"] == EXP
    assert grids[0]["number_energy_price"] == 0.31
    assert grids[0]["cost_adjustment_day"] == 1.25
    assert required_key_errors(result, legacy=False) == []


def test_apply_topology_unified_drops_skipped_keeps_unrelated() -> None:
    current = {"energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.old_grid", "cost_adjustment_day": 0.0},
        {"type": "grid", "stat_energy_from": "sensor.utility_meter", "cost_adjustment_day": 0.0},
    ], "device_consumption": []}
    topo = _topo(
        EnergyRole("grid_import", IMP, "span_ebus", True, "ok", grid_connection="p1"),
        EnergyRole("grid_import", "sensor.old_grid", "span_ebus", False, "skip"),
    )
    result = apply_topology_prefs(current, topo)
    froms = [s["stat_energy_from"] for s in result["energy_sources"]]
    assert froms == ["sensor.utility_meter", IMP]


def test_apply_topology_legacy_keeps_one_grid_source() -> None:
    """Adding an export to a legacy grid source extends it instead of adding a second one."""
    current = {"energy_sources": [_legacy(
        [_flow_from(IMP, stat_cost="sensor.grid_cost")], []
    )], "device_consumption": []}
    result = apply_topology_prefs(current, _grid_topo())
    grids = [s for s in result["energy_sources"] if s["type"] == "grid"]
    assert grids == [_legacy(
        [_flow_from(IMP, stat_cost="sensor.grid_cost")],
        [{"stat_energy_to": EXP}],
    )]
    assert required_key_errors(result, legacy=True) == []


def test_apply_topology_converts_legacy_prefs_for_unified_ha() -> None:
    current = {"energy_sources": [_legacy(
        [{"stat_energy_from": IMP, "number_energy_price": 0.2}],
        [{"stat_energy_to": EXP, "number_energy_price": 0.05}],
        cost_adjustment_day=0.5,
    )], "device_consumption": []}
    result = apply_topology_prefs(current, _grid_topo(), UNIFIED)
    assert result["energy_sources"] == [{
        "type": "grid", "cost_adjustment_day": 0.5,
        "stat_energy_from": IMP, "number_energy_price": 0.2,
        "stat_energy_to": EXP, "number_energy_price_export": 0.05,
    }]


def test_merge_prefs_unified_adds_once() -> None:
    proposed = build_topology_aware_config(_grid_topo())
    merged = merge_prefs({}, proposed, UNIFIED)
    again = merge_prefs(merged, proposed, UNIFIED)
    assert again["energy_sources"] == merged["energy_sources"]
    assert len(again["device_consumption"]) == 1


def test_merge_prefs_legacy_extends_existing_source() -> None:
    current = {"energy_sources": [_legacy([{"stat_energy_from": IMP}], [])]}
    merged = merge_prefs(current, build_topology_aware_config(_grid_topo(), LEGACY))
    assert merged["energy_sources"] == [_legacy(
        [{"stat_energy_from": IMP}], [{"stat_energy_to": EXP}]
    )]


def test_merge_grid_sources_unified_does_not_mutate() -> None:
    grids = [{"type": "grid", "stat_energy_from": IMP, "cost_adjustment_day": 0.0}]
    before = copy.deepcopy(grids)
    merge_grid_sources(grids, [GridConnection(IMP, EXP)], set(), UNIFIED)
    assert grids == before


# HA's _migrate_legacy_grid_to_unified output for a legacy source holding the
# user's meter (priced) next to the lugs import and export that older
# hass-atlas releases added.
_HA_MIGRATED = [
    {"type": "grid", "cost_adjustment_day": 0.0,
     "stat_energy_from": "sensor.util_imp", "stat_cost": None,
     "entity_energy_price": None, "number_energy_price": 0.25,
     "stat_energy_to": EXP, "stat_compensation": None,
     "entity_energy_price_export": None, "number_energy_price_export": None},
    {"type": "grid", "cost_adjustment_day": 0.0,
     "stat_energy_from": IMP, "stat_cost": None,
     "entity_energy_price": None, "number_energy_price": None,
     "stat_energy_to": None, "stat_compensation": None,
     "entity_energy_price_export": None, "number_energy_price_export": None},
]


def test_merge_grid_sources_keeps_other_statistics_of_migrated_prefs() -> None:
    merged = merge_grid_sources(_HA_MIGRATED, [GridConnection(IMP, EXP)], set(), UNIFIED)
    assert merged == _HA_MIGRATED
    assert required_key_errors({"energy_sources": merged}, legacy=False) == []


def test_merge_prefs_keeps_other_statistics_of_migrated_prefs() -> None:
    current = {"energy_sources": copy.deepcopy(_HA_MIGRATED), "device_consumption": []}
    merged = merge_prefs(current, build_topology_aware_config(_grid_topo()), UNIFIED)
    grids = [s for s in merged["energy_sources"] if s["type"] == "grid"]
    assert grids == _HA_MIGRATED


def test_merge_grid_sources_prefers_source_sharing_both_sides() -> None:
    grids = [
        {"type": "grid", "stat_energy_from": IMP, "cost_adjustment_day": 0.0},
        {"type": "grid", "stat_energy_from": IMP, "stat_energy_to": EXP,
         "cost_adjustment_day": 0.0},
    ]
    merged = merge_grid_sources(grids, [GridConnection(IMP, EXP, "sensor.p")], set(), UNIFIED)
    assert merged == [grids[0], {**grids[1], "stat_rate": "sensor.p"}]


def test_merge_grid_sources_skips_rate_used_elsewhere() -> None:
    grids = [
        {"type": "grid", "stat_energy_from": "sensor.util_imp", "stat_rate": "sensor.p",
         "cost_adjustment_day": 0.0},
    ]
    merged = merge_grid_sources(grids, [GridConnection(IMP, EXP, "sensor.p")], set(), UNIFIED)
    assert merged == [grids[0], {"type": "grid", "stat_energy_from": IMP,
                                 "stat_energy_to": EXP, "cost_adjustment_day": 0.0}]


def test_merge_grid_sources_carries_costs_to_replacement() -> None:
    grids = [{"type": "grid", "stat_energy_from": IMP, "stat_energy_to": EXP,
              "number_energy_price": 0.31, "stat_compensation": "sensor.comp",
              "cost_adjustment_day": 1.2, "name": "Utility"}]
    merged = merge_grid_sources(
        grids, [GridConnection("sensor.ct_imp", "ct:external_exp")], {IMP, EXP}, UNIFIED
    )
    assert merged == [{"type": "grid", "stat_energy_from": "sensor.ct_imp",
                       "stat_energy_to": "ct:external_exp", "number_energy_price": 0.31,
                       "stat_compensation": "sensor.comp", "cost_adjustment_day": 1.2,
                       "name": "Utility"}]


def test_merge_grid_sources_legacy_new_import_has_price_keys() -> None:
    merged = merge_grid_sources(
        [_legacy([_flow_from(IMP, number_energy_price=0.31)], [])],
        [GridConnection("sensor.ct_imp")], {IMP}, LEGACY,
    )
    assert merged == [_legacy([_flow_from("sensor.ct_imp", number_energy_price=0.31)], [])]


def test_legacy_to_unified_matches_ha_migration() -> None:
    legacy = _legacy(
        [{"stat_energy_from": "sensor.a", "stat_cost": "sensor.cost"},
         {"stat_energy_from": "sensor.b"}],
        [{"stat_energy_to": "sensor.c", "entity_energy_price": "sensor.price"}],
        power=[{"stat_rate": "sensor.p"}],
        cost_adjustment_day=2.0,
    )
    assert legacy_to_unified(legacy) == [
        {"type": "grid", "cost_adjustment_day": 2.0, "stat_energy_from": "sensor.a",
         "stat_cost": "sensor.cost", "stat_energy_to": "sensor.c",
         "entity_energy_price_export": "sensor.price", "stat_rate": "sensor.p"},
        {"type": "grid", "cost_adjustment_day": 2.0, "stat_energy_from": "sensor.b"},
    ]


def test_convert_sources_unified_to_legacy_round_trip() -> None:
    legacy = _legacy(
        [_flow_from("sensor.a", stat_cost="sensor.cost")],
        [{"stat_energy_to": "sensor.c", "number_energy_price": 0.1}],
    )
    sources = [{"type": "solar", "stat_energy_from": "sensor.pv"}, legacy]
    unified = convert_sources(sources, UNIFIED)
    assert convert_sources(unified, LEGACY) == sources


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


UNIFIED_PREFS = {
    "energy_sources": [
        {"type": "grid", "stat_energy_from": IMP, "stat_energy_to": EXP,
         "stat_rate": "sensor.grid_power_generated",
         "power_config": {"stat_rate_inverted": "sensor.grid_power"},
         "cost_adjustment_day": 0.0},
        {"type": "grid", "stat_energy_from": "sensor.second_meter", "stat_energy_to": None,
         "cost_adjustment_day": 0.0},
    ],
    "device_consumption": [{"stat_consumption": "sensor.kitchen_energy"}],
}


def test_extract_energy_entity_ids_unified() -> None:
    assert extract_energy_entity_ids(UNIFIED_PREFS) == {
        IMP, EXP, "sensor.grid_power_generated", "sensor.grid_power",
        "sensor.second_meter", "sensor.kitchen_energy",
    }


def test_find_stale_references_unified() -> None:
    stale = find_stale_references(UNIFIED_PREFS, {IMP, "sensor.kitchen_energy"})
    assert stale == {
        "grid (grid export)": [EXP],
        "grid (grid import)": ["sensor.second_meter"],
        "grid (stat_rate)": ["sensor.grid_power_generated", "sensor.grid_power"],
    }


def test_remove_stale_references_unified() -> None:
    cleaned = remove_stale_references(
        UNIFIED_PREFS, {EXP, "sensor.second_meter", "sensor.grid_power"}
    )
    assert cleaned["energy_sources"] == [{
        "type": "grid", "stat_energy_from": IMP, "stat_energy_to": None,
        "cost_adjustment_day": 0.0,
    }]
    assert required_key_errors(cleaned, legacy=False) == []


def test_remove_stale_references_legacy_keeps_both_flow_lists() -> None:
    prefs = {"energy_sources": [_legacy(
        [_flow_from(IMP)], [{"stat_energy_to": EXP}]
    )], "device_consumption": []}
    cleaned = remove_stale_references(prefs, {EXP})
    assert cleaned["energy_sources"] == [_legacy([_flow_from(IMP)], [])]
    assert required_key_errors(cleaned, legacy=True) == []
    assert remove_stale_references(prefs, {IMP, EXP})["energy_sources"] == []


def test_apply_remap_unified() -> None:
    remapped = apply_remap_to_prefs(
        UNIFIED_PREFS, {IMP: "sensor.new_imp", "sensor.grid_power": "sensor.new_power"}
    )
    grid = remapped["energy_sources"][0]
    assert grid["stat_energy_from"] == "sensor.new_imp"
    assert grid["power_config"] == {"stat_rate_inverted": "sensor.new_power"}
    assert UNIFIED_PREFS["energy_sources"][0]["stat_energy_from"] == IMP
