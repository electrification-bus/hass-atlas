"""Topology-aware energy system analysis.

Discovers the physical electrical topology (SPAN panels, batteries, PV systems)
and cross-references with other HA integrations to build an Energy Dashboard
configuration that avoids double-counting and suppresses CT-noise false positives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hass_atlas.models import Gap, HADevice, HAEntity, SpanDeviceTree

if TYPE_CHECKING:
    from hass_atlas.span_panel import CircuitRoleOverride

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
    # Upstream lugs ``connection/fed-by-device-id`` and ``-type``, when published.
    fed_by_device_id: str | None = None
    fed_by_device_type: str | None = None
    # (display name, node id) of each remote CT on the panel.
    remote_cts: list[tuple[str, str]] = field(default_factory=list)
    # Breaker circuits' ``connection/feeds-role`` values (span-hass 0.6.0b1
    # Feeds Role), by node id.  Panels that publish no Feeds Role leave it empty.
    feeds_roles: dict[str, str] = field(default_factory=dict)
    # Feeds Role entity ids that are disabled in Home Assistant.
    feeds_role_disabled: list[str] = field(default_factory=list)
    # (node id, display name) of circuits whose Feeds Role is SOLAR, other than
    # a PV device's feed circuit.  Each is a solar source.
    solar_role_circuits: list[tuple[str, str]] = field(default_factory=list)
    # True when the battery feed circuit comes from Feeds Role STORAGE alone.
    battery_by_role: bool = False


@dataclass
class EnergyIntegration:
    """An HA integration that provides energy entities."""

    platform: str
    devices: list[HADevice]
    energy_entities: list[HAEntity]
    # Power (W) sensors on the same platform, used to source a positive
    # production-power sensor for a solar source's stat_rate (Now tab).
    power_entities: list[HAEntity] = field(default_factory=list)


@dataclass
class CircuitRole:
    """A circuit's role in the energy system."""

    circuit: HADevice
    role: str  # "load", "pv_feed", "bess_feed", "ev_feed", "shared_meter_member"
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
    # Grid import and export assignments with the same key form one grid
    # connection; None groups them by platform.
    grid_connection: str | None = None


@dataclass
class EnergyTopology:
    """Complete energy system topology."""

    panels: list[SpanTopology]
    integrations: list[EnergyIntegration]
    circuit_roles: list[CircuitRole]
    role_assignments: list[EnergyRole]
    warnings: list[str]
    gaps: list[Gap] = field(default_factory=list)


# ---------------------------------------------------------------------------
# discover_energy_integrations — scan all HA entities for energy producers
# ---------------------------------------------------------------------------


def discover_energy_integrations(
    devices: list[HADevice],
    entities: list[HAEntity],
    exclude_platform: str = "span_ebus",
) -> list[EnergyIntegration]:
    """Find all HA integrations that provide energy entities.

    Scans ALL entities for device_class=energy + state_class=total_increasing
    and groups them by platform.  ``exclude_platform`` is the SPAN integration
    whose entities the caller handles separately.
    """
    # Index devices by id
    device_by_id: dict[str, HADevice] = {d.id: d for d in devices}

    # Find energy entities (exclude the SPAN integration: handled separately)
    energy_entities: list[HAEntity] = [
        e
        for e in entities
        if e.platform != exclude_platform
        and e.device_class == "energy"
        and e.state_class == "total_increasing"
        and not e.disabled_by
    ]

    # Group by platform
    by_platform: dict[str, list[HAEntity]] = {}
    for entity in energy_entities:
        by_platform.setdefault(entity.platform, []).append(entity)

    # Index power (W) sensors by platform; these provide a positive
    # production-power sensor for a solar source's stat_rate (Now tab). Only
    # platforms that already expose energy entities become integrations.
    power_by_platform: dict[str, list[HAEntity]] = {}
    for entity in entities:
        if (
            entity.platform != exclude_platform
            and entity.device_class == "power"
            and entity.state_class == "measurement"
            and not entity.disabled_by
        ):
            power_by_platform.setdefault(entity.platform, []).append(entity)

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
                power_entities=power_by_platform.get(platform, []),
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


def _ha_device_homie_id(device: HADevice | None) -> str | None:
    """Extract the Homie device-id segment from an HA device identifier.

    span-hass 0.2.0 HA identifiers carry the Homie device-id after the
    panel-serial prefix, e.g.:
        ('span_ebus', 'nt-2026-c192x_nt-2026-c192x-tg121153003k7g')
        ('span_ebus', 'nt-2026-c192x_nt-2026-c192x-iq7plus-72-x-us')

    The Homie ``connection/feeds-device-id`` / ``connection/fed-by-device-id``
    properties carry the bare Homie device-id (no panel-serial prefix), so
    this strips the prefix so the two can be compared.
    """
    if device is None:
        return None
    for domain, ident in device.identifiers:
        if domain == "span_ebus" and "_" in ident:
            return ident.split("_", 1)[1]
    return None


FEEDS_ROLE_SUFFIX = "_connection_feeds-role"


def _subpanel_feeds(
    roles: dict[tuple[str, str], CircuitRoleOverride] | None,
) -> dict[str, tuple[str, str]]:
    """Map each panel fed by a ``subpanel`` ``--circuit-role`` to (serial, node id) of its feeder."""
    return {
        ov.subpanel_serial: key
        for key, ov in (roles or {}).items()
        if ov.role == "subpanel" and ov.subpanel_serial
    }


def extract_span_topology(
    trees: list[SpanDeviceTree],
    states: dict[str, dict[str, Any]],
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> list[SpanTopology]:
    """Extract topology properties for each SPAN panel from entity states.

    span-hass 0.2.0: BESS/PV ``relative-position`` and ``feed`` entities are
    retired.  Position is now derived from the ``connection`` capability:

    - **IN_PANEL** — some circuit's ``connection/feeds-device-id`` equals the
      DER's Homie device-id.  That circuit IS the feed circuit.
    - **UPSTREAM** — the panel's upstream-lug ``connection/fed-by-device-id``
      equals the DER's Homie device-id.
    - **DOWNSTREAM** — SPAN does not publish the downstream-lug connection
      capability; not detectable in v1.
    - **None** — DER exists in the tree but no connection signal yet
      (commissioning not done, or panel offline).

    span-hass 0.6.0b1 adds a circuit's Feeds Role (``connection/feeds-role``),
    which names what a circuit feeds when it is not an eBus device.  SOLAR
    makes the circuit a solar source (IN_PANEL) whether or not a PV device
    exists.  STORAGE makes it the battery feed circuit (IN_PANEL) when no
    circuit's ``feeds-device-id`` names the BESS.  A panel fed by a
    ``subpanel`` value in ``roles`` is not a lead panel.
    """
    topologies: list[SpanTopology] = []
    fed_by_role = _subpanel_feeds(roles)

    for tree in trees:
        serial = tree.serial
        if not serial:
            continue

        topo = SpanTopology(serial=serial)
        topo.is_lead_panel = tree.panel.via_device_id is None and serial not in fed_by_role

        # BESS identity (vendor/model/serial unique_id suffixes are stable in 0.2.0)
        bess = tree.battery
        topo.battery_vendor = _state_value(states, _find_sub_entity(bess, "_vendor-name"))
        topo.battery_model = _state_value(states, _find_sub_entity(bess, "_model"))
        topo.battery_serial = _state_value(states, _find_sub_entity(bess, "_serial-number"))

        # PV identity
        pv = tree.solar
        topo.solar_vendor = _state_value(states, _find_sub_entity(pv, "_vendor-name"))
        topo.solar_product = _state_value(states, _find_sub_entity(pv, "_product-name"))

        # Derive position from the connection graph.
        bess_did = _ha_device_homie_id(bess)
        pv_did = _ha_device_homie_id(pv)

        # Pass 1: each circuit's feeds-device-id may point at an in-panel DER.
        for circuit in tree.circuits:
            fed_to = _state_value(
                states, _find_sub_entity(circuit, "_connection_feeds-device-id")
            )
            if not fed_to:
                continue
            cid = _circuit_node_id(circuit)
            if bess_did and fed_to == bess_did:
                topo.battery_position = "IN_PANEL"
                topo.battery_feed_circuit_id = cid
                topo.battery_feed_circuit_name = circuit.display_name
            elif pv_did and fed_to == pv_did:
                topo.solar_position = "IN_PANEL"
                topo.solar_feed_circuit_id = cid
                topo.solar_feed_circuit_name = circuit.display_name

        # Feeds Role: what each circuit feeds when it is not an eBus device.
        for circuit in tree.circuits:
            cid = _circuit_node_id(circuit)
            entity = _find_sub_entity(circuit, FEEDS_ROLE_SUFFIX)
            if entity is None or cid is None:
                continue
            if entity.disabled_by:
                topo.feeds_role_disabled.append(entity.entity_id)
                continue
            role = _state_value(states, entity)
            if role:
                topo.feeds_roles[cid] = role
        for circuit in tree.circuits:
            cid = _circuit_node_id(circuit)
            role = topo.feeds_roles.get(cid or "")
            if role == "SOLAR" and cid != topo.solar_feed_circuit_id:
                topo.solar_role_circuits.append((cid or "", circuit.display_name))
            elif (
                role == "STORAGE" and topo.battery_feed_circuit_id is None
                and topo.battery_position is None
            ):
                topo.battery_position = "IN_PANEL"
                topo.battery_feed_circuit_id = cid
                topo.battery_feed_circuit_name = circuit.display_name
                topo.battery_by_role = True

        # Pass 2: upstream-lug's fed-by-device-id detects UPSTREAM DERs.  It
        # is empty when the panel does not say what feeds it.
        if tree.lugs_up:
            fed_by = _state_value(
                states, _find_sub_entity(tree.lugs_up, "_connection_fed-by-device-id")
            )
            topo.fed_by_device_id = fed_by
            topo.fed_by_device_type = _state_value(
                states, _find_sub_entity(tree.lugs_up, "_connection_fed-by-device-type")
            )
            if fed_by:
                if bess_did and fed_by == bess_did and not topo.battery_position:
                    topo.battery_position = "UPSTREAM"
                elif pv_did and fed_by == pv_did and not topo.solar_position:
                    topo.solar_position = "UPSTREAM"
        if topo.solar_role_circuits and not topo.solar_position:
            topo.solar_position = "IN_PANEL"

        topo.remote_cts = [
            (ct.display_name, _circuit_node_id(ct) or ct.id) for ct in tree.remote_cts
        ]
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


# ---------------------------------------------------------------------------
# Shared meters: several circuits reporting one meter's readings
# ---------------------------------------------------------------------------

SHARED_WITH_SUFFIX = "_meter_shared-with-device-ids"


@dataclass
class SharedMeterGroup:
    """Circuits on one panel that report the same meter."""

    serial: str
    members: list[HADevice]  # sorted by node id
    method: str  # "shared_with", "spaces", or "readings"
    detail: str | None = None  # the shared breaker space, for method "spaces"

    @property
    def counted(self) -> HADevice:
        """The member whose readings are counted: the lowest node id."""
        return self.members[0]


def _natural_key(text: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def _node_key(device: HADevice) -> list[Any]:
    return _natural_key(_circuit_node_id(device) or device.id)


def _float_state(states: dict[str, dict[str, Any]], entity: HAEntity | None) -> float | None:
    value = _state_value(states, entity)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _energy_readings(
    circuit: HADevice, states: dict[str, dict[str, Any]]
) -> tuple[float, float] | None:
    imported = _float_state(states, _find_circuit_entity(circuit, "imported-energy"))
    exported = _float_state(states, _find_circuit_entity(circuit, "exported-energy"))
    if imported is None or exported is None:
        return None
    return imported, exported


def _spaces_value(circuit: HADevice, states: dict[str, dict[str, Any]]) -> str | None:
    entity = _find_circuit_entity(circuit, "_info_spaces")
    value = _state_value(states, entity)
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return ",".join(sorted(parts, key=_natural_key))


def _shared_with_ids(value: str) -> set[str]:
    return {p.strip().strip("[]\"' ") for p in value.split(",")} - {""}


def find_shared_meter_groups(
    trees: list[SpanDeviceTree],
    states: dict[str, dict[str, Any]] | None,
) -> list[SharedMeterGroup]:
    """Find circuits on each panel that share one meter.

    span-hass publishes ``meter/shared-with-device-ids`` as a sensor that is
    disabled by default.  When any circuit on a panel has it enabled, its
    values define the groups.  Otherwise circuits in the same breaker space
    (``info/spaces``) with the same energy readings form a group, and when the
    panel publishes no spaces, circuits with identical, nonzero imported and
    exported energy readings do.
    """
    states = states or {}
    groups: list[SharedMeterGroup] = []
    for tree in trees:
        serial = tree.serial
        if not serial:
            continue
        by_node = {n: c for c in tree.circuits if (n := _circuit_node_id(c))}
        enabled = {
            n: e for n, c in by_node.items()
            if (e := _find_sub_entity(c, SHARED_WITH_SUFFIX)) and not e.disabled_by
        }
        keyed: dict[str, list[str]] = {}
        method = "shared_with"
        details: dict[str, str] = {}
        if enabled:
            parent: dict[str, str] = {n: n for n in by_node}

            def find(n: str) -> str:
                while parent[n] != n:
                    parent[n] = parent[parent[n]]
                    n = parent[n]
                return n

            for n, entity in enabled.items():
                for other in _shared_with_ids(_state_value(states, entity) or ""):
                    if other in by_node:
                        parent[find(other)] = find(n)
            for n in by_node:
                keyed.setdefault(find(n), []).append(n)
        else:
            spaces = {n: v for n, c in by_node.items() if (v := _spaces_value(c, states))}
            if spaces:
                method = "spaces"
                for n, value in spaces.items():
                    key = f"{value}|{_energy_readings(by_node[n], states)}"
                    keyed.setdefault(key, []).append(n)
                    details[key] = value
            else:
                method = "readings"
                for n, c in by_node.items():
                    readings = _energy_readings(c, states)
                    if readings is not None and readings != (0.0, 0.0):
                        keyed.setdefault(str(readings), []).append(n)
        for key, nodes in keyed.items():
            if len(nodes) < 2:
                continue
            members = sorted((by_node[n] for n in nodes), key=_node_key)
            groups.append(SharedMeterGroup(serial, members, method, details.get(key)))
    return groups


def _shared_meter_gap(group: SharedMeterGroup) -> Gap:
    counted = group.counted
    names = ", ".join(
        f"{m.display_name} ({_circuit_node_id(m)})" for m in group.members
    )
    how = {
        "shared_with": "per their Meter Shared With sensors",
        "spaces": f"same breaker space {group.detail} and same energy readings",
        "readings": "identical imported and exported energy readings",
    }[group.method]
    message = (
        f"Circuits {names} on {group.serial} share one meter ({how}), so their readings are "
        f"one counter; only {counted.display_name} ({_circuit_node_id(counted)}) is counted."
    )
    hint = None
    if group.method != "shared_with":
        disabled = [
            e.entity_id for m in group.members
            if (e := _find_sub_entity(m, SHARED_WITH_SUFFIX))
        ]
        if disabled:
            hint = (
                "Enable the Meter Shared With sensor in Home Assistant "
                f"({', '.join(disabled)}) so hass-atlas reads the group from the panel."
            )
    return Gap(
        kind="shared_meter",
        serial=group.serial,
        circuit_id=_circuit_node_id(counted),
        message=message,
        hint=hint,
    )


def classify_circuits(
    trees: list[SpanDeviceTree],
    topologies: list[SpanTopology],
    states: dict[str, dict[str, Any]] | None = None,
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> list[CircuitRole]:
    """Classify each circuit as load, pv_feed, bess_feed, subpanel_feed, or shared_meter_member.

    A shared-meter member other than the group's counted one is excluded from
    device consumption, since its readings repeat the counted member's.  A
    circuit whose Feeds Role is SOLAR is a solar source and not a consumer.  A
    circuit given ``subpanel:<serial>`` in ``roles`` feeds that panel.
    """
    roles = roles or {}
    # Build lookup: serial → topology
    topo_by_serial: dict[str, SpanTopology] = {t.serial: t for t in topologies}
    shared_with: dict[str, HADevice] = {}  # non-counted member id → counted member
    for group in find_shared_meter_groups(trees, states):
        for member in group.members[1:]:
            shared_with[member.id] = group.counted

    roles_out: list[CircuitRole] = []

    for tree in trees:
        serial = tree.serial
        if not serial:
            continue
        topo = topo_by_serial.get(serial)

        solar_role_ids = {cid for cid, _ in topo.solar_role_circuits} if topo else set()
        for circuit in tree.circuits:
            node_id = _circuit_node_id(circuit)
            override = roles.get((serial, node_id or ""))

            if override is not None and override.role == "subpanel":
                roles_out.append(CircuitRole(
                    circuit=circuit,
                    role="subpanel_feed",
                    skip_return_energy=True,
                    skip_consumption=False,
                    reason=(
                        f"Feeds panel {override.subpanel_serial} (--circuit-role): its "
                        "consumption is the parent of that panel's total"
                    ),
                ))
                continue

            if node_id in solar_role_ids:
                roles_out.append(CircuitRole(
                    circuit=circuit,
                    role="pv_feed",
                    skip_return_energy=False,  # return IS solar production
                    skip_consumption=True,
                    reason=(
                        "Feeds Role SOLAR (IN_PANEL): imported-energy (Energy Returned) = "
                        "solar production; not a consumer"
                    ),
                ))
                continue

            # Check if this circuit is a PV feed
            if (
                topo
                and topo.solar_feed_circuit_id
                and node_id
                and node_id == topo.solar_feed_circuit_id
            ):
                if topo.solar_position == "IN_PANEL":
                    roles_out.append(CircuitRole(
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
                    roles_out.append(CircuitRole(
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
                    roles_out.append(CircuitRole(
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
                    roles_out.append(CircuitRole(
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

            if circuit.id in shared_with:
                counted = shared_with[circuit.id]
                roles_out.append(CircuitRole(
                    circuit=circuit,
                    role="shared_meter_member",
                    skip_return_energy=True,
                    skip_consumption=True,
                    reason=(
                        f"Shares a meter with {counted.display_name}: counted once, "
                        "under that circuit"
                    ),
                ))
                continue

            # Pure load circuit
            feeds_role = topo.feeds_roles.get(node_id or "") if topo else None
            reason = "Pure load circuit: return energy suppressed (CT noise)"
            if feeds_role in ("SUBPANEL", "GENERATOR", "MIXED"):
                reason = (
                    f"Feeds Role {feeds_role}: counted as a load, return energy suppressed"
                )
            roles_out.append(CircuitRole(
                circuit=circuit,
                role="load",
                skip_return_energy=True,  # CT noise — suppress
                skip_consumption=False,
                reason=reason,
            ))

    return roles_out


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
    """Find an upstream-lug entity by property suffix.

    span-hass 0.2.0: upstream-lug entities live on their own HA device
    (``tree.lugs_up``) with unique_ids like ``..._meter_imported-energy``.
    Pass the bare property suffix (``imported-energy``, ``active-power``);
    ``_find_circuit_entity`` matches by ``endswith``.
    """
    if tree.lugs_up is None:
        return None
    return _find_circuit_entity(tree.lugs_up, suffix)


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


def _find_production_power(integration: EnergyIntegration) -> HAEntity | None:
    """Find a positive production-power sensor on a PV integration.

    The Energy Dashboard 'Now' card plots solar from its stat_rate power sensor
    and treats solar as positive=production. This selects a device_class=power
    sensor whose id indicates production (e.g. Enphase
    ``sensor.pv_current_power_production``), preferring a whole-system reading
    over per-phase ones (``..._l1``/``..._l2``).
    """
    candidates = [
        e
        for e in integration.power_entities
        if "production" in e.entity_id.lower() or "produced" in e.entity_id.lower()
    ]
    if not candidates:
        return None
    # Prefer non-per-phase (no _l1/_l2 suffix), then the shortest id (the
    # aggregate sensor is usually the most concise).
    candidates.sort(key=lambda e: (1 if _is_per_phase(e.entity_id) else 0, len(e.entity_id)))
    return candidates[0]


def _is_per_phase(entity_id: str) -> bool:
    """True if the entity id looks like a per-phase reading (``..._l1``/``_l2``)."""
    tail = entity_id.rsplit("_", 1)[-1]
    return len(tail) == 2 and tail[0] == "l" and tail[1].isdigit()


def _solar_rate_entity(
    device: HADevice | None,
    pv_integration: EnergyIntegration | None,
) -> tuple[str | None, str | None]:
    """Resolve a solar source's stat_rate power sensor.

    The Energy Dashboard 'Now' Power-sources card shows a source ONLY if it has
    a stat_rate power sensor, and treats solar as positive=production.

    Preference order:
      1. The SPAN PV-feed circuit's ``active-power``. span-hass >= 0.2.1 publishes
         this POSITIVE while generating (the pv_sign_aware fix suppresses the
         load-convention negation for PV-feed circuits), so it agrees in sign with
         the circuit's ``imported-energy`` and is measurement-consistent with the
         solar source's ``stat_energy_from``. Prefer it over a separate meter.
      2. A dedicated PV integration's positive production-power sensor, used when
         the SPAN power sensor is absent.
      3. None, with a warning. (Silently omitting would drop solar from the Now
         tab entirely, so the caller surfaces the warning instead.)

    Returns ``(rate_entity_id, warning)``.
    """
    span_power = _find_circuit_entity(device, "active-power") if device else None
    if span_power:
        return span_power.entity_id, None
    if pv_integration:
        prod = _find_production_power(pv_integration)
        if prod:
            return prod.entity_id, None
    return None, (
        "Solar source has no power sensor for stat_rate, so the Energy Dashboard "
        "'Now' tab will not show live solar power. Enable a dedicated PV "
        "integration with a production-power sensor, or ensure span-hash publishes "
        "the PV-feed circuit's active-power."
    )


def _find_circuit_by_node_id(
    trees: list[SpanDeviceTree],
    target_node_id: str,
    serial: str | None = None,
) -> HADevice | None:
    """Find a circuit device by its node_id, on panel ``serial`` when given.

    Node ids such as ``circuit-5`` repeat across panels of models other than
    MAIN 32, so a lookup for one panel's circuit passes its serial.
    """
    for tree in trees:
        if serial is not None and tree.serial != serial:
            continue
        for circuit in tree.circuits:
            if _circuit_node_id(circuit) == target_node_id:
                return circuit
    return None


def build_energy_topology(
    trees: list[SpanDeviceTree],
    topologies: list[SpanTopology],
    integrations: list[EnergyIntegration],
    circuit_roles: list[CircuitRole],
    states: dict[str, dict[str, Any]] | None = None,
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> EnergyTopology:
    """Build complete energy topology with role assignments and overlap detection.

    ``roles`` holds ``--circuit-role`` values, keyed by ``(serial, node id)``:
    any role but subpanel for a remote CT, and ``subpanel:<serial>`` for a
    breaker circuit.  A remote CT meters in the enclosure frame, like the
    upstream lugs: ``imported-energy`` is the flow toward the panel (grid
    import, solar production, battery discharge, or a load's consumption) and
    ``exported-energy`` the flow away from it (grid export, battery charge).
    A panel fed by a ``subpanel`` circuit is not a grid source: its upstream
    lugs are a consumer under that circuit.
    """
    states = states or {}
    roles = roles or {}
    fed_by_role = _subpanel_feeds(roles)
    assignments: list[EnergyRole] = []
    warnings: list[str] = []
    gaps: list[Gap] = []
    topo_by_serial = {t.serial: t for t in topologies}

    # Remote CTs with a stated role, by role.
    ct_roles: dict[str, list[tuple[SpanDeviceTree, HADevice]]] = {}
    for tree in trees:
        for ct in tree.remote_cts:
            override = roles.get((tree.serial or "", _circuit_node_id(ct) or ""))
            if override is not None:
                ct_roles.setdefault(override.role, []).append((tree, ct))

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
    if ct_roles.get("grid"):
        for tree, ct in ct_roles["grid"]:
            node = _circuit_node_id(ct)
            power = _find_circuit_entity(ct, "active-power")
            for role, suffix in (("grid_import", "imported-energy"), ("grid_export", "exported-energy")):
                if entity := _find_circuit_entity(ct, suffix):
                    assignments.append(EnergyRole(
                        role=role,
                        entity_id=entity.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason=f"Remote CT {node} given role grid: {suffix}",
                        rate_entity_id=power.entity_id if power else None,
                        grid_connection=f"{tree.serial}:{node}",
                    ))
        for tree in trees:
            for role, suffix in (("grid_import", "imported-energy"), ("grid_export", "exported-energy")):
                if entity := _find_upstream_energy(tree, suffix):
                    assignments.append(EnergyRole(
                        role=role,
                        entity_id=entity.entity_id,
                        platform="span_ebus",
                        preferred=False,
                        reason="A remote CT given role grid meters the grid",
                    ))
    elif all_bess_upstream and bess_integration:
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
            if tree.serial in fed_by_role:
                feeder_serial, feeder_cid = fed_by_role[tree.serial]
                for role, entity in (("grid_import", imported), ("grid_export", exported)):
                    if entity:
                        assignments.append(EnergyRole(
                            role=role,
                            entity_id=entity.entity_id,
                            platform="span_ebus",
                            preferred=False,
                            reason=(
                                f"Fed by circuit {feeder_cid} on {feeder_serial} "
                                "(--circuit-role): not a grid connection"
                            ),
                        ))
                continue
            tree_topo = topo_by_serial.get(tree.serial or "")
            if (
                tree_topo and tree_topo.battery_position == "UPSTREAM"
                and not bess_integration
            ):
                reason = (
                    "SPAN upstream metering: the battery is UPSTREAM and has no vendor "
                    "integration, so this includes battery flow"
                )
            else:
                reason = "SPAN upstream metering — no UPSTREAM BESS or no matching integration"
            if imported:
                assignments.append(EnergyRole(
                    role="grid_import",
                    entity_id=imported.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason=reason,
                    grid_connection=tree.serial,
                ))
            if exported:
                assignments.append(EnergyRole(
                    role="grid_export",
                    entity_id=exported.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason=reason,
                    grid_connection=tree.serial,
                ))

    # --- Remote CTs given role battery or solar ---
    for tree, ct in ct_roles.get("battery", []):
        node = _circuit_node_id(ct)
        power = _find_circuit_entity(ct, "active-power")
        for role, suffix in (
            ("battery_discharge", "imported-energy"), ("battery_charge", "exported-energy")
        ):
            if entity := _find_circuit_entity(ct, suffix):
                assignments.append(EnergyRole(
                    role=role,
                    entity_id=entity.entity_id,
                    platform="span_ebus",
                    preferred=True,
                    reason=f"Remote CT {node} given role battery: {suffix}",
                    rate_entity_id=power.entity_id if power else None,
                ))
    for tree, ct in ct_roles.get("solar", []):
        node = _circuit_node_id(ct)
        power = _find_circuit_entity(ct, "active-power")
        if entity := _find_circuit_entity(ct, "imported-energy"):
            assignments.append(EnergyRole(
                role="solar",
                entity_id=entity.entity_id,
                platform="span_ebus",
                preferred=True,
                reason=f"Remote CT {node} given role solar: imported-energy = solar production",
                rate_entity_id=power.entity_id if power else None,
            ))

    # --- Battery source ---
    serial_to_tree: dict[str, SpanDeviceTree] = {
        t.serial: t for t in trees if t.serial
    }
    for topo in topologies:
        if topo.battery_position == "IN_PANEL" and topo.battery_feed_circuit_id:
            # Use SPAN circuit for battery
            circuit = _find_circuit_by_node_id(trees, topo.battery_feed_circuit_id, topo.serial)
            if circuit:
                discharge = _find_circuit_entity(circuit, "imported-energy")
                charge = _find_circuit_entity(circuit, "exported-energy")
                # Prefer BESS sub-device's active-power for stat_rate (not the
                # feed circuit's, which is negated for consumption convention).
                batt_power = None
                bess_tree = serial_to_tree.get(topo.serial)
                if bess_tree and bess_tree.battery:
                    batt_power = _find_circuit_entity(bess_tree.battery, "active-power")
                if not batt_power and topo.battery_by_role:
                    # span-hass negates a STORAGE circuit's power into the
                    # consumption frame (positive while charging), the
                    # opposite of the battery stat_rate frame.
                    warnings.append(
                        f"Battery on {topo.serial} (Feeds Role STORAGE circuit "
                        f"{topo.battery_feed_circuit_id}) has no BESS power sensor for "
                        "stat_rate, so the Energy Dashboard 'Now' tab will not show live "
                        "battery power."
                    )
                elif not batt_power:
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
            circuit = _find_circuit_by_node_id(trees, topo.solar_feed_circuit_id, topo.serial)
            if circuit:
                solar_entity = _find_circuit_entity(circuit, "imported-energy")
                if solar_entity:
                    # stat_rate must be POSITIVE production power. Prefer the
                    # SPAN PV-feed circuit's active-power (span-hash >= 0.2.1
                    # publishes it positive and measurement-consistent with the
                    # circuit's imported-energy), else a dedicated PV
                    # integration's production-power sensor.
                    rate_eid, rate_warning = _solar_rate_entity(circuit, pv_integration)
                    if rate_warning:
                        warnings.append(rate_warning)
                    assignments.append(EnergyRole(
                        role="solar",
                        entity_id=solar_entity.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="PV IN_PANEL — SPAN circuit imported-energy = solar production",
                        rate_entity_id=rate_eid,
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
    role_solar_serials = {t.serial for t in topologies if t.solar_role_circuits}
    if not any(a.role == "solar" and a.preferred for a in assignments):
        for tree in trees:
            if tree.solar and tree.serial not in role_solar_serials:
                solar_entity = _find_circuit_entity(tree.solar, "imported-energy")
                if solar_entity:
                    # Positive production power only (see _solar_rate_entity):
                    # the SPAN solar device's active-power, else a PV
                    # integration's production-power sensor.
                    rate_eid, rate_warning = _solar_rate_entity(
                        tree.solar, pv_integration
                    )
                    if rate_warning:
                        warnings.append(rate_warning)
                    assignments.append(EnergyRole(
                        role="solar",
                        entity_id=solar_entity.entity_id,
                        platform="span_ebus",
                        preferred=True,
                        reason="SPAN solar device — no dedicated PV integration found",
                        rate_entity_id=rate_eid,
                    ))
                    break

    # --- Circuits whose Feeds Role is SOLAR ---
    # Each is a solar source on any panel.  span-hass publishes such a
    # circuit's active-power positive while generating (pv_sign_aware), and
    # its imported-energy ("Energy Returned") is the production.
    for topo in topologies:
        for cid, _name in topo.solar_role_circuits:
            circuit = _find_circuit_by_node_id(trees, cid, topo.serial)
            if circuit is None:
                continue
            solar_entity = _find_circuit_entity(circuit, "imported-energy")
            if solar_entity is None:
                continue
            rate_eid, rate_warning = _solar_rate_entity(circuit, None)
            if rate_warning:
                warnings.append(rate_warning)
            assignments.append(EnergyRole(
                role="solar",
                entity_id=solar_entity.entity_id,
                platform="span_ebus",
                preferred=True,
                reason=(
                    f"Feeds Role SOLAR on {topo.serial}:{cid}: circuit imported-energy = "
                    "solar production"
                ),
                rate_entity_id=rate_eid,
            ))

    # --- Device consumption (with Sankey hierarchy) ---
    # Sub-panel upstream energy entities are added as device_consumption so that
    # the Sankey diagram shows the hierarchy: Grid → Home → Panel → Circuits.
    # Trade-off: HA's "Individual devices total usage" bar chart renders ALL
    # device_consumption entries flat, so sub-panel aggregates double-count and
    # dominate the chart.  HA has no "hierarchy-only" display option.
    # Decision (2026-02-26): prioritize Sankey hierarchy.  See SHT-9fa.
    #
    # Identify which upstream entities are already preferred grid sources
    preferred_grid_eids = {
        a.entity_id for a in assignments if a.role == "grid_import" and a.preferred
    }

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
            if serial in fed_by_role:
                feeder_serial, feeder_cid = fed_by_role[serial]
                feeder = _find_circuit_by_node_id(trees, feeder_cid, feeder_serial)
                feeder_energy = (
                    _find_circuit_entity(feeder, "exported-energy") if feeder else None
                )
                parent_eid = feeder_energy.entity_id if feeder_energy else None
            elif tree.panel.via_device_id:
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
            if consumption and cr and cr.role == "pv_feed" and cr.skip_consumption:
                # Feeds Role SOLAR: skipped so energy --topology removes an
                # entry added earlier.
                assignments.append(EnergyRole(
                    role="device_consumption",
                    entity_id=consumption.entity_id,
                    platform="span_ebus",
                    preferred=False,
                    reason=cr.reason,
                ))
            elif consumption and cr and cr.role == "shared_meter_member":
                # Skipped so energy --topology removes an entry added earlier.
                assignments.append(EnergyRole(
                    role="device_consumption",
                    entity_id=consumption.entity_id,
                    platform="span_ebus",
                    preferred=False,
                    reason=cr.reason,
                ))
            elif consumption and (not cr or not cr.skip_consumption):
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
    for tree, ct in ct_roles.get("load", []):
        consumption = _find_circuit_entity(ct, "imported-energy")
        if consumption:
            power = _find_circuit_entity(ct, "active-power")
            assignments.append(EnergyRole(
                role="device_consumption",
                entity_id=consumption.entity_id,
                platform="span_ebus",
                preferred=True,
                reason=f"Remote CT {_circuit_node_id(ct)} given role load: imported-energy",
                parent_entity_id=panel_parent_eids.get(tree.serial) if tree.serial else None,
                rate_entity_id=power.entity_id if power else None,
            ))

    gaps += _ebus_gaps(
        trees, topologies, assignments, states, roles, bess_vendor, bess_integration
    )
    return EnergyTopology(
        panels=topologies,
        integrations=integrations,
        circuit_roles=circuit_roles,
        role_assignments=assignments,
        warnings=warnings,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Gaps: what the span_ebus path could not determine
# ---------------------------------------------------------------------------

EBUS_ROLE_SYNTAX = "grid|load|solar|battery|ignore"


def integration_hint(vendor: str | None, kind: str) -> str:
    """Name the vendor's Home Assistant integration for a gap hint."""
    if not vendor:
        return f"add the {kind} vendor's integration"
    platforms = sorted({
        platform
        for key, names in VENDOR_PLATFORM_MAP.items()
        if key in vendor.lower()
        for platform in names
    })
    if platforms:
        return f"add the {vendor} integration ({' or '.join(platforms)})"
    return f"add the {vendor} {kind} integration"


def _fmt_state(states: dict[str, dict[str, Any]], entity: HAEntity | None) -> str:
    value = _float_state(states, entity)
    if value is None or entity is None:
        return "unknown"
    unit = (states.get(entity.entity_id, {}).get("attributes") or {}).get(
        "unit_of_measurement", ""
    )
    return f"{value:g} {unit}".rstrip()


def _remote_ct_gap(
    tree: SpanDeviceTree, ct: HADevice, states: dict[str, dict[str, Any]]
) -> Gap:
    serial = tree.serial or ""
    node = _circuit_node_id(ct) or ct.id
    power = _find_circuit_entity(ct, "active-power")
    references = [
        ("panel Grid Power", _find_sub_entity(tree.panel, "_power-flows_grid")),
        ("upstream lugs Power", _find_upstream_energy(tree, "active-power")),
    ]
    readings = "; ".join(
        f"{label} {_fmt_state(states, ref)}" for label, ref in references if ref is not None
    )
    message = (
        f"{ct.display_name} ({node}) on {serial} is a meter outside the panel; hass-atlas "
        f"cannot tell what it measures, so it is not counted. Power {_fmt_state(states, power)}"
        + (f" ({readings})" if readings else "")
        + f"; imported {_fmt_state(states, _find_circuit_entity(ct, 'imported-energy'))}, "
        f"exported {_fmt_state(states, _find_circuit_entity(ct, 'exported-energy'))}."
    )
    suggestions: list[str] = []
    ct_power = _float_state(states, power)
    for label, ref in references:
        value = _float_state(states, ref)
        if ct_power is None or value is None:
            continue
        if ct_power != 0.0 and abs(ct_power) == abs(value):
            suggestions.append(
                f"Its power equals the {label} in magnitude; it is likely the service "
                f"(grid) CT: --circuit-role {serial}:{node}=grid"
            )
        elif abs(value) >= 50.0 and abs(abs(ct_power) - abs(value)) <= 0.05 * abs(value):
            suggestions.append(
                f"Its power is within 5% of the {label}; it may be the service (grid) CT: "
                f"--circuit-role {serial}:{node}=grid"
            )
        if suggestions:
            break
    return Gap(
        kind="unclassified_remote_ct",
        serial=serial,
        circuit_id=node,
        message=message,
        hint=f"--circuit-role {serial}:{node}={EBUS_ROLE_SYNTAX}",
        suggestions=suggestions,
    )


def _ebus_gaps(
    trees: list[SpanDeviceTree],
    topologies: list[SpanTopology],
    assignments: list[EnergyRole],
    states: dict[str, dict[str, Any]],
    roles: dict[tuple[str, str], CircuitRoleOverride],
    bess_vendor: str | None,
    bess_integration: EnergyIntegration | None,
) -> list[Gap]:
    gaps: list[Gap] = []
    topo_by_serial = {t.serial: t for t in topologies}
    grid_eids = {
        a.entity_id for a in assignments if a.role == "grid_import" and a.preferred
    }
    for tree in trees:
        serial = tree.serial
        if not serial:
            continue
        topo = topo_by_serial.get(serial)
        lugs_import = _find_upstream_energy(tree, "imported-energy")
        lugs_are_grid = lugs_import is not None and lugs_import.entity_id in grid_eids
        unclassified = [
            ct for ct in tree.remote_cts
            if (serial, _circuit_node_id(ct) or "") not in roles
        ]
        if lugs_are_grid and topo and not topo.fed_by_device_id:
            gaps.append(Gap(
                kind="service_entrance_unknown",
                serial=serial,
                circuit_id=None,
                message=(
                    f"hass-atlas does not know where the service entrance is for panel "
                    f"{serial}: the panel's upstream lugs do not say what feeds them. Grid "
                    "import and export come from these lugs, which is correct only if nothing "
                    "(a battery, another panel) sits between them and the utility. Please "
                    "confirm, or tell hass-atlas what feeds this panel."
                ),
                hint=(
                    f"If a remote CT measures the service: --circuit-role {serial}:<ct-id>=grid. "
                    "If a battery sits upstream, add its vendor integration."
                ),
            ))
        if (
            lugs_are_grid and topo and topo.battery_position == "UPSTREAM"
            and bess_integration is None
        ):
            vendor = topo.battery_vendor or bess_vendor
            gaps.append(Gap(
                kind="battery_upstream_no_integration",
                serial=serial,
                circuit_id=None,
                message=(
                    f"Panel {serial}'s upstream lugs are fed by its battery, and no vendor "
                    "battery integration was found. Grid import and export come from these "
                    "lugs, which meter the flow after the battery: battery discharge is counted "
                    "as grid import and charging as grid export, and no battery source can be "
                    "configured."
                ),
                hint=(
                    "To meter the grid ahead of the battery and the battery itself, "
                    f"{integration_hint(vendor, 'battery')}."
                ),
            ))
        if lugs_are_grid and unclassified:
            names = ", ".join(
                f"{ct.display_name} ({_circuit_node_id(ct)})" for ct in unclassified
            )
            gaps.append(Gap(
                kind="remote_ct_with_lugs_grid",
                serial=serial,
                circuit_id=None,
                message=(
                    f"Panel {serial} has a remote CT ({names}) and grid import and export come "
                    "from its upstream lugs. If the remote CT measures the service, the lugs "
                    "leave out what it sees beyond the panel."
                ),
                hint=f"If it measures the service: --circuit-role {serial}:<ct-id>=grid.",
            ))
        gaps += [_remote_ct_gap(tree, ct, states) for ct in unclassified]
        if topo:
            gaps += _feeds_role_gaps(tree, topo, trees, topo_by_serial, roles)
    gaps += [_shared_meter_gap(g) for g in find_shared_meter_groups(trees, states)]
    return gaps


def _unfed_panels(
    trees: list[SpanDeviceTree],
    topo_by_serial: dict[str, SpanTopology],
    roles: dict[tuple[str, str], CircuitRoleOverride],
) -> list[str]:
    """Serials of panels with no known feeder: no subpanel role, panel link, or lugs fed-by."""
    fed_by_role = _subpanel_feeds(roles)
    panel_ids = {t.panel.id for t in trees}
    result = []
    for tree in trees:
        serial = tree.serial
        topo = topo_by_serial.get(serial or "")
        if (
            serial and serial not in fed_by_role
            and tree.panel.via_device_id not in panel_ids
            and not (topo and topo.fed_by_device_id)
        ):
            result.append(serial)
    return result


def _subpanel_target(
    tree: SpanDeviceTree,
    trees: list[SpanDeviceTree],
    topo_by_serial: dict[str, SpanTopology],
    roles: dict[tuple[str, str], CircuitRoleOverride],
) -> tuple[list[str], str | None]:
    """Panels a SUBPANEL circuit on ``tree`` may feed, and the one to suggest.

    A candidate has no subpanel role naming it and is either unfed or linked
    under ``tree``. The one panel linked under ``tree`` is suggested, else the
    one candidate.
    """
    serial = tree.serial or ""
    fed_by_role = _subpanel_feeds(roles)
    upstream = _feeders_of(serial, trees, roles)
    linked = [
        t.serial for t in trees
        if t.serial and t.serial != serial and t.serial not in fed_by_role
        and t.serial not in upstream and t.panel.via_device_id == tree.panel.id
        and not (topo_by_serial.get(t.serial) and topo_by_serial[t.serial].fed_by_device_id)
    ]
    unfed = [
        s for s in _unfed_panels(trees, topo_by_serial, roles)
        if s != serial and s not in upstream and s not in linked
    ]
    candidates = linked + unfed
    if len(linked) == 1:
        return candidates, linked[0]
    if not linked and len(unfed) == 1:
        return candidates, unfed[0]
    return candidates, None


def _has_unlinked_subpanel(
    serial: str,
    topo_by_serial: dict[str, SpanTopology],
    roles: dict[tuple[str, str], CircuitRoleOverride],
) -> bool:
    topo = topo_by_serial.get(serial)
    return bool(topo) and any(
        role == "SUBPANEL" and (serial, cid) not in roles
        for cid, role in (topo.feeds_roles if topo else {}).items()
    )


def _feeders_of(
    serial: str,
    trees: list[SpanDeviceTree],
    roles: dict[tuple[str, str], CircuitRoleOverride],
) -> set[str]:
    """Serials of the panels upstream of ``serial``, by subpanel role or panel link."""
    fed_by_role = _subpanel_feeds(roles)
    serial_by_panel_id = {t.panel.id: t.serial for t in trees if t.serial}
    tree_by_serial = {t.serial: t for t in trees if t.serial}
    seen: set[str] = set()
    cur: str | None = serial
    while cur is not None and cur not in seen:
        seen.add(cur)
        if cur in fed_by_role:
            cur = fed_by_role[cur][0]
        else:
            tree = tree_by_serial.get(cur)
            cur = serial_by_panel_id.get(tree.panel.via_device_id or "") if tree else None
    return seen - {serial}


def _feeds_role_gaps(
    tree: SpanDeviceTree,
    topo: SpanTopology,
    trees: list[SpanDeviceTree],
    topo_by_serial: dict[str, SpanTopology],
    roles: dict[tuple[str, str], CircuitRoleOverride],
) -> list[Gap]:
    """Gaps from circuits' Feeds Role on one span_ebus panel."""
    serial = topo.serial
    gaps: list[Gap] = []
    names = {_circuit_node_id(c): c.display_name for c in tree.circuits}
    if topo.feeds_role_disabled:
        shown = ", ".join(topo.feeds_role_disabled[:3])
        more = len(topo.feeds_role_disabled) - 3
        if more > 0:
            shown += f" and {more} more"
        gaps.append(Gap(
            kind="feeds_role_disabled",
            serial=serial,
            circuit_id=None,
            message=(
                f"Panel {serial} publishes each circuit's Feeds Role, but "
                f"{len(topo.feeds_role_disabled)} Feeds Role sensor(s) are disabled in Home "
                "Assistant, so hass-atlas cannot tell which of those circuits feed solar, a "
                "battery, or another panel."
            ),
            hint=f"Enable the Feeds Role sensors in Home Assistant ({shown}).",
        ))
    for cid, role in topo.feeds_roles.items():
        name = names.get(cid, cid)
        if role == "SUBPANEL" and (serial, cid) not in roles:
            candidates, target = _subpanel_target(tree, trees, topo_by_serial, roles)
            if not candidates:
                continue
            if target and _has_unlinked_subpanel(target, topo_by_serial, roles):
                # Two panels each suggested as the other's feeder: at most one is.
                tree_by_serial = {t.serial: t for t in trees if t.serial}
                _, back = _subpanel_target(tree_by_serial[target], trees, topo_by_serial, roles)
                if back == serial:
                    target = None
            suggestions = [f"--circuit-role {serial}:{cid}=subpanel:{target}"] if target else []
            gaps.append(Gap(
                kind="subpanel_circuit_unlinked",
                serial=serial,
                circuit_id=cid,
                message=(
                    f"{name} ({cid}) on {serial} has Feeds Role SUBPANEL, but which panel it "
                    "feeds is unknown. It is counted as a consumer, and if the panel it feeds "
                    "is in Home Assistant, that panel's flow is counted again as grid."
                ),
                hint=f"--circuit-role {serial}:{cid}=subpanel:<serial>",
                suggestions=suggestions,
            ))
        elif role in ("GENERATOR", "MIXED"):
            what = "a generator" if role == "GENERATOR" else "a mix of loads and sources"
            gaps.append(Gap(
                kind="feeds_role_unhandled",
                serial=serial,
                circuit_id=cid,
                message=(
                    f"{name} ({cid}) on {serial} has Feeds Role {role} ({what}); hass-atlas "
                    "counts it as a load and leaves out its return energy."
                ),
            ))
        elif role == "STORAGE" and cid != topo.battery_feed_circuit_id:
            feed = topo.battery_feed_circuit_id
            gaps.append(Gap(
                kind="feeds_role_unhandled",
                serial=serial,
                circuit_id=cid,
                message=(
                    f"{name} ({cid}) on {serial} has Feeds Role STORAGE, but the battery "
                    f"feed circuit is {names.get(feed, feed)} ({feed}); hass-atlas counts it "
                    "as a load and leaves out its return energy."
                ),
            ))
    return gaps
