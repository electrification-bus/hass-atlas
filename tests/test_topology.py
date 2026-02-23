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
    PANEL_DEVICE_ID,
    PV_FEED_CIRCUIT_DEVICE_ID,
    PV_FEED_CIRCUIT_NODE_ID,
    PW_DEVICE_ID,
    SERIAL,
    make_platform_entity,
    make_topology_states,
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


def test_extract_span_topology_lead_panel(span_tree: SpanDeviceTree) -> None:
    """Lead panel: PV IN_PANEL, BESS UPSTREAM."""
    states = make_topology_states(
        battery=span_tree.battery,
        solar=span_tree.solar,
        bess_position="UPSTREAM",
        bess_vendor="Tesla",
        pv_position="IN_PANEL",
        pv_vendor="Enphase",
        pv_feed_name="Commissioned PV System",
        pv_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    topos = extract_span_topology([span_tree], states)

    assert len(topos) == 1
    t = topos[0]
    assert t.serial == SERIAL
    assert t.is_lead_panel is True
    assert t.battery_position == "UPSTREAM"
    assert t.battery_vendor == "Tesla"
    assert t.solar_position == "IN_PANEL"
    assert t.solar_vendor == "Enphase"
    assert t.solar_feed_circuit_name == "Commissioned PV System"
    assert t.solar_feed_circuit_id == PV_FEED_CIRCUIT_NODE_ID


def test_extract_span_topology_sub_panel() -> None:
    """Sub-panel: PV UPSTREAM, BESS UPSTREAM, no feed circuit."""
    sub_serial = "nt-0000-sub01"
    panel = HADevice(
        id="dev-sub-panel",
        name="Sub Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", sub_serial)],
        via_device_id="dev-main-panel",  # via another SPAN panel
    )
    batt = HADevice(
        id="dev-sub-batt",
        name="Battery Storage",
        model="Battery Storage",
        identifiers=[("span_ebus", f"{sub_serial}_battery")],
        via_device_id="dev-sub-panel",
        entities=[
            HAEntity(
                entity_id="sensor.sub_bess_relative_position",
                unique_id=f"{sub_serial}_bess_relative-position",
                platform="span_ebus", device_id="dev-sub-batt",
                entity_category="diagnostic",
            ),
        ],
    )
    sol = HADevice(
        id="dev-sub-solar",
        name="Solar PV",
        model="Solar PV",
        identifiers=[("span_ebus", f"{sub_serial}_solar")],
        via_device_id="dev-sub-panel",
        entities=[
            HAEntity(
                entity_id="sensor.sub_pv_relative_position",
                unique_id=f"{sub_serial}_pv_relative-position",
                platform="span_ebus", device_id="dev-sub-solar",
                entity_category="diagnostic",
            ),
            HAEntity(
                entity_id="sensor.sub_pv_feed",
                unique_id=f"{sub_serial}_pv_feed",
                platform="span_ebus", device_id="dev-sub-solar",
                entity_category="diagnostic",
            ),
        ],
    )
    tree = SpanDeviceTree(panel=panel, battery=batt, solar=sol)
    states = make_topology_states(
        battery=batt,
        solar=sol,
        bess_position="UPSTREAM",
        pv_position="UPSTREAM",
        pv_feed_name=None,
        pv_feed_circuit_id=None,
    )

    topos = extract_span_topology([tree], states)
    assert len(topos) == 1
    t = topos[0]
    assert t.is_lead_panel is False  # has via_device_id
    assert t.solar_position == "UPSTREAM"
    assert t.solar_feed_circuit_name is None
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
    """Pure load circuits: skip return energy, keep consumption."""
    topo = SpanTopology(serial=SERIAL)  # No PV/BESS feed info
    roles = classify_circuits([span_tree], [topo])

    assert len(roles) == 2
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
        site_metering=site_meter_device,
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
        site_metering=site_meter_device,
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
        site_metering=site_meter_device,
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
        site_metering=site_meter_device,
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
        serial=SERIAL,
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

    # SPAN grid should be non-preferred
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
        serial=SERIAL,
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
        site_metering=site_meter_device,
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
        site_metering=site_meter_device,
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
    """No other integrations, no topology — all SPAN preferred."""
    topo = SpanTopology(serial=SERIAL)
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], [], circuit_roles)

    preferred = [a for a in result.role_assignments if a.preferred]
    assert all(a.platform == "span_ebus" for a in preferred)

    # Grid comes from site metering
    grid = [a for a in preferred if a.role == "grid_import"]
    assert len(grid) == 1

    # Device consumption for both circuits
    consumption = [a for a in preferred if a.role == "device_consumption"]
    assert len(consumption) == 2


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
        site_metering=site_meter_device,
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
        site_metering=site_meter_device,
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
    pv_feed_circuit: HADevice,
) -> None:
    """Full topology-aware config: PW grid+battery, SPAN circuit solar, SPAN consumption."""
    # Add PV feed circuit to tree
    span_tree.circuits.append(pv_feed_circuit)

    topo = SpanTopology(
        serial=SERIAL,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
        solar_position="IN_PANEL",
        solar_vendor="Enphase",
        solar_feed_circuit_id=PV_FEED_CIRCUIT_NODE_ID,
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])
    energy_topo = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    config = build_topology_aware_config(energy_topo)

    # Grid from powerwall
    grid_sources = [s for s in config["energy_sources"] if s["type"] == "grid"]
    assert len(grid_sources) == 1
    grid = grid_sources[0]
    assert any("powerwall_site_import" in f["stat_energy_from"] for f in grid["flow_from"])

    # Solar from SPAN PV feed circuit return energy
    solar_sources = [s for s in config["energy_sources"] if s["type"] == "solar"]
    assert len(solar_sources) == 1
    assert "energy_returned" in solar_sources[0]["stat_energy_from"]

    # Battery from powerwall
    batt_sources = [s for s in config["energy_sources"] if s["type"] == "battery"]
    assert len(batt_sources) == 1
    assert "powerwall" in batt_sources[0].get("stat_energy_from", "")

    # Device consumption — Kitchen + Garage + PV circuit (parasitic)
    consumption_ids = {d["stat_consumption"] for d in config["device_consumption"]}
    assert "sensor.span_kitchen_energy" in consumption_ids
    assert "sensor.span_garage_energy" in consumption_ids
    assert "sensor.span_pv_system_energy" in consumption_ids


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_backward_compat_basic_energy_command(span_tree: SpanDeviceTree) -> None:
    """The original build_energy_config still works unchanged."""
    from hass_atlas.energy import build_energy_config

    config = build_energy_config([span_tree])
    source_types = {s["type"] for s in config["energy_sources"]}
    assert "grid" in source_types
    assert "solar" in source_types
    assert "battery" in source_types
    assert len(config["device_consumption"]) == 2


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


def test_build_energy_topology_circuit_rate_entity_id(
    span_tree: SpanDeviceTree,
) -> None:
    """Circuits with active-power entities get rate_entity_id set."""
    topo = SpanTopology(serial=SERIAL, is_lead_panel=True)
    circuit_roles = classify_circuits([span_tree], [topo])
    result = build_energy_topology([span_tree], [topo], [], circuit_roles)

    consumption = [a for a in result.role_assignments if a.role == "device_consumption" and a.preferred]
    # Kitchen circuit has active-power entity in conftest
    kitchen = next(a for a in consumption if "kitchen" in a.entity_id)
    assert kitchen.rate_entity_id == "sensor.span_kitchen_active_power"

    # Garage circuit does NOT have active-power entity in conftest
    garage = next(a for a in consumption if "garage" in a.entity_id)
    assert garage.rate_entity_id is None


def test_build_energy_topology_solar_rate_entity_id(
    panel_device: HADevice,
    site_meter_device: HADevice,
    pv_feed_circuit: HADevice,
    circuit_devices: list[HADevice],
) -> None:
    """Solar IN_PANEL: PV feed circuit gets rate_entity_id if active-power exists."""
    # Add an active-power entity to the PV feed circuit
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
        site_metering=site_meter_device,
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
    """BESS UPSTREAM: panel upstream → device_consumption parent, circuits get parent_entity_id."""
    topo = SpanTopology(
        serial=SERIAL,
        battery_position="UPSTREAM",
        battery_vendor="Tesla",
        is_lead_panel=True,
    )
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits([span_tree], [topo])

    result = build_energy_topology([span_tree], [topo], integrations, circuit_roles)

    # Panel upstream should be added as device_consumption (it's non-preferred for grid)
    consumption = [a for a in result.role_assignments if a.role == "device_consumption" and a.preferred]
    panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason]
    assert len(panel_entries) == 1
    panel_eid = panel_entries[0].entity_id
    assert panel_entries[0].parent_entity_id is None  # lead panel has no parent

    # Circuit entries should have parent_entity_id pointing to panel
    circuit_entries = [a for a in consumption if a.entity_id != panel_eid]
    assert len(circuit_entries) == 2  # kitchen + garage
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
    panel_device: HADevice,
    site_meter_device: HADevice,
    circuit_devices: list[HADevice],
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Multi-panel: lead → sub-panel → circuits, 3-level hierarchy."""
    lead_serial = SERIAL
    sub_serial = "nt-0000-sub01"

    # Lead panel tree
    lead_tree = SpanDeviceTree(
        panel=panel_device,
        circuits=circuit_devices,
        site_metering=site_meter_device,
    )

    # Sub-panel tree
    sub_panel = HADevice(
        id="dev-sub-panel",
        name="Sub Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", sub_serial)],
        via_device_id=PANEL_DEVICE_ID,
    )
    sub_site_meter = HADevice(
        id="dev-sub-site-meter",
        name="Sub Site Metering",
        model="Site Metering",
        identifiers=[("span_ebus", f"{sub_serial}_site-meter")],
        via_device_id="dev-sub-panel",
        entities=[
            HAEntity(
                entity_id="sensor.sub_site_imported_energy",
                unique_id=f"{sub_serial}_site-meter_imported-energy",
                platform="span_ebus", device_id="dev-sub-site-meter",
            ),
        ],
    )
    sub_circuit = HADevice(
        id="dev-sub-circuit-001",
        name="Sub Kitchen",
        model="Circuit",
        identifiers=[("span_ebus", f"{sub_serial}_sc1-node")],
        via_device_id="dev-sub-panel",
        entities=[
            HAEntity(
                entity_id="sensor.sub_kitchen_energy",
                unique_id=f"{sub_serial}_sc1-node_exported-energy",
                platform="span_ebus", device_id="dev-sub-circuit-001",
            ),
        ],
    )
    sub_tree = SpanDeviceTree(
        panel=sub_panel,
        circuits=[sub_circuit],
        site_metering=sub_site_meter,
    )

    lead_topo = SpanTopology(serial=lead_serial, battery_position="UPSTREAM",
                             battery_vendor="Tesla", is_lead_panel=True)
    sub_topo = SpanTopology(serial=sub_serial, battery_position="UPSTREAM",
                            battery_vendor="Tesla", is_lead_panel=False)

    trees = [lead_tree, sub_tree]
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits(trees, [lead_topo, sub_topo])

    result = build_energy_topology(trees, [lead_topo, sub_topo], integrations, circuit_roles)

    consumption = [a for a in result.role_assignments if a.role == "device_consumption" and a.preferred]

    # Lead panel entry — no parent
    lead_panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason
                         and a.parent_entity_id is None]
    assert len(lead_panel_entries) == 1
    lead_eid = lead_panel_entries[0].entity_id

    # Sub-panel entry — parent is lead panel
    sub_panel_entries = [a for a in consumption if "Sankey hierarchy" in a.reason
                        and a.parent_entity_id == lead_eid]
    assert len(sub_panel_entries) == 1
    sub_eid = sub_panel_entries[0].entity_id

    # Lead circuits → parent is lead panel
    lead_circuits = [a for a in consumption if a.parent_entity_id == lead_eid
                     and "Sankey hierarchy" not in a.reason]
    assert len(lead_circuits) == 2  # kitchen + garage

    # Sub circuits → parent is sub-panel
    sub_circuits = [a for a in consumption if a.parent_entity_id == sub_eid]
    assert len(sub_circuits) == 1
    assert sub_circuits[0].entity_id == "sensor.sub_kitchen_energy"


def test_build_energy_topology_daisy_chain_hierarchy(
    powerwall_device: HADevice,
    powerwall_entities: list[HAEntity],
) -> None:
    """Daisy-chain: lead → mid → tail, each panel points to its direct parent."""
    lead_serial = "nt-2143-c1akc"
    mid_serial = "nt-2204-c1c46"
    tail_serial = "nt-2026-c192x"

    # Lead panel (no via_device_id)
    lead_panel = HADevice(
        id="dev-lead",
        name="Lead Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", lead_serial)],
    )
    lead_site_meter = HADevice(
        id="dev-lead-sm",
        name="Lead Site Metering",
        model="Site Metering",
        identifiers=[("span_ebus", f"{lead_serial}_site-meter")],
        via_device_id="dev-lead",
        entities=[
            HAEntity(
                entity_id="sensor.lead_imported_energy",
                unique_id=f"{lead_serial}_site-meter_imported-energy",
                platform="span_ebus", device_id="dev-lead-sm",
            ),
        ],
    )
    lead_circuit = HADevice(
        id="dev-lead-c1",
        name="Lead Kitchen",
        model="Circuit",
        identifiers=[("span_ebus", f"{lead_serial}_lc1")],
        via_device_id="dev-lead",
        entities=[
            HAEntity(
                entity_id="sensor.lead_kitchen_energy",
                unique_id=f"{lead_serial}_lc1_exported-energy",
                platform="span_ebus", device_id="dev-lead-c1",
            ),
        ],
    )
    lead_tree = SpanDeviceTree(
        panel=lead_panel, circuits=[lead_circuit], site_metering=lead_site_meter,
    )

    # Mid panel (via_device_id → lead)
    mid_panel = HADevice(
        id="dev-mid",
        name="Mid Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", mid_serial)],
        via_device_id="dev-lead",
    )
    mid_site_meter = HADevice(
        id="dev-mid-sm",
        name="Mid Site Metering",
        model="Site Metering",
        identifiers=[("span_ebus", f"{mid_serial}_site-meter")],
        via_device_id="dev-mid",
        entities=[
            HAEntity(
                entity_id="sensor.mid_imported_energy",
                unique_id=f"{mid_serial}_site-meter_imported-energy",
                platform="span_ebus", device_id="dev-mid-sm",
            ),
        ],
    )
    mid_circuit = HADevice(
        id="dev-mid-c1",
        name="Mid Kitchen",
        model="Circuit",
        identifiers=[("span_ebus", f"{mid_serial}_mc1")],
        via_device_id="dev-mid",
        entities=[
            HAEntity(
                entity_id="sensor.mid_kitchen_energy",
                unique_id=f"{mid_serial}_mc1_exported-energy",
                platform="span_ebus", device_id="dev-mid-c1",
            ),
        ],
    )
    mid_tree = SpanDeviceTree(
        panel=mid_panel, circuits=[mid_circuit], site_metering=mid_site_meter,
    )

    # Tail panel (via_device_id → mid)
    tail_panel = HADevice(
        id="dev-tail",
        name="Tail Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", tail_serial)],
        via_device_id="dev-mid",
    )
    tail_site_meter = HADevice(
        id="dev-tail-sm",
        name="Tail Site Metering",
        model="Site Metering",
        identifiers=[("span_ebus", f"{tail_serial}_site-meter")],
        via_device_id="dev-tail",
        entities=[
            HAEntity(
                entity_id="sensor.tail_imported_energy",
                unique_id=f"{tail_serial}_site-meter_imported-energy",
                platform="span_ebus", device_id="dev-tail-sm",
            ),
        ],
    )
    tail_circuit = HADevice(
        id="dev-tail-c1",
        name="Tail Office",
        model="Circuit",
        identifiers=[("span_ebus", f"{tail_serial}_tc1")],
        via_device_id="dev-tail",
        entities=[
            HAEntity(
                entity_id="sensor.tail_office_energy",
                unique_id=f"{tail_serial}_tc1_exported-energy",
                platform="span_ebus", device_id="dev-tail-c1",
            ),
        ],
    )
    tail_tree = SpanDeviceTree(
        panel=tail_panel, circuits=[tail_circuit], site_metering=tail_site_meter,
    )

    # All panels must see BESS UPSTREAM for Powerwall to claim grid source,
    # freeing SPAN upstream entities for Sankey hierarchy
    lead_topo = SpanTopology(serial=lead_serial, battery_position="UPSTREAM",
                             battery_vendor="Tesla", is_lead_panel=True)
    mid_topo = SpanTopology(serial=mid_serial, battery_position="UPSTREAM",
                            battery_vendor="Tesla", is_lead_panel=False)
    tail_topo = SpanTopology(serial=tail_serial, battery_position="UPSTREAM",
                             battery_vendor="Tesla", is_lead_panel=False)

    # Pass trees in REVERSE order to also test topological sort
    trees = [tail_tree, mid_tree, lead_tree]
    topos = [lead_topo, mid_topo, tail_topo]
    integrations = [_make_pw_integration(powerwall_device, powerwall_entities)]
    circuit_roles = classify_circuits(trees, topos)

    result = build_energy_topology(trees, topos, integrations, circuit_roles)

    consumption = [a for a in result.role_assignments
                   if a.role == "device_consumption" and a.preferred]

    # Lead panel — no parent (it's the root)
    lead_entries = [a for a in consumption if "Sankey hierarchy" in a.reason
                    and a.parent_entity_id is None]
    assert len(lead_entries) == 1
    lead_eid = lead_entries[0].entity_id
    assert lead_eid == "sensor.lead_imported_energy"

    # Mid panel — parent is lead panel
    mid_entries = [a for a in consumption if "Sankey hierarchy" in a.reason
                   and a.parent_entity_id == lead_eid]
    assert len(mid_entries) == 1
    mid_eid = mid_entries[0].entity_id
    assert mid_eid == "sensor.mid_imported_energy"

    # Tail panel — parent is mid panel (NOT lead panel)
    tail_entries = [a for a in consumption if "Sankey hierarchy" in a.reason
                    and a.parent_entity_id == mid_eid]
    assert len(tail_entries) == 1
    tail_eid = tail_entries[0].entity_id
    assert tail_eid == "sensor.tail_imported_energy"

    # Lead circuits → parent is lead panel
    lead_circuits = [a for a in consumption if a.parent_entity_id == lead_eid
                     and "Sankey hierarchy" not in a.reason]
    assert len(lead_circuits) == 1
    assert lead_circuits[0].entity_id == "sensor.lead_kitchen_energy"

    # Mid circuits → parent is mid panel
    mid_circuits = [a for a in consumption if a.parent_entity_id == mid_eid
                    and "Sankey hierarchy" not in a.reason]
    assert len(mid_circuits) == 1
    assert mid_circuits[0].entity_id == "sensor.mid_kitchen_energy"

    # Tail circuits → parent is tail panel
    tail_circuits = [a for a in consumption if a.parent_entity_id == tail_eid]
    assert len(tail_circuits) == 1
    assert tail_circuits[0].entity_id == "sensor.tail_office_energy"
