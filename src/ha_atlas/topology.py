"""Topology-aware energy system analysis.

Discovers the physical electrical topology (SPAN panels, batteries, PV systems)
and cross-references with other HA integrations to build an Energy Dashboard
configuration that avoids double-counting and suppresses CT-noise false positives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ha_atlas.models import HADevice, HAEntity, SpanDeviceTree

# ---------------------------------------------------------------------------
# Vendor → HA integration platform mapping
# ---------------------------------------------------------------------------

VENDOR_PLATFORM_MAP: dict[str, set[str]] = {
    "tesla": {"powerwall", "tesla_fleet"},
    "enphase": {"enphase_envoy"},
    "solaredge": {"solaredge"},
    "generac": {"generac"},
    "sonnen": {"sonnen"},
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SpanTopology:
    """Topology properties for one SPAN panel."""

    serial: str
    battery_position: str | None = None  # UPSTREAM, IN_PANEL, DOWNSTREAM
    battery_vendor: str | None = None
    battery_model: str | None = None
    battery_serial: str | None = None
    battery_feed_circuit_name: str | None = None
    battery_feed_circuit_id: str | None = None
    solar_position: str | None = None
    solar_vendor: str | None = None
    solar_product: str | None = None
    solar_feed_circuit_name: str | None = None
    solar_feed_circuit_id: str | None = None
    is_lead_panel: bool = False


@dataclass
class EnergyIntegration:
    """An HA integration that provides energy entities."""

    platform: str
    devices: list[HADevice]
    energy_entities: list[HAEntity]


@dataclass
class CircuitRole:
    """A circuit's role in the energy system."""

    circuit: HADevice
    role: str  # "load", "pv_feed", "bess_feed", "ev_feed"
    skip_return_energy: bool  # True = exclude imported-energy from ED
    skip_consumption: bool  # True = exclude exported-energy from device_consumption
    reason: str


@dataclass
class EnergyRole:
    """An entity assigned to an energy dashboard role."""

    role: str  # grid_import, grid_export, solar, battery_charge, battery_discharge, device_consumption
    entity_id: str
    platform: str
    preferred: bool  # True = include in ED, False = skip (overlap)
    reason: str
    parent_entity_id: str | None = None  # For included_in_stat hierarchy
    rate_entity_id: str | None = None  # Power sensor for stat_rate (Now tab)


@dataclass
class EnergyTopology:
    """Complete energy system topology."""

    panels: list[SpanTopology]
    integrations: list[EnergyIntegration]
    circuit_roles: list[CircuitRole]
    role_assignments: list[EnergyRole]
    warnings: list[str]


# ---------------------------------------------------------------------------
# discover_energy_integrations — scan all HA entities for energy producers
# ---------------------------------------------------------------------------


def discover_energy_integrations(
    devices: list[HADevice],
    entities: list[HAEntity],
) -> list[EnergyIntegration]:
    """Find all HA integrations that provide energy entities.

    Scans ALL entities for device_class=energy + state_class=total_increasing
    and groups them by platform.
    """
    # Index devices by id
    device_by_id: dict[str, HADevice] = {d.id: d for d in devices}

    # Find energy entities (exclude span_ebus — we handle that separately)
    energy_entities: list[HAEntity] = [
        e
        for e in entities
        if e.platform != "span_ebus"
        and e.device_class == "energy"
        and e.state_class == "total_increasing"
        and not e.disabled_by
    ]

    # Group by platform
    by_platform: dict[str, list[HAEntity]] = {}
    for entity in energy_entities:
        by_platform.setdefault(entity.platform, []).append(entity)

    integrations: list[EnergyIntegration] = []
    for platform, plat_entities in sorted(by_platform.items()):
        # Collect unique devices for this platform
        seen_device_ids: set[str] = set()
        plat_devices: list[HADevice] = []
        for entity in plat_entities:
            if entity.device_id and entity.device_id not in seen_device_ids:
                if entity.device_id in device_by_id:
                    plat_devices.append(device_by_id[entity.device_id])
                    seen_device_ids.add(entity.device_id)
        integrations.append(
            EnergyIntegration(
                platform=platform,
                devices=plat_devices,
                energy_entities=plat_entities,
            )
        )

    return integrations


# ---------------------------------------------------------------------------
# extract_span_topology — read topology properties from entity states
# ---------------------------------------------------------------------------


def _find_sub_entity(device: HADevice | None, suffix: str) -> HAEntity | None:
    """Find an entity on a sub-device by unique_id suffix.

    SPAN sub-device entities have unique_ids like '{serial}_{node}_{property}'.
    We match by the trailing portion (e.g., '_bess_vendor-name') to avoid
    depending on how HA slugifies entity_ids.
    """
    if device is None:
        return None
    for entity in device.entities:
        if entity.unique_id.endswith(suffix):
            return entity
    return None


def _state_value(
    states: dict[str, dict[str, Any]],
    entity: HAEntity | None,
) -> str | None:
    """Get the state value for an entity from the states dict."""
    if entity is None:
        return None
    entry = states.get(entity.entity_id)
    if entry is None:
        return None
    val = entry.get("state")
    if val in (None, "", "unknown", "unavailable"):
        return None
    return str(val)


def _state_attr(
    states: dict[str, dict[str, Any]],
    entity: HAEntity | None,
    attr_name: str,
) -> str | None:
    """Get an attribute value for an entity from the states dict."""
    if entity is None:
        return None
    entry = states.get(entity.entity_id)
    if entry is None:
        return None
    val = entry.get("attributes", {}).get(attr_name)
    if val in (None, "", "unknown", "unavailable"):
        return None
    return str(val)


def extract_span_topology(
    trees: list[SpanDeviceTree],
    states: dict[str, dict[str, Any]],
) -> list[SpanTopology]:
    """Extract topology properties for each SPAN panel from entity states.

    Uses the entities already registered on the tree's sub-devices
    (battery, solar) to find entity_ids, then looks up their live states.
    This avoids constructing entity_ids from the serial, which doesn't
    match HA's has_entity_name slugification.
    """
    topologies: list[SpanTopology] = []

    for tree in trees:
        serial = tree.serial
        if not serial:
            continue

        topo = SpanTopology(serial=serial)

        # Determine if this is the lead panel (no via_device_id pointing to another SPAN panel)
        topo.is_lead_panel = tree.panel.via_device_id is None

        # Battery Storage topology — from tree.battery sub-device entities
        bess = tree.battery
        topo.battery_position = _state_value(
            states, _find_sub_entity(bess, "_relative-position")
        )
        topo.battery_vendor = _state_value(
            states, _find_sub_entity(bess, "_vendor-name")
        )
        topo.battery_model = _state_value(
            states, _find_sub_entity(bess, "_model")
        )
        topo.battery_serial = _state_value(
            states, _find_sub_entity(bess, "_serial-number")
        )
        bess_feed = _find_sub_entity(bess, "_feed")
        topo.battery_feed_circuit_name = _state_value(states, bess_feed)
        topo.battery_feed_circuit_id = _state_attr(states, bess_feed, "circuit_id")

        # Solar PV topology — from tree.solar sub-device entities
        pv = tree.solar
        topo.solar_position = _state_value(
            states, _find_sub_entity(pv, "_relative-position")
        )
        topo.solar_vendor = _state_value(
            states, _find_sub_entity(pv, "_vendor-name")
        )
        topo.solar_product = _state_value(
            states, _find_sub_entity(pv, "_product-name")
        )
        pv_feed = _find_sub_entity(pv, "_feed")
        topo.solar_feed_circuit_name = _state_value(states, pv_feed)
        topo.solar_feed_circuit_id = _state_attr(states, pv_feed, "circuit_id")

        topologies.append(topo)

    return topologies


# ---------------------------------------------------------------------------
# classify_circuits — assign roles to each circuit
# ---------------------------------------------------------------------------


def _circuit_node_id(circuit: HADevice) -> str | None:
    """Extract the node_id portion from a circuit device's identifier.

    Identifier format: (span_ebus, {serial}_{node_id})
    """
    for domain, ident in circuit.identifiers:
        if domain == "span_ebus" and "_" in ident:
            # node_id is everything after the serial prefix
            # serial format: nt-XXXX-XXXXX, so split on first underscore after serial
            parts = ident.split("_", 1)
            if len(parts) == 2:
                return parts[1]
    return None


def classify_circuits(
    trees: list[SpanDeviceTree],
    topologies: list[SpanTopology],
) -> list[CircuitRole]:
    """Classify each circuit as load, pv_feed, bess_feed, or ev_feed."""
    # Build lookup: serial → topology
    topo_by_serial: dict[str, SpanTopology] = {t.serial: t for t in topologies}

    roles: list[CircuitRole] = []

    for tree in trees:
        serial = tree.serial
        if not serial:
            continue
        topo = topo_by_serial.get(serial)

        for circuit in tree.circuits:
            node_id = _circuit_node_id(circuit)

            # Check if this circuit is a PV feed
            if (
                topo
                and topo.solar_feed_circuit_id
                and node_id
                and node_id == topo.solar_feed_circuit_id
            ):
                if topo.solar_position == "IN_PANEL":
                    roles.append(CircuitRole(
                        circuit=circuit,
                        role="pv_feed",
                        skip_return_energy=False,  # return IS solar production
                        skip_consumption=False,  # parasitic load is real
                        reason=(
                            "PV feed circuit (IN_PANEL): imported-energy = solar production, "
                            "exported-energy = parasitic load"
                        ),
                    ))
                else:
                    roles.append(CircuitRole(
                        circuit=circuit,
                        role="pv_feed",
                        skip_return_energy=True,  # dedicated integration handles PV
                        skip_consumption=False,
                        reason=(
                            f"PV feed circuit ({topo.solar_position}): "
                            f"solar metered by dedicated integration"
                        ),
                    ))
                continue

            # Check if this circuit is a BESS feed
            if (
                topo
                and topo.battery_feed_circuit_id
                and node_id
                and node_id == topo.battery_feed_circuit_id
            ):
                if topo.battery_position == "IN_PANEL":
                    roles.append(CircuitRole(
                        circuit=circuit,
                        role="bess_feed",
                        skip_return_energy=False,  # return IS battery discharge
                        skip_consumption=True,  # both directions are battery ops
                        reason=(
                            "BESS feed circuit (IN_PANEL): imported-energy = discharge, "
                            "exported-energy = charge — excluded from device_consumption"
                        ),
                    ))
                else:
                    roles.append(CircuitRole(
                        circuit=circuit,
                        role="bess_feed",
                        skip_return_energy=True,  # dedicated integration handles BESS
                        skip_consumption=False,
                        reason=(
                            f"BESS feed circuit ({topo.battery_position}): "
                            f"battery metered by dedicated integration"
                        ),
                    ))
                continue

            # Pure load circuit
            roles.append(CircuitRole(
                circuit=circuit,
                role="load",
                skip_return_energy=True,  # CT noise — suppress
                skip_consumption=False,
                reason="Pure load circuit: return energy suppressed (CT noise)",
            ))

    return roles


# ---------------------------------------------------------------------------
# detect_overlaps + build_energy_topology — the core decision engine
# ---------------------------------------------------------------------------


def _find_platform_for_vendor(
    vendor: str | None,
    integrations: list[EnergyIntegration],
) -> EnergyIntegration | None:
    """Find an active HA integration matching a vendor name.

    Uses substring matching: if a VENDOR_PLATFORM_MAP key is contained in the
    vendor name (case-insensitive), the corresponding platforms are checked.
    This handles variations like "Enphase Energy" matching the "enphase" key.
    """
    if not vendor:
        return None
    vendor_lower = vendor.lower()
    candidate_platforms: set[str] = set()
    for map_key, platforms in VENDOR_PLATFORM_MAP.items():
        if map_key in vendor_lower:
            candidate_platforms.update(platforms)
    for integration in integrations:
        if integration.platform in candidate_platforms:
            return integration
    return None


def _find_upstream_energy(
    tree: SpanDeviceTree,
    suffix: str,
) -> HAEntity | None:
    """Find an upstream lug energy entity on a SPAN panel.

    Looks for the entity on the panel device with unique_id ending in
    'lugs-upstream_{suffix}'. Falls back to site_metering or panel
    with just the suffix for compatibility with test fixtures.
    """
    # Primary: panel's upstream lugs (matches live HA)
    upstream_entity = _find_circuit_entity(tree.panel, f"lugs-upstream_{suffix}")
    if upstream_entity:
        return upstream_entity
    # Fallback: site_metering device (matches test fixtures)
    if tree.site_metering:
        fallback = _find_circuit_entity(tree.site_metering, suffix)
        if fallback:
            return fallback
    # Last resort: panel with generic suffix
    return _find_circuit_entity(tree.panel, suffix)


def _topo_sort_trees(
    trees: list[SpanDeviceTree],
    device_id_to_serial: dict[str, str],
) -> list[SpanDeviceTree]:
    """Sort trees so parents appear before children (for panel_parent_eids build).

    Trees with no via_device_id (or whose via_device_id points outside the
    SPAN panel set) come first, followed by their children, etc.
    """
    serial_to_tree: dict[str, SpanDeviceTree] = {}
    for tree in trees:
        if tree.serial:
            serial_to_tree[tree.serial] = tree

    # Build adjacency: serial → list of child serials
    children: dict[str, list[str]] = {s: [] for s in serial_to_tree}
    roots: list[str] = []
    for tree in trees:
        serial = tree.serial
        if not serial:
            continue
        parent_serial = device_id_to_serial.get(tree.panel.via_device_id or "")
        if parent_serial and parent_serial in serial_to_tree:
            children[parent_serial].append(serial)
        else:
            roots.append(serial)

    # BFS from roots
    result: list[SpanDeviceTree] = []
    queue = list(roots)
    while queue:
        s = queue.pop(0)
        result.append(serial_to_tree[s])
        queue.extend(children.get(s, []))

    # Append any trees not reached (shouldn't happen, but be safe)
    seen = {t.serial for t in result}
    for tree in trees:
        if tree.serial and tree.serial not in seen:
            result.append(tree)

    return result


def _find_entity_on_integration(
    integration: EnergyIntegration,
    keyword: str,
) -> HAEntity | None:
    """Find an energy entity whose entity_id contains a keyword."""
    for entity in integration.energy_entities:
        if keyword in entity.entity_id:
            return entity
    return None


def _find_circuit_entity(
    circuit: HADevice,
    property_suffix: str,
) -> HAEntity | None:
    """Find an entity on a device by unique_id suffix.

    Matches by unique_id suffix alone (e.g. 'imported-energy', 'exported-energy')
    without requiring device_class/state_class. The span_ebus integration does not
    set these in the entity registry, so filtering on them would find nothing.
    """
    for entity in circuit.entities:
        if not entity.disabled_by and entity.unique_id.endswith(property_suffix):
            return entity
    return None


def _find_circuit_by_node_id(
    trees: list[SpanDeviceTree],
    target_node_id: str,
) -> HADevice | None:
    """Find a circuit device by its node_id across all trees."""
    for tree in trees:
        for circuit in tree.circuits:
            if _circuit_node_id(circuit) == target_node_id:
                return circuit
    return None


def build_energy_topology(
    trees: list[SpanDeviceTree],
    topologies: list[SpanTopology],
    integrations: list[EnergyIntegration],
    circuit_roles: list[CircuitRole],
) -> EnergyTopology:
    """Build complete energy topology with role assignments and overlap detection."""
    assignments: list[EnergyRole] = []
    warnings: list[str] = []

    # Check if ALL panels see BESS UPSTREAM
    all_bess_upstream = (
        bool(topologies)
        and all(t.battery_position == "UPSTREAM" for t in topologies)
    )

    # Find matching integration for BESS vendor
    bess_vendor = next(
        (t.battery_vendor for t in topologies if t.battery_vendor), None
    )
    bess_integration = _find_platform_for_vendor(bess_vendor, integrations)

    # Find matching integration for PV vendor
    pv_vendor = next(
        (t.solar_vendor for t in topologies if t.solar_vendor), None
    )
    pv_integration = _find_platform_for_vendor(pv_vendor, integrations)

    # --- Grid source ---
    if all_bess_upstream and bess_integration:
        # SPAN upstream energy is post-battery, not true grid → use battery integration
        grid_import = _find_entity_on_integration(bess_integration, "import")
        grid_export = _find_entity_on_integration(bess_integration, "export")
        if grid_import:
            # Filter for site-level entities (not battery-level)
            site_import = next(
                (e for e in bess_integration.energy_entities if "site_import" in e.entity_id),
                grid_import,
            )
            assignments.append(EnergyRole(
                role="grid_import",
                entity_id=site_import.entity_id,
                platform=bess_integration.platform,
                preferred=True,
                reason=f"BESS UPSTREAM on all panels — {bess_integration.platform} meters true grid",
            ))
        if grid_export:
            site_export = next(
                (e for e in bess_integration.energy_entities if "site_export" in e.entity_id),
                grid_export,
            )
            assignments.append(EnergyRole(
                role="grid_export",
                entity_id=site_export.entity_id,
                platform=bess_integration.platform,
                preferred=True,
                reason=f"BESS UPSTREAM on all panels — {bess_integration.platform} meters true grid",
            ))
        # SPAN upstream is NOT true grid — mark as non-preferred
        for tree in trees:
            imported = _find_upstream_energy(tree, "imported-energy")
            exported = _find_upstream_energy(tree, "exported-energy")
            if imported:
                assignments.append(EnergyRole(
                    role="grid_import",
                    entity_id=imported.entity_id,
                    platform="span_ebus",
                    preferred=False,
                    reason="BESS UPSTREAM — SPAN upstream is post-battery, not true grid",
                ))
            if exported:
                assignments.append(EnergyRole(
                    role="grid_export",
                    entity_id=exported.entity_id,
                    platform="span_ebus",
                    preferred=False,
                    reason="BESS UPSTREAM — SPAN upstream is post-battery, not true grid",
                ))
        warnings.append(
            f"BESS is UPSTREAM of all panels (vendor={bess_vendor}) — "
            f"using {bess_integration.platform} for grid metering"
        )
    else:
        # Use SPAN upstream for grid
        for tree in trees:
            imported = _find_upstream_energy(tree, "imported-energy")
            exported = _find_upstream_energy(tree, "exported-energy")
            if imported:
                assignments.append(EnergyRole(
                    role="grid_import",
                    entity_id=imported.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason="SPAN upstream metering — no UPSTREAM BESS or no matching integration",
                ))
            if exported:
                assignments.append(EnergyRole(
                    role="grid_export",
                    entity_id=exported.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason="SPAN upstream metering — no UPSTREAM BESS or no matching integration",
                ))

    # --- Battery source ---
    for topo in topologies:
        if topo.battery_position == "IN_PANEL" and topo.battery_feed_circuit_id:
            # Use SPAN circuit for battery
            circuit = _find_circuit_by_node_id(trees, topo.battery_feed_circuit_id)
            if circuit:
                discharge = _find_circuit_entity(circuit, "imported-energy")
                charge = _find_circuit_entity(circuit, "exported-energy")
                batt_power = _find_circuit_entity(circuit, "active-power")
                batt_rate = batt_power.entity_id if batt_power else None
                if discharge:
                    assignments.append(EnergyRole(
                        role="battery_discharge",
                        entity_id=discharge.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="BESS IN_PANEL — SPAN circuit imported-energy = discharge",
                        rate_entity_id=batt_rate,
                    ))
                if charge:
                    assignments.append(EnergyRole(
                        role="battery_charge",
                        entity_id=charge.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="BESS IN_PANEL — SPAN circuit exported-energy = charge",
                        rate_entity_id=batt_rate,
                    ))
            # Dedicated integration is non-preferred for battery
            if bess_integration:
                for entity in bess_integration.energy_entities:
                    if "battery" in entity.entity_id:
                        role = "battery_discharge" if "export" in entity.entity_id else "battery_charge"
                        assignments.append(EnergyRole(
                            role=role,
                            entity_id=entity.entity_id,
                            platform=bess_integration.platform,
                            preferred=False,
                            reason="BESS IN_PANEL — SPAN circuit is preferred (measurement consistency)",
                        ))
        elif topo.battery_position == "UPSTREAM" and bess_integration:
            # Use dedicated integration for battery
            for entity in bess_integration.energy_entities:
                if "battery" in entity.entity_id:
                    if "export" in entity.entity_id:
                        assignments.append(EnergyRole(
                            role="battery_discharge",
                            entity_id=entity.entity_id,
                            platform=bess_integration.platform,
                            preferred=True,
                            reason=f"BESS UPSTREAM — {bess_integration.platform} meters battery",
                        ))
                    elif "import" in entity.entity_id:
                        assignments.append(EnergyRole(
                            role="battery_charge",
                            entity_id=entity.entity_id,
                            platform=bess_integration.platform,
                            preferred=True,
                            reason=f"BESS UPSTREAM — {bess_integration.platform} meters battery",
                        ))
            # Only emit once for battery (same serial across panels)
            break

    # --- Solar source ---
    for topo in topologies:
        if topo.solar_position == "IN_PANEL" and topo.solar_feed_circuit_id:
            # Use SPAN circuit for solar
            circuit = _find_circuit_by_node_id(trees, topo.solar_feed_circuit_id)
            if circuit:
                solar_entity = _find_circuit_entity(circuit, "imported-energy")
                if solar_entity:
                    solar_power = _find_circuit_entity(circuit, "active-power")
                    assignments.append(EnergyRole(
                        role="solar",
                        entity_id=solar_entity.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="PV IN_PANEL — SPAN circuit imported-energy = solar production",
                        rate_entity_id=solar_power.entity_id if solar_power else None,
                    ))
            # Dedicated PV integration is non-preferred
            if pv_integration:
                for entity in pv_integration.energy_entities:
                    assignments.append(EnergyRole(
                        role="solar",
                        entity_id=entity.entity_id,
                        platform=pv_integration.platform,
                        preferred=False,
                        reason="PV IN_PANEL — SPAN circuit is preferred (measurement consistency)",
                    ))
            break  # Only one solar source needed
        elif topo.solar_position == "UPSTREAM" and pv_integration:
            # Use dedicated integration for solar
            for entity in pv_integration.energy_entities:
                assignments.append(EnergyRole(
                    role="solar",
                    entity_id=entity.entity_id,
                    platform=pv_integration.platform,
                    preferred=True,
                    reason=f"PV UPSTREAM — {pv_integration.platform} meters solar",
                ))
            break

    # If no PV integration and PV is UPSTREAM, check SPAN solar device
    if not any(a.role == "solar" and a.preferred for a in assignments):
        for tree in trees:
            if tree.solar:
                solar_entity = _find_circuit_entity(tree.solar, "imported-energy")
                if solar_entity:
                    solar_power = _find_circuit_entity(tree.solar, "active-power")
                    assignments.append(EnergyRole(
                        role="solar",
                        entity_id=solar_entity.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="SPAN solar device — no dedicated PV integration found",
                        rate_entity_id=solar_power.entity_id if solar_power else None,
                    ))
                    break

    # --- Device consumption (with Sankey hierarchy) ---
    # Identify which upstream entities are already preferred grid sources
    preferred_grid_eids = {
        a.entity_id for a in assignments if a.role == "grid_import" and a.preferred
    }

    # Build serial → topology lookup
    topo_by_serial: dict[str, SpanTopology] = {t.serial: t for t in topologies}

    # Build device_id → serial mapping for following via_device_id chain
    device_id_to_serial: dict[str, str] = {}
    for tree in trees:
        if tree.serial:
            device_id_to_serial[tree.panel.id] = tree.serial

    # Topological sort: process parent panels before children so
    # panel_parent_eids has the parent's entry when the child is processed.
    sorted_trees = _topo_sort_trees(trees, device_id_to_serial)

    # Add panel-level consumption entries and build parent mapping
    panel_parent_eids: dict[str, str] = {}  # serial → parent entity_id for circuits
    for tree in sorted_trees:
        serial = tree.serial
        if not serial:
            continue
        upstream = _find_upstream_energy(tree, "imported-energy")
        if upstream and upstream.entity_id not in preferred_grid_eids:
            # Follow via_device_id to find direct parent panel's upstream entity
            parent_eid = None
            if tree.panel.via_device_id:
                parent_serial = device_id_to_serial.get(tree.panel.via_device_id)
                if parent_serial:
                    parent_eid = panel_parent_eids.get(parent_serial)
            # Find upstream active-power for stat_rate
            upstream_power = _find_upstream_energy(tree, "active-power")
            assignments.append(EnergyRole(
                role="device_consumption",
                entity_id=upstream.entity_id,
                platform="span_ebus",
                preferred=True,
                reason="Panel total energy — Sankey hierarchy parent",
                parent_entity_id=parent_eid,
                rate_entity_id=upstream_power.entity_id if upstream_power else None,
            ))
            panel_parent_eids[serial] = upstream.entity_id

    # Circuit consumption with parent linkage
    circuit_role_map: dict[str, CircuitRole] = {
        cr.circuit.id: cr for cr in circuit_roles
    }
    for tree in trees:
        serial = tree.serial
        parent_eid = panel_parent_eids.get(serial) if serial else None
        for circuit in tree.circuits:
            cr = circuit_role_map.get(circuit.id)
            consumption = _find_circuit_entity(circuit, "exported-energy")
            if consumption and (not cr or not cr.skip_consumption):
                power = _find_circuit_entity(circuit, "active-power")
                assignments.append(EnergyRole(
                    role="device_consumption",
                    entity_id=consumption.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason=cr.reason if cr else "Circuit consumption",
                    parent_entity_id=parent_eid,
                    rate_entity_id=power.entity_id if power else None,
                ))

    return EnergyTopology(
        panels=topologies,
        integrations=integrations,
        circuit_roles=circuit_roles,
        role_assignments=assignments,
        warnings=warnings,
    )
