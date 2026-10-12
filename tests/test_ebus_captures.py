"""span_ebus behavior on the masked r202639 captures: remote CTs, shared meters, gaps."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from hass_atlas.areas import _plan_assignments
from hass_atlas.cli import cli
from hass_atlas.context import Context
from hass_atlas.energy import (
    _build_topology,
    apply_topology_prefs,
    build_energy_config,
    build_topology_aware_config,
)
from hass_atlas.energy_prefs import LEGACY, UNIFIED
from hass_atlas.models import HADevice, HAEntity
from hass_atlas.output import console
from hass_atlas.registry import build_span_trees
from hass_atlas.span_panel import CircuitRoleError, resolve_ebus_circuit_roles
from hass_atlas.topology import (
    EnergyTopology,
    _circuit_node_id,
    find_shared_meter_groups,
)
from tests._fixture_loader import load_capture
from tests._ha_energy_schema import (
    required_key_errors,
    validate_legacy_flows_with_ha,
    validate_with_ha,
)
from tests.conftest import make_platform_entity

HANDLES = ["r202639-a", "r202639-b", "r202639-c", "r202639-d", "r202639-e", "main32_r202639"]


class Capture:
    def __init__(self, handle: str) -> None:
        self.devices, self.entities, self.states = load_capture(handle)
        self.trees = build_span_trees(self.devices, self.entities)
        self.tree = self.trees[0]

    def entity(self, device: HADevice, suffix: str) -> HAEntity:
        return next(e for e in device.entities if e.unique_id.endswith(suffix))

    def circuit(self, node_id: str) -> HADevice:
        return next(c for c in self.tree.circuits if _circuit_node_id(c) == node_id)

    def topology(self, *roles: str, extra: list[HAEntity] | None = None) -> EnergyTopology:
        resolved = resolve_ebus_circuit_roles(list(roles), self.trees)
        return _build_topology(
            self.trees, self.devices, self.entities + (extra or []), self.states, resolved
        )


def _preferred(topo: EnergyTopology, role: str) -> set[str]:
    return {a.entity_id for a in topo.role_assignments if a.preferred and a.role == role}


def _kinds(topo: EnergyTopology) -> list[str]:
    return sorted(g.kind for g in topo.gaps)


# ---------------------------------------------------------------------------
# Remote CTs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handle", "name", "circuits"),
    [("r202639-a", "9ooto Remote CT", 2), ("r202639-b", "18fkf Remote CT", 16)],
)
def test_remote_ct_kept_out_of_circuits(handle: str, name: str, circuits: int) -> None:
    cap = Capture(handle)
    assert [ct.display_name for ct in cap.tree.remote_cts] == [name]
    assert len(cap.tree.circuits) == circuits
    assert cap.tree.remote_cts[0] in cap.tree.all_child_devices


@pytest.mark.parametrize("handle", ["r202639-c", "r202639-d", "r202639-e", "main32_r202639"])
def test_no_remote_ct_without_one(handle: str) -> None:
    assert Capture(handle).tree.remote_cts == []


def test_unclassified_remote_ct_not_counted() -> None:
    cap = Capture("r202639-a")
    ct = cap.tree.remote_cts[0]
    topo = cap.topology()
    ct_eids = {e.entity_id for e in ct.entities}
    assert not ct_eids & {a.entity_id for a in topo.role_assignments}
    assert {cr.circuit.id for cr in topo.circuit_roles}.isdisjoint({ct.id})
    assert _kinds(topo) == [
        "remote_ct_with_lugs_grid", "service_entrance_unknown", "unclassified_remote_ct",
    ]
    gap = next(g for g in topo.gaps if g.kind == "unclassified_remote_ct")
    assert gap.circuit_id == "remote-ct-3"
    assert gap.hint == "--circuit-role xg-3940-9ooto:remote-ct-3=grid|load|solar|battery|ignore"
    assert gap.suggestions == [
        "Its power equals the panel Grid Power in magnitude; it is likely the service (grid) "
        "CT: --circuit-role xg-3940-9ooto:remote-ct-3=grid"
    ]
    assert "upstream lugs Power 8.8" in gap.message


def test_remote_ct_grid_role() -> None:
    cap = Capture("r202639-a")
    ct = cap.tree.remote_cts[0]
    lugs = cap.tree.lugs_up
    assert lugs is not None
    topo = cap.topology("remote-ct-3=grid")
    ct_import = cap.entity(ct, "_meter_imported-energy").entity_id
    ct_export = cap.entity(ct, "_meter_exported-energy").entity_id
    lugs_import = cap.entity(lugs, "_meter_imported-energy").entity_id
    assert _preferred(topo, "grid_import") == {ct_import}
    assert _preferred(topo, "grid_export") == {ct_export}
    # The lugs become the panel's total, the parent of its circuits.
    assert lugs_import in _preferred(topo, "device_consumption")
    assert _kinds(topo) == []
    grid = [s for s in build_topology_aware_config(topo)["energy_sources"] if s["type"] == "grid"]
    assert grid == [{
        "type": "grid", "stat_energy_from": ct_import, "stat_energy_to": ct_export,
        "stat_rate": cap.entity(ct, "_meter_active-power").entity_id, "cost_adjustment_day": 0.0,
    }]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("load", {"device_consumption": "imported-energy"}),
        ("solar", {"solar": "imported-energy"}),
        ("battery", {"battery_discharge": "imported-energy", "battery_charge": "exported-energy"}),
        ("ignore", {}),
    ],
)
def test_remote_ct_roles_direction(role: str, expected: dict[str, str]) -> None:
    cap = Capture("r202639-b")
    ct = cap.tree.remote_cts[0]
    topo = cap.topology(f"xg-1155-18fkf:remote-ct-3={role}")
    ct_eids = {e.entity_id for e in ct.entities}
    got = {
        a.role: a.entity_id.rsplit("_", 2)[-2] + "-" + a.entity_id.rsplit("_", 1)[-1]
        for a in topo.role_assignments if a.preferred and a.entity_id in ct_eids
    }
    assert got == expected
    assert "unclassified_remote_ct" not in _kinds(topo)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("circuit-17=grid", "is a breaker circuit"),
        ("remote-ct-3=subpanel:xg-1155-18fkf", "takes no subpanel role"),
        ("remote-ct-9=grid", "no panel has circuit 'remote-ct-9'"),
        ("nt-0000-zzzzz:remote-ct-3=grid", "no span_ebus panel with serial"),
        ("remote-ct-3=meter", "Unknown circuit role"),
    ],
)
def test_ebus_circuit_role_errors(spec: str, message: str) -> None:
    with pytest.raises(CircuitRoleError, match=message):
        resolve_ebus_circuit_roles([spec], Capture("r202639-b").trees)


def test_remote_ct_has_no_area_assignment_or_warning() -> None:
    cap = Capture("r202639-a")
    planned = {a.device_id for a in _plan_assignments(cap.trees, {}, {})}
    assert cap.tree.remote_cts[0].id not in planned
    assert planned == {c.id for c in cap.tree.circuits}


# ---------------------------------------------------------------------------
# Shared meters
# ---------------------------------------------------------------------------


def test_shared_meters_from_spaces() -> None:
    cap = Capture("r202639-e")
    groups = find_shared_meter_groups(cap.trees, cap.states)
    assert [[_circuit_node_id(m) for m in g.members] for g in groups] == [
        ["circuit-52", "circuit-53"], ["circuit-54", "circuit-55"], ["circuit-56", "circuit-57"],
    ]
    assert {g.method for g in groups} == {"spaces"}
    topo = cap.topology()
    consumers = _preferred(topo, "device_consumption")
    for counted, member in (("circuit-52", "circuit-53"), ("circuit-54", "circuit-55"),
                            ("circuit-56", "circuit-57")):
        assert cap.entity(cap.circuit(counted), "_meter_exported-energy").entity_id in consumers
        skipped = cap.entity(cap.circuit(member), "_meter_exported-energy").entity_id
        assert skipped not in consumers
        assert any(
            a.entity_id == skipped and not a.preferred for a in topo.role_assignments
        )
    gaps = [g for g in topo.gaps if g.kind == "shared_meter"]
    assert [g.circuit_id for g in gaps] == ["circuit-52", "circuit-54", "circuit-56"]
    assert gaps[0].hint is not None and "Meter Shared With" in gaps[0].hint


def test_shared_meters_from_enabled_shared_with_entity() -> None:
    cap = Capture("r202639-e")
    for c in cap.tree.circuits:
        for e in c.entities:
            if e.unique_id.endswith("_meter_shared-with-device-ids"):
                e.disabled_by = None
    # Spaces disagree with the published groups; the shared-with values decide.
    for e in cap.circuit("circuit-53").entities:
        if e.unique_id.endswith("_info_spaces"):
            cap.states[e.entity_id]["state"] = "99"
    groups = find_shared_meter_groups(cap.trees, cap.states)
    assert {g.method for g in groups} == {"shared_with"}
    assert len(groups) == 3
    gap = next(g for g in cap.topology().gaps if g.kind == "shared_meter")
    assert gap.hint is None


def test_shared_meters_from_readings_without_spaces() -> None:
    cap = Capture("r202639-e")
    for c in cap.tree.circuits:
        for e in c.entities:
            if e.unique_id.endswith("_info_spaces"):
                cap.states[e.entity_id]["state"] = "unknown"
    groups = find_shared_meter_groups(cap.trees, cap.states)
    assert {g.method for g in groups} == {"readings"}
    assert [_circuit_node_id(g.counted) for g in groups] == ["circuit-52", "circuit-54", "circuit-56"]


def test_same_space_different_readings_not_shared() -> None:
    cap = Capture("r202639-e")
    for e in cap.circuit("circuit-53").entities:
        if e.unique_id.endswith("_meter_exported-energy"):
            cap.states[e.entity_id]["state"] = "1.0"
    groups = find_shared_meter_groups(cap.trees, cap.states)
    assert ["circuit-52"] not in [[_circuit_node_id(g.counted)] for g in groups]
    assert len(groups) == 2


# ---------------------------------------------------------------------------
# Upstream battery, service entrance
# ---------------------------------------------------------------------------


def test_main32_battery_upstream_without_integration() -> None:
    cap = Capture("main32_r202639")
    topo = cap.topology()
    assert _kinds(topo) == ["battery_upstream_no_integration"]
    gap = topo.gaps[0]
    assert "battery discharge is counted as grid import" in gap.message
    assert gap.hint is not None and "powerwall or tesla_fleet" in gap.hint
    reasons = {a.reason for a in topo.role_assignments if a.role == "grid_import"}
    assert reasons == {
        "SPAN upstream metering: the battery is UPSTREAM and has no vendor integration, so "
        "this includes battery flow"
    }


def test_main32_battery_upstream_with_integration() -> None:
    cap = Capture("main32_r202639")
    pw = [
        make_platform_entity(eid, eid, "powerwall", "dev-pw", "energy", "total_increasing")
        for eid in ("sensor.powerwall_site_import", "sensor.powerwall_site_export",
                    "sensor.powerwall_battery_import", "sensor.powerwall_battery_export")
    ]
    cap.devices.append(HADevice(id="dev-pw", name="Powerwall", identifiers=[("powerwall", "x")]))
    topo = cap.topology(extra=pw)
    assert _kinds(topo) == []
    assert _preferred(topo, "grid_import") == {"sensor.powerwall_site_import"}


@pytest.mark.parametrize("handle", ["r202639-c", "r202639-d", "r202639-e"])
def test_service_entrance_unknown(handle: str) -> None:
    topo = Capture(handle).topology()
    assert "service_entrance_unknown" in _kinds(topo)


# ---------------------------------------------------------------------------
# Plain energy matches energy --topology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handle", HANDLES)
def test_plain_energy_matches_topology(handle: str) -> None:
    cap = Capture(handle)
    plain = build_energy_config(cap.trees, cap.states, cap.devices, cap.entities)
    assert plain == build_topology_aware_config(cap.topology())


def test_plain_energy_battery_feed_is_battery_not_load() -> None:
    cap = Capture("r202639-c")
    config = build_energy_config(cap.trees, cap.states, cap.devices, cap.entities)
    feed = cap.circuit("circuit-5")
    battery = next(s for s in config["energy_sources"] if s["type"] == "battery")
    assert battery["stat_energy_from"] == cap.entity(feed, "_meter_imported-energy").entity_id
    assert battery["stat_energy_to"] == cap.entity(feed, "_meter_exported-energy").entity_id
    consumers = {d["stat_consumption"] for d in config["device_consumption"]}
    assert cap.entity(feed, "_meter_exported-energy").entity_id not in consumers


def test_plain_energy_main32_solar_from_pv_feed() -> None:
    cap = Capture("main32_r202639")
    config = build_energy_config(cap.trees, cap.states, cap.devices, cap.entities)
    solar = next(s for s in config["energy_sources"] if s["type"] == "solar")
    assert solar["stat_energy_from"] == "sensor.commissioned_pv_system_imported_energy"


# ---------------------------------------------------------------------------
# Produced prefs against Home Assistant's schema
# ---------------------------------------------------------------------------


def _produced_prefs(fmt: str) -> list[dict[str, Any]]:
    produced = []
    for handle in HANDLES:
        cap = Capture(handle)
        produced.append(apply_topology_prefs({}, cap.topology(), fmt))
    cap = Capture("r202639-a")
    produced.append(apply_topology_prefs({}, cap.topology("remote-ct-3=grid"), fmt))
    return produced


@pytest.mark.parametrize("fmt", [UNIFIED, LEGACY])
def test_produced_prefs_have_required_keys(fmt: str) -> None:
    for prefs in _produced_prefs(fmt):
        assert required_key_errors(prefs, legacy=fmt == LEGACY) == []


def test_produced_prefs_pass_home_assistant_schema() -> None:
    checked = validate_with_ha(_produced_prefs(UNIFIED))
    if checked is None:
        pytest.skip("no Home Assistant install to import energy/data.py from")
    version, results = checked
    assert version >= "2026.3"
    assert results == [None] * len(results)


def test_legacy_prefs_pass_home_assistant_flow_schema() -> None:
    results = validate_legacy_flows_with_ha(_produced_prefs(LEGACY))
    if results is None:
        pytest.skip("no Home Assistant install to import energy/data.py from")
    assert results == [None] * len(results)


def test_remote_ct_grid_keeps_costs_of_replaced_lugs_source() -> None:
    cap = Capture("r202639-b")
    current = build_topology_aware_config(cap.topology(), UNIFIED)
    for s in current["energy_sources"]:
        if s["type"] == "grid":
            s.update(number_energy_price=0.31, number_energy_price_export=0.05,
                     cost_adjustment_day=1.2, name="Utility")
    result = apply_topology_prefs(current, cap.topology("remote-ct-3=grid"), UNIFIED)
    grids = [s for s in result["energy_sources"] if s["type"] == "grid"]
    assert len(grids) == 1
    assert "remote_ct" in grids[0]["stat_energy_from"]
    assert grids[0]["number_energy_price"] == 0.31
    assert grids[0]["number_energy_price_export"] == 0.05
    assert grids[0]["cost_adjustment_day"] == 1.2
    assert grids[0]["name"] == "Utility"


def test_home_assistant_schema_rejects_legacy_grid() -> None:
    checked = validate_with_ha(_produced_prefs(LEGACY)[:1])
    if checked is None:
        pytest.skip("no Home Assistant install to import energy/data.py from")
    _, results = checked
    assert results[0] is not None and "flow_from" in results[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _raw_device(d: HADevice) -> dict[str, Any]:
    return {
        "id": d.id, "name": d.name, "name_by_user": d.name_by_user, "model": d.model,
        "identifiers": [list(i) for i in d.identifiers], "via_device_id": d.via_device_id,
        "area_id": d.area_id,
    }


def _raw_entity(e: HAEntity) -> dict[str, Any]:
    return {
        "entity_id": e.entity_id, "unique_id": e.unique_id, "platform": e.platform,
        "device_id": e.device_id, "original_device_class": e.device_class,
        "original_state_class": e.state_class, "original_name": e.original_name,
        "disabled_by": e.disabled_by, "has_entity_name": e.has_entity_name,
    }


class EbusFakeClient:
    """Mock HA client serving one capture as span_ebus registry rows and states."""

    def __init__(self, handle: str, ha_version: str | None, prefs: dict | None = None) -> None:
        self.devices, self.entities, self.states = load_capture(handle)
        self.ha_version = ha_version
        self.prefs = prefs or {}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> EbusFakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_command(self, msg_type: str, **kwargs: Any) -> Any:
        self.sent.append((msg_type, kwargs))
        if msg_type == "config/device_registry/list":
            return [_raw_device(d) for d in self.devices]
        if msg_type == "config/entity_registry/list":
            return [_raw_entity(e) for e in self.entities]
        if msg_type == "config/area_registry/list":
            return []
        if msg_type == "get_states":
            return [{"entity_id": eid, **st} for eid, st in self.states.items()]
        if msg_type == "energy/get_prefs":
            return self.prefs
        if msg_type == "energy/save_prefs":
            return None
        raise AssertionError(f"unexpected command {msg_type}")


def _run(fake: EbusFakeClient, *args: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Context, "client", lambda self: fake)
    monkeypatch.setattr(console, "width", 10_000)
    return CliRunner().invoke(cli, ["--url", "http://ha.test:8123", "--token", "t", *args])


@pytest.mark.parametrize("args", [["energy"], ["energy", "--topology"]])
@pytest.mark.parametrize(("version", "legacy"), [("2026.9.4", False), ("2026.2.3", True)])
def test_cli_energy_saves_format_for_version(
    args: list[str], version: str, legacy: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = EbusFakeClient("r202639-a", version)
    result = _run(fake, *args, "--circuit-role", "remote-ct-3=grid", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    saved = next(kw for cmd, kw in fake.sent if cmd == "energy/save_prefs")
    assert required_key_errors(saved, legacy=legacy) == []
    grids = [s for s in saved["energy_sources"] if s["type"] == "grid"]
    assert len(grids) == 1
    key = "flow_from" if legacy else "stat_energy_from"
    assert "remote_ct" in str(grids[0][key])


@pytest.mark.parametrize(
    "args", [["energy-topology"], ["audit"], ["--dry-run", "energy"]]
)
def test_cli_reports_gaps(args: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(EbusFakeClient("r202639-a", "2026.9.4"), *args, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "[unclassified_remote_ct] xg-3940-9ooto:remote-ct-3" in result.output
    assert "[service_entrance_unknown]" in result.output


def test_cli_audit_lists_remote_ct_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(EbusFakeClient("r202639-a", "2026.9.4"), "audit", monkeypatch=monkeypatch)
    assert "Remote CTs (1, meters outside the panel)" in result.output
    assert "Circuits (2)" in result.output
    no_area = result.output.split("have no area assigned:")[1].split("WARN")[0]
    assert "Remote CT" not in no_area


def test_cli_plain_energy_warns_skipped_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = Capture("r202639-e")
    member = cap.entity(cap.circuit("circuit-53"), "_meter_exported-energy").entity_id
    fake = EbusFakeClient(
        "r202639-e", "2026.9.4",
        prefs={"energy_sources": [], "device_consumption": [{"stat_consumption": member}]},
    )
    result = _run(fake, "--dry-run", "energy", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "that energy --topology would remove are kept" in result.output
    assert member in result.output
