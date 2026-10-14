"""Tests for span_panel support."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from click.testing import CliRunner

from tests._ha_energy_schema import required_key_errors, validate_with_ha
from tests._span_panel_loader import (
    STEMS,
    SYNTHETIC_DIR,
    SYNTHETIC_STEMS,
    FakeClient,
    SpanPanelFixture,
    build_fixture,
    drop_newer_fields,
    load_fixture,
    load_json,
    load_synthetic,
    renamed_fixture,
)
from hass_atlas.cli import cli
from hass_atlas.context import Context
from hass_atlas.ha_client import HAClientError
from hass_atlas.models import HADevice
from hass_atlas.output import console, render_topology
from hass_atlas.registry import enrich_entities_from_states, parse_registries
from hass_atlas.span_panel import (
    CircuitRoleError,
    PanelTopology,
    SpanPanelAdminRequired,
    build_energy_topology_span_panel,
    detect_integrations,
    fetch_panel_topologies,
    find_misplaced_consumers,
    find_panel_devices,
    parse_panel_topology,
    resolve_circuit_roles,
    resolve_main_meter,
)
from hass_atlas.topology import EnergyIntegration, EnergyRole, EnergyTopology

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panels(*fixtures: SpanPanelFixture) -> tuple[list[PanelTopology], dict[str, Any]]:
    """Parse fixtures into PanelTopology objects and merged states."""
    panels: list[PanelTopology] = []
    states: dict[str, Any] = {}
    for f in fixtures:
        devices, entities = f.models()
        enrich_entities_from_states(entities, f.states)
        for device in find_panel_devices(devices):
            panels.append(parse_panel_topology(f.topology, device, entities))
        states.update(f.states)
    return panels, states


def _build(
    *fixtures: SpanPanelFixture,
    roles: tuple[str, ...] = (),
    integrations: list[EnergyIntegration] | None = None,
) -> tuple[list[PanelTopology], Any, list[Any]]:
    panels, states = _panels(*fixtures)
    resolved = resolve_circuit_roles(roles, panels)
    topo, gaps = build_energy_topology_span_panel(panels, states, integrations or [], resolved)
    return panels, topo, gaps


def _role_eids(topo: EnergyTopology, role: str, preferred: bool = True) -> list[str]:
    return [a.entity_id for a in topo.role_assignments if a.role == role and a.preferred is preferred]


def _consumers(topo: EnergyTopology) -> dict[str, EnergyRole]:
    return {a.entity_id: a for a in topo.role_assignments if a.role == "device_consumption"}


def _kinds(gaps: list[Any]) -> list[str]:
    return [g.kind for g in gaps]


def _two_panels() -> tuple[SpanPanelFixture, SpanPanelFixture]:
    return load_fixture("r202639-c"), renamed_fixture("r202639-d", "p2")


def _with_topology(stem: str, topology: dict[str, Any]) -> SpanPanelFixture:
    return build_fixture(stem, topology, load_json("expected_entities", stem))


def _run(fake: FakeClient, *args: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Context, "client", lambda self: fake)
    return CliRunner().invoke(cli, ["--url", "http://ha.test:8123", "--token", "t", *args])


# ---------------------------------------------------------------------------
# Loader + detection
# ---------------------------------------------------------------------------


def test_loader_ids_match_topology() -> None:
    for stem in STEMS:
        f = load_fixture(stem)
        device_ids = {d["id"] for d in f.raw_devices}
        assert f.topology["panel_device_id"] in device_ids
        assert set(f.topology["sub_devices"]) <= device_ids


def test_detect_integrations_span_panel_only() -> None:
    devices, _ = load_fixture("r202639-c").models()
    assert detect_integrations(devices) == {"span_panel"}


def test_detect_integrations_span_ebus(raw_devices: list[dict]) -> None:
    devices, _, _ = parse_registries(raw_devices, [], [])
    assert detect_integrations(devices) == {"span_ebus"}


def test_find_panel_devices_only_the_panel() -> None:
    devices, _ = load_fixture("main32_r202639").models()
    panels = find_panel_devices(devices)
    assert [d.id for d in panels] == ["nt-9874-s7rxt"]


# ---------------------------------------------------------------------------
# Topology parsing + main meter
# ---------------------------------------------------------------------------


def test_parse_topology_fields() -> None:
    panels, _ = _panels(load_fixture("r202639-e"))
    (p,) = panels
    assert p.serial == "xg-6159-74dlx"
    assert p.model == "MLO_48"
    assert p.panel_size == 48
    assert p.missing_fields == []
    assert len(p.circuits) == 46
    groups = {c.id: c.shared_meter_group for c in p.circuits if c.shared_meter_group}
    assert groups["circuit-53"] == "circuit-52"
    assert {"circuit-52", "circuit-54", "circuit-56"} <= p.counted_meter_members()
    assert [s["type"] for s in p.sub_devices] == ["evse"]


def test_parse_topology_remote_ct() -> None:
    panels, _ = _panels(load_fixture("r202639-b"))
    ct = next(c for c in panels[0].circuits if c.id == "remote-ct-3")
    assert ct.outside_panel is True
    assert ct.remote_ct
    assert ct.tabs == []


def test_main_meter_resolution() -> None:
    panels, _ = _panels(load_fixture("r202639-c"))
    p = panels[0]
    assert p.main_meter_consumed is not None
    assert p.main_meter_consumed.unique_id == "span_xg-7814-15i6h_main_meter_consumed_energy"
    assert p.main_meter_produced is not None
    assert p.main_meter_produced.entity_id == "sensor.span_panel_main_meter_produced_energy"


def test_main_meter_resolution_lowercases_serial() -> None:
    _, entities = load_fixture("r202639-c").models()
    consumed, produced = resolve_main_meter("XG-7814-15I6H", entities)
    assert consumed is not None and produced is not None


# ---------------------------------------------------------------------------
# Energy roles per fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", STEMS)
def test_energy_roles_each_fixture(stem: str) -> None:
    panels, topo, _ = _build(load_fixture(stem))
    (p,) = panels
    assert p.main_meter_consumed and p.main_meter_produced
    assert _role_eids(topo, "grid_import") == [p.main_meter_consumed.entity_id]
    assert _role_eids(topo, "grid_export") == [p.main_meter_produced.entity_id]
    assert all(a.platform == "span_panel" for a in topo.role_assignments)

    consumers = _consumers(topo)
    expected_solar = []
    for c in p.circuits:
        eid = c.entities["consumed_energy"]
        excluded = c.remote_ct or p.non_key_meter_member(c) or p.is_generation_circuit(c)
        assert (eid in consumers) is not excluded, (stem, c.id)
        if eid in consumers:
            assert consumers[eid].rate_entity_id == c.entities.get("power")
            assert consumers[eid].parent_entity_id is None
        if p.is_generation_circuit(c):
            expected_solar.append(c.entities["produced_energy"])
    assert _role_eids(topo, "solar") == expected_solar


def test_main32_solar_from_generation_circuit() -> None:
    _, topo, gaps = _build(load_fixture("main32_r202639"))
    (solar,) = [a for a in topo.role_assignments if a.role == "solar"]
    assert solar.entity_id == "sensor.span_panel_commissioned_pv_system_produced_energy"
    assert solar.rate_entity_id == "sensor.span_panel_solar_pv_power"
    assert "sensor.span_panel_commissioned_pv_system_consumed_energy" not in _consumers(topo)
    assert "solar_no_energy" not in _kinds(gaps)


def test_shared_meter_counts_key_member_once() -> None:
    panels, topo, _ = _build(load_fixture("r202639-e"))
    circuits = {c.id: c for c in panels[0].circuits}
    consumers = _consumers(topo)
    assert circuits["circuit-52"].entities["consumed_energy"] in consumers
    assert circuits["circuit-53"].entities["consumed_energy"] not in consumers
    assert len(consumers) == 43


def test_synthetic_other_model_solar_circuit() -> None:
    topology = copy.deepcopy(load_json("topology", "r202639-c"))
    topology["circuits"]["circuit-2"]["is_generation"] = True
    _, topo, _ = _build(_with_topology("r202639-c", topology))
    circuit = topology["circuits"]["circuit-2"]["entities"]
    (solar,) = [a for a in topo.role_assignments if a.role == "solar"]
    assert solar.entity_id == circuit["produced_energy"]
    # No PV sub-device and no pv_power: the circuit's own power is the rate.
    assert solar.rate_entity_id == circuit["power"]
    assert circuit["consumed_energy"] not in _consumers(topo)


def test_vendor_solar_non_preferred_when_generation_circuit() -> None:
    enphase = EnergyIntegration(
        platform="enphase_envoy", devices=[],
        energy_entities=[_entity("sensor.envoy_lifetime_energy_production", "enphase_envoy")],
    )
    _, topo, _ = _build(load_fixture("main32_r202639"), integrations=[enphase])
    assert _role_eids(topo, "solar", preferred=False) == ["sensor.envoy_lifetime_energy_production"]


def test_vendor_battery_preferred_when_no_battery_circuit() -> None:
    powerwall = EnergyIntegration(
        platform="powerwall", devices=[],
        energy_entities=[
            _entity("sensor.powerwall_battery_import", "powerwall"),
            _entity("sensor.powerwall_battery_export", "powerwall"),
        ],
    )
    _, topo, gaps = _build(load_fixture("r202639-c"), integrations=[powerwall])
    assert _role_eids(topo, "battery_charge") == ["sensor.powerwall_battery_import"]
    assert _role_eids(topo, "battery_discharge") == ["sensor.powerwall_battery_export"]
    assert "battery_no_energy" not in _kinds(gaps)


def _entity(entity_id: str, platform: str) -> Any:
    from hass_atlas.models import HAEntity

    return HAEntity(
        entity_id=entity_id, unique_id=entity_id, platform=platform, device_id=None,
        device_class="energy", state_class="total_increasing",
    )


# ---------------------------------------------------------------------------
# --circuit-role
# ---------------------------------------------------------------------------


def test_role_grid_on_remote_ct() -> None:
    panels, topo, gaps = _build(load_fixture("r202639-b"), roles=("remote-ct-3=grid",))
    p = panels[0]
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_remote_ct_3_consumed_energy"]
    assert _role_eids(topo, "grid_export") == ["sensor.span_panel_remote_ct_3_produced_energy"]
    consumers = _consumers(topo)
    assert p.main_meter_consumed is not None
    main = p.main_meter_consumed.entity_id
    assert consumers[main].parent_entity_id is None
    circuit_parents = {a.parent_entity_id for eid, a in consumers.items() if eid != main}
    assert circuit_parents == {main}
    assert "unclassified_remote_ct" not in _kinds(gaps)


def test_role_load_on_remote_ct() -> None:
    _, topo, gaps = _build(load_fixture("r202639-b"), roles=("remote-ct-3=load",))
    assert "sensor.span_panel_remote_ct_3_consumed_energy" in _consumers(topo)
    assert "unclassified_remote_ct" not in _kinds(gaps)


def test_role_solar_on_remote_ct_uses_consumed() -> None:
    # A remote CT counts from the panel's side: PV output flows in as consumed.
    _, topo, _ = _build(load_fixture("r202639-b"), roles=("remote-ct-3=solar",))
    (solar,) = [a for a in topo.role_assignments if a.role == "solar"]
    assert solar.entity_id == "sensor.span_panel_remote_ct_3_consumed_energy"
    assert solar.rate_entity_id == "sensor.span_panel_remote_ct_3_power"


def test_role_battery_on_remote_ct_direction() -> None:
    _, topo, _ = _build(load_fixture("r202639-a"), roles=("remote-ct-3=battery",))
    assert _role_eids(topo, "battery_discharge") == [
        "sensor.span_panel_remote_ct_3_consumed_energy"
    ]
    assert _role_eids(topo, "battery_charge") == ["sensor.span_panel_remote_ct_3_produced_energy"]


def test_role_solar_on_breaker_circuit_skips_negative_power_rate() -> None:
    # span_panel flips power sign only for generation circuits, so a breaker
    # circuit given role solar reads negative while producing.
    panels, topo, _ = _build(load_fixture("r202639-b"), roles=("circuit-17=solar",))
    circuit = next(c for c in panels[0].circuits if c.id == "circuit-17")
    (solar,) = [a for a in topo.role_assignments if a.role == "solar"]
    assert solar.entity_id == circuit.entities["produced_energy"]
    assert solar.rate_entity_id is None
    assert any("circuit-17" in w for w in topo.warnings)


def test_role_battery() -> None:
    _, topo, gaps = _build(load_fixture("r202639-c"), roles=("circuit-2=battery",))
    charge = [a for a in topo.role_assignments if a.role == "battery_charge"]
    discharge = [a for a in topo.role_assignments if a.role == "battery_discharge"]
    assert [a.entity_id for a in charge] == ["sensor.span_panel_bathroom_outlets_consumed_energy"]
    assert [a.entity_id for a in discharge] == [
        "sensor.span_panel_bathroom_outlets_produced_energy"
    ]
    assert charge[0].rate_entity_id == "sensor.span_panel_battery_battery_power"
    assert "sensor.span_panel_bathroom_outlets_consumed_energy" not in _consumers(topo)
    assert "battery_no_energy" not in _kinds(gaps)


def test_role_ignore() -> None:
    _, topo, gaps = _build(load_fixture("r202639-b"), roles=("remote-ct-3=ignore",))
    assert "sensor.span_panel_remote_ct_3_consumed_energy" not in _consumers(topo)
    assert "unclassified_remote_ct" not in _kinds(gaps)


def test_role_subpanel() -> None:
    lead, sub = _two_panels()
    _, topo, gaps = _build(lead, sub, roles=("xg-7814-15i6h:circuit-3=subpanel:xg-1334-066e8",))
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_main_meter_consumed_energy"]
    consumers = _consumers(topo)
    sub_main = "sensor.p2_main_meter_consumed_energy"
    assert consumers[sub_main].parent_entity_id is None
    assert "sensor.span_panel_living_room_lights_consumed_energy" not in consumers
    sub_circuits = [a for eid, a in consumers.items() if eid.startswith("sensor.p2_")]
    assert {a.parent_entity_id for a in sub_circuits if a.entity_id != sub_main} == {sub_main}
    assert "feed_unknown" not in _kinds(gaps)


def test_role_unknown_circuit_id() -> None:
    panels, _ = _panels(load_fixture("r202639-c"))
    with pytest.raises(CircuitRoleError, match="no panel has circuit 'circuit-99'"):
        resolve_circuit_roles(("circuit-99=load",), panels)


def test_role_ambiguous_across_panels() -> None:
    panels, _ = _panels(*_two_panels())
    with pytest.raises(CircuitRoleError, match="exists on panels"):
        resolve_circuit_roles(("circuit-1=load",), panels)
    roles = resolve_circuit_roles(("xg-1334-066e8:circuit-1=load",), panels)
    assert ("xg-1334-066e8", "circuit-1") in roles


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ("circuit-1=heater", "Unknown circuit role"),
        ("circuit-1", "Invalid --circuit-role"),
        ("circuit-1=subpanel:", "needs a panel serial"),
        ("circuit-1=subpanel:xg-0000-nopes", "no span_panel panel with serial"),
        ("circuit-1=subpanel:xg-7814-15i6h", "cannot feed itself"),
        ("xg-0000-nopes:circuit-1=load", "no span_panel panel with serial"),
    ],
)
def test_role_validation_errors(spec: str, match: str) -> None:
    panels, _ = _panels(load_fixture("r202639-c"))
    with pytest.raises(CircuitRoleError, match=match):
        resolve_circuit_roles((spec,), panels)


def test_role_feed_cycle_rejected() -> None:
    panels, _ = _panels(*_two_panels())
    with pytest.raises(CircuitRoleError, match="cycle"):
        resolve_circuit_roles(
            (
                "xg-7814-15i6h:circuit-3=subpanel:xg-1334-066e8",
                "xg-1334-066e8:circuit-2=subpanel:xg-7814-15i6h",
            ),
            panels,
        )


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def test_gap_unclassified_remote_ct() -> None:
    _, topo, gaps = _build(load_fixture("r202639-b"))
    (gap,) = [g for g in gaps if g.kind == "unclassified_remote_ct"]
    assert gap.serial == "xg-1155-18fkf"
    assert gap.circuit_id == "remote-ct-3"
    assert "Remote CT 3" in gap.message
    assert "consumed 2202.2 Wh" in gap.message
    assert "produced 685.2 Wh" in gap.message
    assert "Power 0.2 W (panel Grid Power 0.2 W)" in gap.message
    assert gap.hint is not None and "--circuit-role xg-1155-18fkf:remote-ct-3=" in gap.hint
    # Equal to the panel's Grid Power: suggested whatever the size.
    assert [s.endswith("remote-ct-3=grid") for s in gap.suggestions] == [True]
    assert "sensor.span_panel_remote_ct_3_consumed_energy" not in _consumers(topo)


def test_gap_remote_ct_grid_suggestion_not_applied() -> None:
    f = load_fixture("r202639-b")
    f.states["sensor.span_panel_remote_ct_3_power"]["state"] = "1510"
    f.states["sensor.span_panel_grid_power"]["state"] = "1500"
    _, topo, gaps = _build(f)
    (gap,) = [g for g in gaps if g.kind == "unclassified_remote_ct"]
    assert any("within 5%" in s and "=grid" in s for s in gap.suggestions)
    assert "sensor.span_panel_remote_ct_3_consumed_energy" not in _role_eids(topo, "grid_import")


def test_gap_unknown_panel_model() -> None:
    _, _, gaps = _build(load_fixture("r202639-a"))
    (gap,) = [g for g in gaps if g.kind == "unknown_panel_model"]
    assert "'SPAN Panel'" in gap.message and "size 0" in gap.message


def test_gap_battery_without_energy_source() -> None:
    _, topo, gaps = _build(load_fixture("r202639-c"))
    assert "battery_no_energy" in _kinds(gaps)
    assert not _role_eids(topo, "battery_charge")


def test_gap_battery_suggests_closest_circuits() -> None:
    f = load_fixture("r202639-c")
    f.states["sensor.span_panel_battery_battery_power"]["state"] = "-2000"
    f.states["sensor.span_panel_bathroom_outlets_power"]["state"] = "1990"
    _, _, gaps = _build(f)
    (gap,) = [g for g in gaps if g.kind == "battery_no_energy"]
    assert gap.suggestions[0].startswith("xg-7814-15i6h:circuit-2 ")
    assert gap.suggestions[0].endswith("=battery")


def test_gap_solar_without_energy_source() -> None:
    _, topo, gaps = _build(load_fixture("main32_r202633"))
    assert "solar_no_energy" in _kinds(gaps)
    assert not _role_eids(topo, "solar")


def test_gap_two_panels_feed_unknown() -> None:
    _, topo, gaps = _build(*_two_panels())
    assert "feed_unknown" in _kinds(gaps)
    assert sorted(_role_eids(topo, "grid_import")) == [
        "sensor.p2_main_meter_consumed_energy",
        "sensor.span_panel_main_meter_consumed_energy",
    ]


def test_gap_subpanel_not_at_service_entrance_suggests_feed() -> None:
    lead, sub = _two_panels()
    sub.states["sensor.p2_current_power"]["state"] = "1200"
    sub.states["sensor.p2_current_power"]["attributes"]["at_service_entrance"] = False
    lead.states["sensor.span_panel_living_room_lights_power"]["state"] = "1180"
    _, topo, gaps = _build(lead, sub)
    (gap,) = [g for g in gaps if g.kind == "subpanel_feed_unknown"]
    assert gap.serial == "xg-1334-066e8"
    assert gap.suggestions[0].startswith("xg-7814-15i6h:circuit-3 ")
    assert gap.suggestions[0].endswith("=subpanel:xg-1334-066e8")
    # The lead keeps the grid; the sub-panel's main meter is an unparented consumer.
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_main_meter_consumed_energy"]
    assert _consumers(topo)["sensor.p2_main_meter_consumed_energy"].parent_entity_id is None
    assert "feed_unknown" not in _kinds(gaps)


# ---------------------------------------------------------------------------
# 2.1.2 payload degradation
# ---------------------------------------------------------------------------


def test_payload_from_2_1_2_degrades() -> None:
    old = drop_newer_fields(load_json("topology", "main32_r202639"))
    panels, topo, gaps = _build(_with_topology("main32_r202639", old))
    assert set(panels[0].missing_fields) == {
        "first_position", "last_position", "outside_panel", "is_generation",
        "shared_meter_group", "shared_relay_group",
    }
    assert all(c.is_generation is None for c in panels[0].circuits)
    # Generation falls back to device_type "pv".
    assert _role_eids(topo, "solar") == [
        "sensor.span_panel_commissioned_pv_system_produced_energy"
    ]
    (gap,) = [g for g in gaps if g.kind == "payload_fields_missing"]
    assert "Remote CT and shared-meter checks were unavailable" in gap.message


def test_payload_from_2_1_2_remote_ct_by_missing_tabs() -> None:
    old = drop_newer_fields(load_json("topology", "r202639-b"))
    _, topo, gaps = _build(_with_topology("r202639-b", old))
    assert "sensor.span_panel_remote_ct_3_consumed_energy" not in _consumers(topo)
    assert "unclassified_remote_ct" in _kinds(gaps)


# ---------------------------------------------------------------------------
# Misplaced consumers (energy-audit)
# ---------------------------------------------------------------------------


def test_find_misplaced_consumers() -> None:
    panels, _ = _panels(load_fixture("r202639-b"), renamed_fixture("main32_r202639", "m"))
    prefs = {"device_consumption": [
        {"stat_consumption": "sensor.span_panel_remote_ct_3_consumed_energy"},
        {"stat_consumption": "sensor.m_commissioned_pv_system_consumed_energy"},
        {"stat_consumption": "sensor.span_panel_breaker_1_consumed_energy"},
    ]}
    found = find_misplaced_consumers(panels, prefs)
    assert sorted(_kinds(found)) == ["consumer_generation", "consumer_remote_ct"]
    roles = resolve_circuit_roles(("remote-ct-3=load",), panels)
    assert _kinds(find_misplaced_consumers(panels, prefs, roles)) == ["consumer_generation"]


def test_find_misplaced_shared_meter_member() -> None:
    panels, _ = _panels(load_fixture("r202639-e"))
    member = next(c for c in panels[0].circuits if c.id == "circuit-53")
    prefs = {"device_consumption": [{"stat_consumption": member.entities["consumed_energy"]}]}
    assert _kinds(find_misplaced_consumers(panels, prefs)) == ["consumer_shared_meter_member"]


# ---------------------------------------------------------------------------
# Fetch + admin error
# ---------------------------------------------------------------------------


async def test_fetch_panel_topologies_sends_device_id() -> None:
    f = load_fixture("r202639-c")
    fake = FakeClient([f])
    devices, entities = f.models()
    panels = await fetch_panel_topologies(fake, devices, entities)  # type: ignore[arg-type]
    assert [p.serial for p in panels] == ["xg-7814-15i6h"]
    assert fake.sent == [("span_panel/panel_topology", {"device_id": "xg-7814-15i6h"})]


async def test_fetch_panel_topologies_admin_required() -> None:
    f = load_fixture("r202639-c")
    fake = FakeClient([f], topology_error=HAClientError(
        "span_panel/panel_topology failed: Unauthorized", code="unauthorized"
    ))
    devices, _ = f.models()
    with pytest.raises(SpanPanelAdminRequired, match="administrator token"):
        await fetch_panel_topologies(fake, devices)  # type: ignore[arg-type]


def test_cli_admin_required_message(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("r202639-c")], topology_error=HAClientError(
        "span_panel/panel_topology failed: Unauthorized", code="unauthorized"
    ))
    result = _run(fake, "energy-topology", monkeypatch=monkeypatch)
    assert result.exit_code == 1
    assert "administrator token" in result.output


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["areas"], "areas"),
        (["normalize"], "normalize"),
        (["cascade"], "cascade"),
        (["link-panels", "nt-2024-d3e4f:nt-2024-a1b2c"], "link-panels"),
        (["energy-audit", "--remap"], "energy-audit --remap"),
    ],
)
def test_writer_commands_exit_for_span_panel(
    args: list[str], command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient([load_fixture("r202639-c")])
    result = _run(fake, *args, monkeypatch=monkeypatch)
    assert result.exit_code == 1
    assert f"span_panel support for '{command}' is not yet available" in " ".join(
        result.output.split()
    )
    assert not any(t.endswith("save_prefs") or t.endswith("/update") for t, _ in fake.sent)


def test_cli_audit_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("r202639-b")])
    result = _run(fake, "audit", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Found 1 span_panel panel(s)" in result.output
    assert "unclassified_remote_ct" in result.output
    assert ("span_panel/panel_topology", {"device_id": "xg-1155-18fkf"}) in fake.sent


def test_cli_audit_json_includes_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    f = load_fixture("r202639-b")
    f.raw_entities = [
        {**e, "area_id": "garage"} if e["entity_id"] == "sensor.span_panel_breaker_1_power" else e
        for e in f.raw_entities
    ]
    fake = FakeClient([f])
    monkeypatch.setattr(console, "width", 10_000)
    result = _run(fake, "audit", "--format", "json", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["integration"] == "span_panel"
    # The fixture states carry no attributes, so at_service_entrance is unknown.
    assert sorted(g["kind"] for g in data["gaps"]) == [
        "service_entrance_unknown", "unclassified_remote_ct",
    ]
    (panel,) = data["panels"]
    circuits = {c["id"]: c for c in panel["circuits"]}
    assert circuits["remote-ct-3"]["remote_ct"] is True
    assert circuits["circuit-17"]["areas"] == ["garage"]


def test_cli_audit_with_circuit_role(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("r202639-b")])
    monkeypatch.setattr(console, "width", 10_000)
    result = _run(
        fake, "audit", "--format", "json", "--circuit-role", "remote-ct-3=grid",
        monkeypatch=monkeypatch,
    )
    data = json.loads(result.output)
    assert data["gaps"] == []
    assert {c["id"]: c["role"] for c in data["panels"][0]["circuits"]}["remote-ct-3"] == "grid"


def test_cli_bad_circuit_role(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("r202639-b")])
    result = _run(fake, "energy-topology", "--circuit-role", "nope=load", monkeypatch=monkeypatch)
    assert result.exit_code == 2
    assert "no panel has circuit 'nope'" in result.output


def test_cli_energy_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("main32_r202639")])
    monkeypatch.setattr(console, "width", 200)
    result = _run(fake, "energy-topology", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "SPAN Panel nt-9874-s7rxt" in result.output
    assert "Circuit consumed_energy = consumption" in result.output
    assert "battery_no_energy" in result.output


def test_cli_energy_audit_flags_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = {"device_consumption": [
        {"stat_consumption": "sensor.span_panel_remote_ct_3_consumed_energy"},
    ]}
    fake = FakeClient([load_fixture("r202639-b")], prefs=prefs)
    monkeypatch.setattr(console, "width", 200)
    result = _run(fake, "energy-audit", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "consumer_remote_ct" in result.output
    assert "No stale references" in result.output


def test_cli_span_ebus_does_not_call_panel_topology(
    raw_devices: list[dict], raw_entities: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient([], extra_devices=raw_devices, extra_entities=raw_entities)
    for args in (["audit"], ["energy-topology"], ["energy-audit"]):
        result = _run(fake, *args, monkeypatch=monkeypatch)
        assert result.exit_code == 0, result.output
        assert "span_panel" not in result.output
    assert not any(t == "span_panel/panel_topology" for t, _ in fake.sent)


# ---------------------------------------------------------------------------
# Shared output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["span_ebus", "span_panel"])
def test_render_topology_consumption_platform_from_roles(platform: str) -> None:
    topo = EnergyTopology(
        panels=[], integrations=[], circuit_roles=[], warnings=[],
        role_assignments=[EnergyRole(
            role="device_consumption", entity_id="sensor.x", platform=platform,
            preferred=True, reason="r",
        )],
    )
    with console.capture() as cap:
        render_topology(topo)
    assert platform in cap.get()


def test_panel_device_without_identifier_is_not_a_panel() -> None:
    other = HADevice(id="x", name="x", identifiers=[("hue", "1")])
    assert find_panel_devices([other]) == []


# ---------------------------------------------------------------------------
# Solar defaults, grid away from the service entrance, feed-through feeds
# ---------------------------------------------------------------------------


def test_generation_remote_ct_is_a_gap_not_solar() -> None:
    topology = copy.deepcopy(load_json("topology", "r202639-c"))
    circuit = topology["circuits"]["circuit-2"]
    circuit.update(outside_panel=True, is_generation=True, tabs=[])
    _, topo, gaps = _build(_with_topology("r202639-c", topology))
    assert not _role_eids(topo, "solar")
    assert [g.circuit_id for g in gaps if g.kind == "unclassified_remote_ct"] == ["circuit-2"]


def test_shared_meter_generation_counted_once() -> None:
    topology = copy.deepcopy(load_json("topology", "r202639-c"))
    for cid in ("circuit-1", "circuit-2"):
        topology["circuits"][cid].update(is_generation=True, shared_meter_group="circuit-1")
    _, topo, _ = _build(_with_topology("r202639-c", topology))
    assert _role_eids(topo, "solar") == [
        topology["circuits"]["circuit-1"]["entities"]["produced_energy"]
    ]


def _off_entrance(stem: str = "r202639-c") -> SpanPanelFixture:
    f = load_fixture(stem)
    f.states["sensor.span_panel_current_power"]["attributes"]["at_service_entrance"] = False
    return f


def test_grid_not_at_lugs_gap() -> None:
    _, topo, gaps = _build(_off_entrance())
    (gap,) = [g for g in gaps if g.kind == "grid_not_at_lugs"]
    assert "does not know where the service entrance is" in gap.message
    assert "leaves out whatever sits upstream" in gap.message
    assert gap.hint is not None and "Tesla integration (powerwall or tesla_fleet)" in gap.hint
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_main_meter_consumed_energy"]
    (grid,) = [a for a in topo.role_assignments if a.role == "grid_import"]
    assert grid.rate_entity_id == "sensor.span_panel_current_power"


def test_grid_not_at_lugs_uses_vendor_site_meter() -> None:
    powerwall = EnergyIntegration(
        platform="powerwall", devices=[],
        energy_entities=[
            _entity("sensor.powerwall_battery_import", "powerwall"),
            _entity("sensor.powerwall_battery_export", "powerwall"),
            _entity("sensor.powerwall_site_import", "powerwall"),
            _entity("sensor.powerwall_site_export", "powerwall"),
        ],
    )
    _, topo, gaps = _build(_off_entrance(), integrations=[powerwall])
    assert _role_eids(topo, "grid_import") == ["sensor.powerwall_site_import"]
    assert _role_eids(topo, "grid_export") == ["sensor.powerwall_site_export"]
    assert _role_eids(topo, "grid_import", preferred=False) == [
        "sensor.span_panel_main_meter_consumed_energy"
    ]
    (site,) = [a for a in topo.role_assignments if a.role == "grid_import" and a.preferred]
    assert site.rate_entity_id == "sensor.span_panel_grid_power"
    assert "grid_not_at_lugs" not in _kinds(gaps)


def test_grid_not_at_lugs_cleared_by_grid_ct() -> None:
    f = _off_entrance("r202639-b")
    _, topo, gaps = _build(f, roles=("remote-ct-3=grid",))
    assert "grid_not_at_lugs" not in _kinds(gaps)
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_remote_ct_3_consumed_energy"]


def test_main_meter_produced_missing_gap() -> None:
    f = load_fixture("r202639-c")
    f.raw_entities = [
        {**e, "disabled_by": "user"}
        if e["entity_id"] == "sensor.span_panel_main_meter_produced_energy" else e
        for e in f.raw_entities
    ]
    _, topo, gaps = _build(f)
    (gap,) = [g for g in gaps if g.kind == "main_meter_produced_missing"]
    assert "span_xg-7814-15i6h_main_meter_produced_energy" in gap.message
    assert not _role_eids(topo, "grid_export")


def test_role_feedthrough_subpanel() -> None:
    lead, sub = _two_panels()
    _, topo, gaps = _build(lead, sub, roles=("xg-7814-15i6h:feedthrough=subpanel:xg-1334-066e8",))
    consumers = _consumers(topo)
    assert consumers["sensor.p2_main_meter_consumed_energy"].parent_entity_id is None
    # No lead circuit is taken out of consumption.
    assert "sensor.span_panel_living_room_lights_consumed_energy" in consumers
    assert {"feed_unknown", "subpanel_feed_unknown"}.isdisjoint(_kinds(gaps))


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ("feedthrough=subpanel:xg-1334-066e8", "needs the feeding panel's serial"),
        ("xg-7814-15i6h:feedthrough=load", "takes only subpanel"),
    ],
)
def test_role_feedthrough_validation(spec: str, match: str) -> None:
    panels, _ = _panels(*_two_panels())
    with pytest.raises(CircuitRoleError, match=match):
        resolve_circuit_roles((spec,), panels)


def test_gap_subpanel_suggests_feedthrough() -> None:
    lead, sub = _two_panels()
    sub.states["sensor.p2_current_power"]["state"] = "1200"
    sub.states["sensor.p2_current_power"]["attributes"]["at_service_entrance"] = False
    lead.states["sensor.span_panel_feed_through_power"] = {"state": "1195", "attributes": {}}
    _, _, gaps = _build(lead, sub)
    (gap,) = [g for g in gaps if g.kind == "subpanel_feed_unknown"]
    assert gap.suggestions[0].endswith("xg-7814-15i6h:feedthrough=subpanel:xg-1334-066e8")


def test_gap_subpanel_asks_to_enable_feedthrough() -> None:
    lead, sub = _two_panels()
    sub.states["sensor.p2_current_power"]["attributes"]["at_service_entrance"] = False
    _, _, gaps = _build(lead, sub)
    (gap,) = [g for g in gaps if g.kind == "subpanel_feed_unknown"]
    assert "enable sensor.span_panel_feed_through_power on xg-7814-15i6h" in gap.message
    assert "feedthrough=subpanel:xg-1334-066e8" in (gap.hint or "")


# ---------------------------------------------------------------------------
# Gap wording: vendor integration, missing suggestions
# ---------------------------------------------------------------------------


def test_gap_hints_name_vendor_integration() -> None:
    _, _, gaps = _build(load_fixture("main32_r202633"))
    by_kind = {g.kind: g for g in gaps}
    assert "Tesla integration (powerwall or tesla_fleet)" in (by_kind["battery_no_energy"].hint or "")
    assert "Enphase Energy integration (enphase_envoy)" in (by_kind["solar_no_energy"].hint or "")


def test_gap_battery_integration_without_battery_entities() -> None:
    powerwall = EnergyIntegration(
        platform="powerwall", devices=[],
        energy_entities=[_entity("sensor.powerwall_battery_charged", "powerwall")],
    )
    _, topo, gaps = _build(load_fixture("r202639-c"), integrations=[powerwall])
    (gap,) = [g for g in gaps if g.kind == "battery_no_energy"]
    assert "the powerwall integration has no energy sensor" in gap.message
    assert not _role_eids(topo, "battery_charge")


@pytest.mark.parametrize(
    ("stem", "kind", "reason"),
    [
        ("main32_r202633", "solar_no_energy", "PV power reads 0 W; rerun while it is producing"),
        ("main32_r202639", "battery_no_energy", "battery power reads 0 W; rerun while it is"),
        ("main32_r202639-unpublished-readings", "battery_no_energy",
         "battery power reading unavailable"),
    ],
)
def test_gap_says_why_no_suggestion(stem: str, kind: str, reason: str) -> None:
    _, _, gaps = _build(load_fixture(stem))
    (gap,) = [g for g in gaps if g.kind == kind]
    assert gap.suggestions == []
    assert f"No suggestion: {reason}" in gap.message


# ---------------------------------------------------------------------------
# energy-audit without an admin token; Energy? column
# ---------------------------------------------------------------------------


def test_cli_energy_audit_prune_without_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = {"device_consumption": [{"stat_consumption": "sensor.gone"}]}
    fake = FakeClient([load_fixture("r202639-c")], prefs=prefs, topology_error=HAClientError(
        "span_panel/panel_topology failed: Unauthorized", code="unauthorized"
    ))
    monkeypatch.setattr(console, "width", 200)
    result = _run(fake, "--dry-run", "energy-audit", "--prune", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Skipping the span_panel misplaced-consumer check" in result.output
    assert "sensor.gone" in result.output
    assert "Would drop 1 stale reference" in result.output


def test_circuit_table_energy_column_follows_assigned_role() -> None:
    from hass_atlas.output import render_span_panel_circuits

    panels, topo, _ = _build(load_fixture("main32_r202639"))
    assigned = {a.entity_id for a in topo.role_assignments if a.preferred}
    pv = "sensor.span_panel_commissioned_pv_system"
    width = console.width
    console.width = 300
    try:
        for listed, mark in (
            ({f"{pv}_produced_energy"}, " Y "),
            ({f"{pv}_consumed_energy"}, " extra "),
        ):
            with console.capture() as cap:
                render_span_panel_circuits(panels, {}, {}, listed, assigned=assigned)
            (row,) = [line for line in cap.get().splitlines() if "Commissioned PV" in line]
            assert row.rstrip(" │").endswith(mark.rstrip()), row
    finally:
        console.width = width


async def test_fetch_energy_prefs_not_found_is_empty() -> None:
    """A fresh HA answers not_found until the dashboard is first saved."""
    from unittest.mock import AsyncMock

    from hass_atlas.ha_client import HAClientError
    from hass_atlas.registry import fetch_energy_prefs

    client = AsyncMock()
    client.send_command.side_effect = HAClientError("energy/get_prefs failed: No prefs", code="not_found")
    assert await fetch_energy_prefs(client) == {}


def test_service_entrance_unknown_gap() -> None:
    """With no at_service_entrance attribute, hass-atlas says it does not know and asks."""
    _, _, gaps = _build(load_fixture("r202639-c"))
    (gap,) = [g for g in gaps if g.kind == "service_entrance_unknown"]
    assert gap.serial == "xg-7814-15i6h"
    assert "Please confirm" in gap.message
    assert "--circuit-role xg-7814-15i6h:<ct-id>=grid" in (gap.hint or "")


def test_service_entrance_known_has_no_unknown_gap() -> None:
    _, _, gaps = _build(_off_entrance())
    assert not [g for g in gaps if g.kind == "service_entrance_unknown"]


# ---------------------------------------------------------------------------
# Synthetic replays: circuit roles on panels without native PV/storage wiring
# ---------------------------------------------------------------------------

SOLAR_STORAGE = "other-solar-storage"  # edited r202639-c
SUBPANEL = "other-subpanel"  # edited r202639-e
SOLAR_ONLY = "other-solar-only"  # edited r202639-b


@pytest.mark.parametrize("stem", SYNTHETIC_STEMS)
def test_synthetic_loader_ids_and_states(stem: str) -> None:
    f = load_synthetic(stem)
    device_ids = {d["id"] for d in f.raw_devices}
    assert f.topology["panel_device_id"] in device_ids
    assert set(f.topology["sub_devices"]) <= device_ids
    # Recorded attributes replace the derived ones.
    assert f.states["sensor.span_panel_current_power"]["attributes"]["friendly_name"]


@pytest.mark.parametrize(
    ("stem", "cid", "eid", "rate"),
    [
        (SOLAR_STORAGE, "circuit-4", "sensor.span_panel_other_produced_energy",
         "sensor.span_panel_solar_pv_power"),
        (SOLAR_ONLY, "circuit-32", "sensor.span_panel_breaker_16_produced_energy",
         "sensor.span_panel_solar_pv_power"),
    ],
)
def test_synthetic_solar_from_is_generation(stem: str, cid: str, eid: str, rate: str) -> None:
    panels, topo, gaps = _build(load_synthetic(stem))
    (p,) = panels
    circuit = next(c for c in p.circuits if c.id == cid)
    assert circuit.device_type == "circuit" and circuit.is_generation is True
    (solar,) = [a for a in topo.role_assignments if a.role == "solar"]
    assert solar.entity_id == eid
    assert solar.rate_entity_id == rate
    assert circuit.entities["consumed_energy"] not in _consumers(topo)
    assert "solar_no_energy" not in _kinds(gaps)
    # Every other in-panel circuit is still a consumer.
    others = {
        c.entities["consumed_energy"] for c in p.circuits
        if c.id != cid and not c.remote_ct and not p.non_key_meter_member(c)
    }
    assert others <= set(_consumers(topo))


def test_synthetic_solar_circuit_flagged_when_listed_as_consumer() -> None:
    panels, _ = _panels(load_synthetic(SOLAR_STORAGE))
    prefs = {"device_consumption": [
        {"stat_consumption": "sensor.span_panel_other_consumed_energy"},
    ]}
    assert _kinds(find_misplaced_consumers(panels, prefs)) == ["consumer_generation"]


def test_synthetic_solar_only_keeps_remote_ct_gap() -> None:
    _, topo, gaps = _build(load_synthetic(SOLAR_ONLY))
    assert [g.circuit_id for g in gaps if g.kind == "unclassified_remote_ct"] == ["remote-ct-3"]
    assert "sensor.span_panel_remote_ct_3_consumed_energy" not in _consumers(topo)
    # The lugs publish no at_service_entrance in this replay.
    assert "service_entrance_unknown" in _kinds(gaps)


def test_synthetic_storage_circuit_is_a_consumer_until_given_battery() -> None:
    # The payload carries no feeds role, so the STORAGE circuit looks like a load.
    storage = "sensor.span_panel_bathroom_outlets_consumed_energy"
    payload = load_json("topology", SOLAR_STORAGE, SYNTHETIC_DIR)
    assert all("feeds_role" not in c for c in payload["circuits"].values())

    _, topo, gaps = _build(load_synthetic(SOLAR_STORAGE))
    assert storage in _consumers(topo)
    (gap,) = [g for g in gaps if g.kind == "battery_no_energy"]
    assert "--circuit-role xg-7814-15i6h:<circuit-id>=battery" in (gap.hint or "")
    assert not _role_eids(topo, "battery_charge")

    _, topo, gaps = _build(load_synthetic(SOLAR_STORAGE), roles=("circuit-2=battery",))
    assert storage not in _consumers(topo)
    assert _role_eids(topo, "battery_charge") == [storage]
    assert _role_eids(topo, "battery_discharge") == [
        "sensor.span_panel_bathroom_outlets_produced_energy"
    ]
    assert "battery_no_energy" not in _kinds(gaps)


def test_synthetic_storage_circuit_listed_as_consumer_with_battery_role() -> None:
    panels, _ = _panels(load_synthetic(SOLAR_STORAGE))
    prefs = {"device_consumption": [
        {"stat_consumption": "sensor.span_panel_bathroom_outlets_consumed_energy"},
    ]}
    assert find_misplaced_consumers(panels, prefs) == []
    roles = resolve_circuit_roles(("circuit-2=battery",), panels)
    assert _kinds(find_misplaced_consumers(panels, prefs, roles)) == ["consumer_role_mismatch"]


def test_synthetic_at_service_entrance_false_from_recorded_attributes() -> None:
    f = load_synthetic(SOLAR_STORAGE)
    attrs = f.states["sensor.span_panel_current_power"]["attributes"]
    assert attrs["at_service_entrance"] is False
    _, topo, gaps = _build(f)
    (gap,) = [g for g in gaps if g.kind == "grid_not_at_lugs"]
    assert gap.serial == "xg-7814-15i6h"
    assert "service_entrance_unknown" not in _kinds(gaps)
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_main_meter_consumed_energy"]


def test_synthetic_cli_audit_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_synthetic(SOLAR_STORAGE)])
    monkeypatch.setattr(console, "width", 10_000)
    result = _run(fake, "audit", "--format", "json", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    kinds = {g["kind"] for g in json.loads(result.output)["gaps"]}
    assert {"grid_not_at_lugs", "battery_no_energy"} <= kinds
    assert "service_entrance_unknown" not in kinds


def _subpanel_pair() -> tuple[SpanPanelFixture, SpanPanelFixture]:
    # Laundry Room (circuit-4) on the edited r202639-e is the SUBPANEL feed.
    return load_synthetic(SUBPANEL), renamed_fixture(SOLAR_ONLY, "p2", SYNTHETIC_DIR)


LEAD, SUB = "xg-6159-74dlx", "xg-1155-18fkf"
FEED = "sensor.span_panel_laundry_room_consumed_energy"


def test_synthetic_subpanel_feed_unknown_without_role() -> None:
    _, topo, gaps = _build(*_subpanel_pair())
    assert "feed_unknown" in _kinds(gaps)
    assert FEED in _consumers(topo)
    assert sorted(_role_eids(topo, "grid_import")) == [
        "sensor.p2_main_meter_consumed_energy",
        "sensor.span_panel_main_meter_consumed_energy",
    ]


def test_synthetic_subpanel_role() -> None:
    panels, topo, gaps = _build(*_subpanel_pair(), roles=(f"circuit-4=subpanel:{SUB}",))
    assert _role_eids(topo, "grid_import") == ["sensor.span_panel_main_meter_consumed_energy"]
    assert _role_eids(topo, "grid_export") == ["sensor.span_panel_main_meter_produced_energy"]
    consumers = _consumers(topo)
    assert FEED not in consumers
    sub_main = "sensor.p2_main_meter_consumed_energy"
    assert consumers[sub_main].parent_entity_id is None
    assert consumers[sub_main].reason == f"Sub-panel main meter, fed by {LEAD}:circuit-4"
    sub_circuits = {a.parent_entity_id for eid, a in consumers.items()
                    if eid.startswith("sensor.p2_") and eid != sub_main}
    assert sub_circuits == {sub_main}
    # The sub-panel keeps its generation circuit as solar.
    assert _role_eids(topo, "solar") == ["sensor.p2_breaker_16_produced_energy"]
    assert {"feed_unknown", "subpanel_feed_unknown"}.isdisjoint(_kinds(gaps))
    # Shared meter groups on the lead still count the key member once.
    lead = next(p for p in panels if p.serial == LEAD)
    members = {c.id: c for c in lead.circuits}
    assert members["circuit-52"].entities["consumed_energy"] in consumers
    assert members["circuit-53"].entities["consumed_energy"] not in consumers


# ---------------------------------------------------------------------------
# energy: saves
# ---------------------------------------------------------------------------

ALL_FIXTURES = [(load_fixture, s) for s in STEMS] + [(load_synthetic, s) for s in SYNTHETIC_STEMS]


@pytest.mark.parametrize("args", [["energy"], ["energy", "--topology"]])
@pytest.mark.parametrize(("load", "stem"), ALL_FIXTURES)
def test_cli_energy_saves_valid_prefs(
    load: Any, stem: str, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClient([load(stem)])
    result = _run(fake, *args, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert fake.saved is not None
    assert required_key_errors(fake.saved, legacy=False) == []
    consumers = [d["stat_consumption"] for d in fake.saved["device_consumption"]]
    assert len(consumers) == len(set(consumers))
    sources = {e for s in fake.saved["energy_sources"] for e in _source_eids(s)}
    assert not sources & set(consumers)


def _source_eids(source: dict[str, Any]) -> set[str]:
    return {source[k] for k in ("stat_energy_from", "stat_energy_to") if source.get(k)}


def test_cli_energy_dry_run_does_not_save(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("main32_r202639")])
    result = _run(fake, "--dry-run", "energy", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert fake.saved is None
    assert not any(t == "energy/save_prefs" for t, _ in fake.sent)


def test_cli_energy_keeps_user_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    user_source = {"type": "gas", "stat_energy_from": "sensor.gas_meter"}
    user_consumer = {"stat_consumption": "sensor.fridge_plug_energy"}
    fake = FakeClient(
        [load_fixture("main32_r202639")],
        prefs={"energy_sources": [user_source], "device_consumption": [user_consumer]},
    )
    result = _run(fake, "energy", "--topology", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert fake.saved is not None
    assert user_source in fake.saved["energy_sources"]
    assert user_consumer in fake.saved["device_consumption"]


def test_cli_energy_applies_circuit_role(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient([load_fixture("r202639-b")])
    result = _run(
        fake, "energy", "--topology", "--circuit-role", "remote-ct-3=grid",
        monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert fake.saved is not None
    (grid,) = [s for s in fake.saved["energy_sources"] if s["type"] == "grid"]
    assert grid["stat_energy_from"] == "sensor.span_panel_remote_ct_3_consumed_energy"


def test_saved_prefs_pass_ha_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = []
    for load, stem in ALL_FIXTURES:
        fake = FakeClient([load(stem)])
        result = _run(fake, "energy", "--topology", monkeypatch=monkeypatch)
        assert result.exit_code == 0, result.output
        saved.append(fake.saved)
    checked = validate_with_ha(saved)
    if checked is None:
        pytest.skip("no Home Assistant install to validate against")
    _, errors = checked
    assert errors == [None] * len(saved)
