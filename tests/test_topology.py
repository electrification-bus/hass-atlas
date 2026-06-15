"""Tests for topology-aware energy system analysis."""

from __future__ import annotations


from hass_atlas.energy import build_topology_aware_config
from hass_atlas.models import HADevice, HAEntity, SpanDeviceTree
from hass_atlas.topology import (
    VENDOR_PLATFORM_MAP,
    EnergyIntegration,
    SpanTopology,
    _circuit_node_id,
    _find_platform_for_vendor,
    build_energy_topology,
    classify_circuits,
    discover_energy_integrations,
    extract_span_topology,
)
from tests.conftest import (
    BESS_FEED_CIRCUIT_NODE_ID,
    CIRCUIT_1_NODE_ID,
    PV_FEED_CIRCUIT_DEVICE_ID,
    PV_FEED_CIRCUIT_NODE_ID,
    PW_DEVICE_ID,
    SERIAL,
    make_panel_tree_with_lugs,
    make_platform_entity,
)


# ---------------------------------------------------------------------------
# discover_energy_integrations
# ---------------------------------------------------------------------------


def test_discover_energy_integrations_multi_platform(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
    panel_device: HADevice,
    site_meter_device: HADevice,
) -> None:
    """Find energy integrations across multiple platforms, excluding span_ebus."""
    all_devices = [powerwall_device, enphase_device, panel_device]
    # Include span_ebus entities too — they should be excluded
    span_entities = list(site_meter_device.entities)
    all_entities = powerwall_entities + enphase_entities + span_entities

    integrations = discover_energy_integrations(all_devices, all_entities)

    platforms = {i.platform for i in integrations}
    assert "powerwall" in platforms
    assert "enphase_envoy" in platforms
    assert "span_ebus" not in platforms  # excluded

    pw_int = next(i for i in integrations if i.platform == "powerwall")
    assert len(pw_int.energy_entities) == 4
    assert pw_int.devices == [powerwall_device]

    enphase_int = next(i for i in integrations if i.platform == "enphase_envoy")
    assert len(enphase_int.energy_entities) == 1


def test_discover_energy_integrations_no_energy_entities() -> None:
    """No energy entities found at all."""
    devices = [HADevice(id="d1", name="Hue", identifiers=[("hue", "123")])]
    entities = [HAEntity(
        entity_id="light.hue_1", unique_id="h1", platform="hue",
        device_id="d1", device_class=None, state_class=None,
    )]
    assert discover_energy_integrations(devices, entities) == []


def test_discover_energy_integrations_skips_disabled(
    powerwall_device: HADevice,
) -> None:
    """Disabled energy entities should be excluded."""
    disabled = make_platform_entity(
        "sensor.powerwall_disabled", "pw_disabled", "powerwall",
        PW_DEVICE_ID, device_class="energy", state_class="total_increasing",
    )
    disabled.disabled_by = "user"
    integrations = discover_energy_integrations([powerwall_device], [disabled])
    assert integrations == []


# ---------------------------------------------------------------------------
# extract_span_topology
# ---------------------------------------------------------------------------


def test_extract_span_topology_lead_panel(span_tree_def34, states_def34) -> None:
    """Lead panel with UPSTREAM BESS, derived from lugs-up connection capability.

    span-hass 0.2.x publishes ``<panel>-lugs-up/connection/fed-by-device-id``
    when an upstream DER feeds the panel; the topology engine reads that to
    detect UPSTREAM position.  The def34 fixture has this property populated
    pointing at its BESS.
    """
    topos = extract_span_topology([span_tree_def34], states_def34)

    assert len(topos) == 1
    t = topos[0]
    assert t.serial == "nt-0000-def34"
    assert t.is_lead_panel is True
    assert t.battery_position == "UPSTREAM"
    assert t.battery_vendor == "Tesla"
    # PV identity is published; IN_PANEL position requires a circuit with
    # connection/feeds-device-id pointing at the PV, which the fixture does
    # not currently include — position stays None.
    assert t.solar_vendor == "Enphase Energy"
    assert t.solar_position is None


def test_extract_span_topology_sub_panel() -> None:
    """Sub-panel detection: panel.via_device_id is non-None → is_lead_panel=False.

    span-hass 0.2.x does not yet publish the upstream-panel ``fed-by-device-id``
    on a sub-panel's lugs-up (a known firmware gap — see CHANGELOG §Known
    issues), so DER positions cannot be derived from the wire alone on a
    sub-panel; the topology engine reports None for those fields here.  The
    HA device-registry via_device_id linkage (set via ``link_subpanel``) is
    still authoritative for the panel hierarchy.
    """
    sub = make_panel_tree_with_lugs(
        "nt-0000-sub01",
        num_circuits=1,
        via_device_id="dev-nt-0000-lead1",  # marks this as a sub-panel
    )

    topos = extract_span_topology([sub], {})
    assert len(topos) == 1
    t = topos[0]
    assert t.is_lead_panel is False
    assert t.battery_position is None
    assert t.solar_position is None
    assert t.solar_feed_circuit_id is None


def test_extract_span_topology_no_states(span_tree: SpanDeviceTree) -> None:
    """No topology states available — all fields should be None."""
    topos = extract_span_topology([span_tree], {})
    assert len(topos) == 1
    t = topos[0]
    assert t.battery_position is None
    assert t.solar_position is None
    assert t.battery_vendor is None


# ---------------------------------------------------------------------------
# classify_circuits
# ---------------------------------------------------------------------------


def test_classify_circuits_pure_load(span_tree: SpanDeviceTree) -> None:
    """Without PV/BESS feed info, all circuits classify as pure 'load'."""
    topo = SpanTopology(serial=span_tree.serial)  # No PV/BESS feed info
    roles = classify_circuits([span_tree], [topo])

    assert len(roles) == len(span_tree.circuits)  # abc12: 11 circuits
    for cr in roles:
        assert cr.role == "load"
        assert cr.skip_return_energy is True
        assert cr.skip_consumption is False


def test_classify_circuits_pv_feed_in_panel(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """PV feed circuit IN_PANEL: keep return energy (= solar), keep consumption."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="IN_PANEL",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    roles = classify_circuits([tree], [topo])

    pv_role = next(r for r in roles if r.circuit.id == PV_FEED_CIRCUIT_DEVICE_ID)
    assert pv_role.role == "pv_feed"
    assert pv_role.skip_return_energy is False  # solar production
    assert pv_role.skip_consumption is False  # parasitic load

    # Other circuits are pure load
    load_roles = [r for r in roles if r.role == "load"]
    assert len(load_roles) == 2
    for lr in load_roles:
        assert lr.skip_return_energy is True


def test_classify_circuits_pv_feed_upstream(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
) -> None:
    """PV feed circuit UPSTREAM: skip return energy."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit],
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="UPSTREAM",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    roles = classify_circuits([tree], [topo])

    assert len(roles) == 1
    assert roles[0].role == "pv_feed"
    assert roles[0].skip_return_energy is True


def test_classify_circuits_bess_feed_in_panel(
    panel_device: HADevice,
    site_meter_device: HADevice,
    bess_feed_circuit: HADevice,
) -> None:
    """BESS feed circuit IN_PANEL: keep return (= discharge), skip consumption."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[bess_feed_circuit],
    )
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="IN_PANEL",
        battery_feed_circuit_id=BESS_FEED_CIRCUIT_NODE_ID,
    )
    roles = classify_circuits([tree], [topo])

    assert len(roles) == 1
    cr = roles[0]
    assert cr.role == "bess_feed"
    assert cr.skip_return_energy is False  # battery discharge
    assert cr.skip_consumption is True  # both directions are battery ops


def test_classify_circuits_bess_feed_upstream(
    panel_device: HADevice,
    site_meter_device: HADevice,
    bess_feed_circuit: HADevice,
) -> None:
    """BESS feed circuit UPSTREAM: skip return, keep consumption."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[bess_feed_circuit],
    )
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="UPSTREAM",
        battery_feed_circuit_id=BESS_FEED_CIRCUIT_NODE_ID,
    )
    roles = classify_circuits([tree], [topo])

    assert len(roles) == 1
    assert roles[0].role == "bess_feed"
    assert roles[0].skip_return_energy is True
    assert roles[0].skip_consumption is False


# ---------------------------------------------------------------------------
# Vendor matching
# ---------------------------------------------------------------------------


def test_vendor_matching_tesla(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Tesla vendor matches powerwall platform."""
    integrations = [
        EnergyIntegration(
            platform="powerwall",
            devices=[powerwall_device],
            energy_entities=powerwall_entities,
        )
    ]
    result = _find_platform_for_vendor("Tesla", integrations)
    assert result is not None
    assert result.platform == "powerwall"


def test_vendor_matching_enphase(
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
) -> None:
    """Enphase vendor matches enphase_envoy platform."""
    integrations = [
        EnergyIntegration(
            platform="enphase_envoy",
            devices=[enphase_device],
            energy_entities=enphase_entities,
        )
    ]
    result = _find_platform_for_vendor("Enphase", integrations)
    assert result is not None
    assert result.platform == "enphase_envoy"


def test_vendor_matching_enphase_energy(
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
) -> None:
    """'Enphase Energy' (full vendor name from SPAN metadata) matches enphase_envoy."""
    integrations = [
        EnergyIntegration(
            platform="enphase_envoy",
            devices=[enphase_device],
            energy_entities=enphase_entities,
        )
    ]
    result = _find_platform_for_vendor("Enphase Energy", integrations)
    assert result is not None
    assert result.platform == "enphase_envoy"


def test_vendor_matching_no_match() -> None:
    """Unknown vendor returns None."""
    assert _find_platform_for_vendor("UnknownVendor", []) is None


def test_vendor_matching_none_vendor() -> None:
    """None vendor returns None."""
    assert _find_platform_for_vendor(None, []) is None


def test_vendor_platform_map_structure() -> None:
    """VENDOR_PLATFORM_MAP has expected entries."""
    assert "tesla" in VENDOR_PLATFORM_MAP
    assert "powerwall" in VENDOR_PLATFORM_MAP["tesla"]
    assert "enphase" in VENDOR_PLATFORM_MAP
    assert "enphase_envoy" in VENDOR_PLATFORM_MAP["enphase"]


# ---------------------------------------------------------------------------
# build_energy_topology — overlap detection (the core integration tests)
# ---------------------------------------------------------------------------


def _make_pw_integration(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> EnergyIntegration:
    return EnergyIntegration(
        platform="powerwall",
        devices=[powerwall_device],
        energy_entities=powerwall_entities,
    )


def _make_enphase_integration(
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
) -> EnergyIntegration:
    return EnergyIntegration(
        platform="enphase_envoy",
        devices=[enphase_device],
        energy_entities=enphase_entities,
    )


def test_detect_overlaps_bess_upstream_with_powerwall(
    span_tree: SpanDeviceTree,
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """BESS UPSTREAM + Powerwall integration → prefer Powerwall for grid + battery."""
    topo = SpanTopology(
        serial=span_tree.serial,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    # Grid should come from powerwall
    grid_preferred = [a for a in result.role_assignments if a.role == "grid_import" and a.preferred]
    assert len(grid_preferred) == 1
    assert grid_preferred[0].platform == "powerwall"
    assert "site_import" in grid_preferred[0].entity_id

    # SPAN grid should be non-preferred (post-battery, not true grid)
    grid_span = [a for a in result.role_assignments if a.role == "grid_import" and not a.preferred]
    assert len(grid_span) >= 1
    assert all(a.platform == "span_ebus" for a in grid_span)

    # Battery should come from powerwall
    batt_preferred = [
        a for a in result.role_assignments
        if a.role in ("battery_discharge", "battery_charge") and a.preferred
    ]
    assert len(batt_preferred) >= 1
    assert all(a.platform == "powerwall" for a in batt_preferred)

    assert len(result.warnings) >= 1


def test_detect_overlaps_bess_upstream_no_powerwall(
    span_tree: SpanDeviceTree,
) -> None:
    """BESS UPSTREAM but no matching integration → use SPAN for grid."""
    topo = SpanTopology(
        serial=span_tree.serial,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
    )
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], [], circuit_roles)

    # Grid should come from SPAN (no alternative)
    grid_preferred = [a for a in result.role_assignments if a.role == "grid_import" and a.preferred]
    assert len(grid_preferred) == 1
    assert grid_preferred[0].platform == "span_ebus"


def test_detect_overlaps_pv_in_panel_uses_span_circuit(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
) -> None:
    """PV IN_PANEL → prefer SPAN circuit for solar, Enphase non-preferred."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="IN_PANEL",
        solar_vendor="Enphase",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    integrations = [_make_enphase_integration(enphase_device, enphase_entities)]
    circuit_roles = classify_circuits([tree], [topo])

    result = build_energy_topology([tree], [topo], integrations, circuit_roles)

    solar_preferred = [a for a in result.role_assignments if a.role == "solar" and a.preferred]
    assert len(solar_preferred) == 1
    assert solar_preferred[0].platform == "span_ebus"
    # Must be the PV feed circuit's imported-energy (return = generation)
    assert "energy_returned" in solar_preferred[0].entity_id

    # Enphase should be non-preferred
    solar_skip = [a for a in result.role_assignments if a.role == "solar" and not a.preferred]
    assert len(solar_skip) >= 1
    assert all(a.platform == "enphase_envoy" for a in solar_skip)


def test_detect_overlaps_pv_upstream_uses_dedicated(
    span_tree: SpanDeviceTree,
    enphase_device: HADevice,
    enphase_entities: list[HAEntity],
) -> None:
    """PV UPSTREAM → prefer dedicated Enphase integration for solar."""
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="UPSTREAM",
        solar_vendor="Enphase",
    )
    integrations = [_make_enphase_integration(enphase_device, enphase_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    solar_preferred = [a for a in result.role_assignments if a.role == "solar" and a.preferred]
    assert len(solar_preferred) == 1
    assert solar_preferred[0].platform == "enphase_envoy"


def test_detect_overlaps_bess_in_panel_uses_span_circuit(
    panel_device: HADevice,
    site_meter_device: HADevice,
    bess_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """BESS IN_PANEL → prefer SPAN circuit for battery, Powerwall non-preferred."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[bess_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="IN_PANEL",
        battery_vendor="Tesla",
        battery_feed_circuit_id=BESS_FEED_CIRCUIT_NODE_ID,
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([tree], [topo])

    result = build_energy_topology([tree], [topo], integrations, circuit_roles)

    # Battery discharge from SPAN circuit imported-energy
    discharge_preferred = [
        a for a in result.role_assignments
        if a.role == "battery_discharge" and a.preferred
    ]
    assert len(discharge_preferred) == 1
    assert discharge_preferred[0].platform == "span_ebus"

    # Battery charge from SPAN circuit exported-energy
    charge_preferred = [
        a for a in result.role_assignments
        if a.role == "battery_charge" and a.preferred
    ]
    assert len(charge_preferred) == 1
    assert charge_preferred[0].platform == "span_ebus"

    # Powerwall battery should be non-preferred
    pw_batt = [
        a for a in result.role_assignments
        if a.role in ("battery_discharge", "battery_charge")
        and a.platform == "powerwall"
    ]
    assert all(not a.preferred for a in pw_batt)


def test_detect_overlaps_bess_upstream_uses_dedicated(
    span_tree: SpanDeviceTree,
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """BESS UPSTREAM → prefer Powerwall for battery."""
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    batt_preferred = [
        a for a in result.role_assignments
        if a.role in ("battery_discharge", "battery_charge") and a.preferred
    ]
    assert len(batt_preferred) >= 1
    assert all(a.platform == "powerwall" for a in batt_preferred)


def test_detect_overlaps_span_only(span_tree: SpanDeviceTree) -> None:
    """No other integrations, no UPSTREAM BESS — all SPAN preferred."""
    topo = SpanTopology(serial=span_tree.serial)
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], [], circuit_roles)

    preferred = [a for a in result.role_assignments if a.preferred]
    assert all(a.platform == "span_ebus" for a in preferred)

    # Grid comes from the upstream lugs
    grid = [a for a in preferred if a.role == "grid_import"]
    assert len(grid) == 1

    # One device_consumption entry per circuit (abc12: 11 circuits)
    consumption = [a for a in preferred if a.role == "device_consumption"]
    assert len(consumption) == len(span_tree.circuits)


def test_device_consumption_excludes_bess_feed(
    panel_device: HADevice,
    site_meter_device: HADevice,
    bess_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """BESS feed circuit (IN_PANEL) should be excluded from device_consumption."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[bess_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="IN_PANEL",
        battery_feed_circuit_id=BESS_FEED_CIRCUIT_NODE_ID,
    )
    circuit_roles = classify_circuits([tree], [topo])

    result = build_energy_topology([tree], [topo], [], circuit_roles)

    consumption = [a for a in result.role_assignments if a.role == "device_consumption"]
    consumption_ids = {a.entity_id for a in consumption}

    # BESS feed circuit should NOT be in consumption
    assert "sensor.span_battery_circuit_energy" not in consumption_ids
    # Regular circuits should be
    assert "sensor.span_kitchen_energy" in consumption_ids
    assert "sensor.span_garage_energy" in consumption_ids


def test_device_consumption_includes_pv_feed(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """PV feed circuit (IN_PANEL): consumption (parasitic load) is included."""
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="IN_PANEL",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    circuit_roles = classify_circuits([tree], [topo])

    result = build_energy_topology([tree], [topo], [], circuit_roles)

    consumption = [a for a in result.role_assignments if a.role == "device_consumption"]
    consumption_ids = {a.entity_id for a in consumption}

    # PV feed circuit consumption IS included (parasitic load)
    assert "sensor.span_pv_system_energy" in consumption_ids


# ---------------------------------------------------------------------------
# build_topology_aware_config (energy.py)
# ---------------------------------------------------------------------------


def test_build_topology_aware_config_full(
    span_tree: SpanDeviceTree,
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Full topology-aware config: BESS UPSTREAM → Powerwall grid + battery, SPAN circuits."""
    topo = SpanTopology(
        serial=span_tree.serial,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])
    energy_topo = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    config = build_topology_aware_config(energy_topo)

    # Grid from powerwall
    grid_sources = [s for s in config["energy_sources"] if s["type"] == "grid"]
    assert len(grid_sources) == 1
    assert any("powerwall_site_import" in f["stat_energy_from"]
               for f in grid_sources[0]["flow_from"])

    # Battery from powerwall
    batt_sources = [s for s in config["energy_sources"] if s["type"] == "battery"]
    assert len(batt_sources) == 1
    assert "powerwall" in batt_sources[0].get("stat_energy_from", "")

    # No solar source — abc12 has no PV device.  (Solar IN_PANEL requires a
    # circuit's ``connection/feeds-device-id`` to point at the PV; covered in
    # the PV-specific overlap tests below.)
    solar_sources = [s for s in config["energy_sources"] if s["type"] == "solar"]
    assert len(solar_sources) == 0

    # One device_consumption per circuit, plus a panel-tier root entry for
    # the Sankey hierarchy (BESS upstream → SPAN lugs is non-preferred grid
    # and surfaces as device_consumption).
    consumption_ids = {d["stat_consumption"] for d in config["device_consumption"]}
    panel_eid = next(iter(c for c in consumption_ids if "lugs_imported_energy" in c), None)
    assert panel_eid is not None
    # Every circuit gets an entry
    circuit_consumption = {c for c in consumption_ids if c != panel_eid}
    assert len(circuit_consumption) == len(span_tree.circuits)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_backward_compat_basic_energy_command(span_tree: SpanDeviceTree) -> None:
    """The original build_energy_config still works against the tree-v1 shape.

    Note: span-hass 0.2.x's BESS and PV devices don't expose a ``meter``
    capability, so the non-topology path only proposes grid (from lugs_up)
    + circuit consumption.  Use the topology-aware path for solar/battery
    sources sourced from dedicated DER integrations.
    """
    from hass_atlas.energy import build_energy_config

    config = build_energy_config([span_tree])
    source_types = {s["type"] for s in config["energy_sources"]}
    assert source_types == {"grid"}
    assert len(config["device_consumption"]) == len(span_tree.circuits)


# ---------------------------------------------------------------------------
# _circuit_node_id helper
# ---------------------------------------------------------------------------


def test_circuit_node_id_extraction(circuit_devices: list[HADevice]) -> None:
    """Extract node_id from circuit device identifiers."""
    kitchen = circuit_devices[0]
    assert _circuit_node_id(kitchen) == CIRCUIT_1_NODE_ID


def test_circuit_node_id_no_underscore() -> None:
    """Device with no underscore in identifier returns None."""
    device = HADevice(id="d1", name="X", identifiers=[("span_ebus", "just-a-serial")])
    assert _circuit_node_id(device) is None


def test_circuit_node_id_non_span() -> None:
    """Non-span device returns None."""
    device = HADevice(id="d1", name="X", identifiers=[("hue", "abc_def")])
    assert _circuit_node_id(device) is None


# ---------------------------------------------------------------------------
# build_energy_topology — rate_entity_id for power sensors
# ---------------------------------------------------------------------------


def test_build_energy_topology_circuit_rate_entity_id() -> None:
    """Circuits with active-power get rate_entity_id; circuits without get None.

    Build a synthetic tree with two circuits: the first has an active-power
    entity, the second doesn't.  ``build_energy_topology`` should set
    ``rate_entity_id`` only on the first.
    """
    tree = make_panel_tree_with_lugs(
        "nt-0000-rate1",
        num_circuits=1,
        circuit_names=["Kitchen"],
        include_circuit_power=True,
    )
    no_power = make_panel_tree_with_lugs(
        "nt-0000-rate2",
        num_circuits=1,
        circuit_names=["Garage"],
        include_circuit_power=False,
    )
    tree.circuits.append(no_power.circuits[0])

    topo = SpanTopology(serial=tree.serial, is_lead_panel=True)
    circuit_roles = classify_circuits([tree], [topo])
    result = build_energy_topology([tree], [topo], [], circuit_roles)

    consumption = [
        a for a in result.role_assignments
        if a.role == "device_consumption" and a.preferred
    ]
    kitchen = next(a for a in consumption if "kitchen" in a.entity_id.lower())
    assert kitchen.rate_entity_id == "sensor.kitchen_active_power"

    garage = next(a for a in consumption if "garage" in a.entity_id.lower())
    assert garage.rate_entity_id is None


def test_build_energy_topology_solar_rate_prefers_generation_power(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """Solar IN_PANEL: prefers generation-power (non-negated) over active-power for stat_rate."""
    from tests.conftest import make_entity, PV_FEED_CIRCUIT_DEVICE_ID, PV_FEED_CIRCUIT_NODE_ID
    # Both active-power and generation-power exist on the circuit
    pv_feed_circuit.entities.append(make_entity(
        "sensor.span_pv_system_power",
        f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}_active-power",
        PV_FEED_CIRCUIT_DEVICE_ID,
        device_class="power",
    ))
    pv_feed_circuit.entities.append(make_entity(
        "sensor.span_pv_system_generation_power",
        f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}_generation-power",
        PV_FEED_CIRCUIT_DEVICE_ID,
        device_class="power",
    ))
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="IN_PANEL",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    circuit_roles = classify_circuits([tree], [topo])
    result = build_energy_topology([tree], [topo], [], circuit_roles)

    solar = next(a for a in result.role_assignments if a.role == "solar" and a.preferred)
    assert solar.rate_entity_id == "sensor.span_pv_system_generation_power"


def test_build_energy_topology_solar_rate_falls_back_to_active_power(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """Solar IN_PANEL: falls back to active-power when generation-power is absent."""
    from tests.conftest import make_entity, PV_FEED_CIRCUIT_DEVICE_ID, PV_FEED_CIRCUIT_NODE_ID
    pv_feed_circuit.entities.append(make_entity(
        "sensor.span_pv_system_power",
        f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}_active-power",
        PV_FEED_CIRCUIT_DEVICE_ID,
        device_class="power",
    ))
    tree = SpanDeviceTree(
        panel=panel_device,
        circuits=[pv_feed_circuit] + circuit_devices,
    )
    topo = SpanTopology(
        serial=SERIAL,
        solar_position="IN_PANEL",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    circuit_roles = classify_circuits([tree], [topo])
    result = build_energy_topology([tree], [topo], [], circuit_roles)

    solar = next(a for a in result.role_assignments if a.role == "solar" and a.preferred)
    assert solar.rate_entity_id == "sensor.span_pv_system_power"


# ---------------------------------------------------------------------------
# build_energy_topology — parent_entity_id for Sankey hierarchy
# ---------------------------------------------------------------------------


def test_build_energy_topology_panel_parents_bess_upstream(
    span_tree: SpanDeviceTree,
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """BESS UPSTREAM → panel upstream emitted as Sankey-root consumption, circuits nest under it."""
    topo = SpanTopology(
        serial=span_tree.serial,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
        is_lead_panel=True,
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    consumption = [
        a for a in result.role_assignments
        if a.role == "device_consumption" and a.preferred
    ]
    panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason]
    assert len(panel_entries) == 1
    panel_eid = panel_entries[0].entity_id
    assert panel_entries[0].parent_entity_id is None  # lead panel has no parent

    # Every circuit nests under the panel entry
    circuit_entries = [a for a in consumption if a.entity_id != panel_eid]
    assert len(circuit_entries) == len(span_tree.circuits)
    for ce in circuit_entries:
        assert ce.parent_entity_id == panel_eid


def test_build_energy_topology_no_parents_when_span_is_grid(
    span_tree: SpanDeviceTree,
) -> None:
    """No BESS UPSTREAM: panel upstream IS the grid source, no panel consumption entry."""
    topo = SpanTopology(serial=SERIAL, is_lead_panel=True)
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], [], circuit_roles)

    consumption = [a for a in result.role_assignments if a.role == "device_consumption" and a.preferred]
    # No panel-level entries (upstream is the grid source)
    panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason]
    assert len(panel_entries) == 0
    # Circuits have no parent
    for a in consumption:
        assert a.parent_entity_id is None


def test_build_energy_topology_multi_panel_hierarchy(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Lead + sub-panel: 2-level Sankey hierarchy via via_device_id."""
    lead_tree = make_panel_tree_with_lugs(
        "nt-0000-lead1",
        num_circuits=2,
        circuit_names=["Lead Kitchen", "Lead Garage"],
    )
    sub_tree = make_panel_tree_with_lugs(
        "nt-0000-sub01",
        num_circuits=1,
        circuit_names=["Sub Office"],
        via_device_id=lead_tree.panel.id,
    )

    lead_topo = SpanTopology(
        serial=lead_tree.serial, battery_position="UPSTREAM",
        battery_vendor="Tesla", is_lead_panel=True,
    )
    sub_topo = SpanTopology(
        serial=sub_tree.serial, battery_position="UPSTREAM",
        battery_vendor="Tesla", is_lead_panel=False,
    )

    trees = [lead_tree, sub_tree]
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits(trees, [lead_topo, sub_topo])

    result = build_energy_topology(trees, [lead_topo, sub_topo], integrations, circuit_roles)

    consumption = [
        a for a in result.role_assignments
        if a.role == "device_consumption" and a.preferred
    ]

    # Lead panel — no parent
    lead_panel_entries = [
        a for a in consumption
        if "Sankey hierarchy" in a.reason and a.parent_entity_id is None
    ]
    assert len(lead_panel_entries) == 1
    lead_eid = lead_panel_entries[0].entity_id

    # Sub-panel — parent is lead
    sub_panel_entries = [
        a for a in consumption
        if "Sankey hierarchy" in a.reason and a.parent_entity_id == lead_eid
    ]
    assert len(sub_panel_entries) == 1
    sub_eid = sub_panel_entries[0].entity_id

    # Exactly 2 panel-level entries total
    all_panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason]
    assert len(all_panel_entries) == 2

    # Lead circuits nest under lead panel
    lead_circuits = [
        a for a in consumption
        if a.parent_entity_id == lead_eid and "Sankey hierarchy" not in a.reason
    ]
    assert len(lead_circuits) == 2

    # Sub-panel circuits nest under sub panel
    sub_circuits = [a for a in consumption if a.parent_entity_id == sub_eid]
    assert len(sub_circuits) == 1
    assert "sub_office" in sub_circuits[0].entity_id.lower()


def test_build_energy_topology_daisy_chain_hierarchy(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Daisy-chain: lead → mid → tail.  Each panel parents to its DIRECT predecessor."""
    lead_tree = make_panel_tree_with_lugs(
        "nt-2024-a1b2c",
        num_circuits=1,
        circuit_names=["Lead Kitchen"],
    )
    mid_tree = make_panel_tree_with_lugs(
        "nt-2024-d3e4f",
        num_circuits=1,
        circuit_names=["Mid Kitchen"],
        via_device_id=lead_tree.panel.id,
    )
    tail_tree = make_panel_tree_with_lugs(
        "nt-2024-g5h6j",
        num_circuits=1,
        circuit_names=["Tail Office"],
        via_device_id=mid_tree.panel.id,
    )

    # All BESS UPSTREAM so SPAN lugs become non-preferred grid → free to use
    # as Sankey-hierarchy device_consumption parents.
    lead_topo = SpanTopology(
        serial=lead_tree.serial, battery_position="UPSTREAM",
        battery_vendor="Tesla", is_lead_panel=True,
    )
    mid_topo = SpanTopology(
        serial=mid_tree.serial, battery_position="UPSTREAM",
        battery_vendor="Tesla", is_lead_panel=False,
    )
    tail_topo = SpanTopology(
        serial=tail_tree.serial, battery_position="UPSTREAM",
        battery_vendor="Tesla", is_lead_panel=False,
    )

    # Reverse order — exercises topological sort inside build_energy_topology.
    trees = [tail_tree, mid_tree, lead_tree]
    topos = [lead_topo, mid_topo, tail_topo]
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits(trees, topos)

    result = build_energy_topology(trees, topos, integrations, circuit_roles)

    consumption = [
        a for a in result.role_assignments
        if a.role == "device_consumption" and a.preferred
    ]

    # Lead — root of the chain
    lead_entries = [
        a for a in consumption
        if "Sankey hierarchy" in a.reason and a.parent_entity_id is None
    ]
    assert len(lead_entries) == 1
    lead_eid = lead_entries[0].entity_id

    # Mid — parent is lead
    mid_entries = [
        a for a in consumption
        if "Sankey hierarchy" in a.reason and a.parent_entity_id == lead_eid
    ]
    assert len(mid_entries) == 1
    mid_eid = mid_entries[0].entity_id

    # Tail — parent is mid (NOT lead)
    tail_entries = [
        a for a in consumption
        if "Sankey hierarchy" in a.reason and a.parent_entity_id == mid_eid
    ]
    assert len(tail_entries) == 1
    tail_eid = tail_entries[0].entity_id

    # Exactly 3 panel-level entries
    all_panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason]
    assert len(all_panel_entries) == 3

    # Each panel's circuit nests under its own panel entry
    lead_circuits = [
        a for a in consumption
        if a.parent_entity_id == lead_eid and "Sankey hierarchy" not in a.reason
    ]
    assert len(lead_circuits) == 1
    assert "lead_kitchen" in lead_circuits[0].entity_id.lower()

    mid_circuits = [
        a for a in consumption
        if a.parent_entity_id == mid_eid and "Sankey hierarchy" not in a.reason
    ]
    assert len(mid_circuits) == 1
    assert "mid_kitchen" in mid_circuits[0].entity_id.lower()

    tail_circuits = [a for a in consumption if a.parent_entity_id == tail_eid]
    assert len(tail_circuits) == 1
    assert "tail_office" in tail_circuits[0].entity_id.lower()
