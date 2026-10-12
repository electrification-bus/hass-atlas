"""Read-only support for the span_panel Home Assistant integration.

span_panel publishes each circuit as entities on the panel device rather than
as a device of its own, and answers the admin-only ``span_panel/panel_topology``
WebSocket command with the panel's circuits, their entity mappings, and its
sub-devices (BESS, MID, EVSE, PV).  This module turns that payload into the
``EnergyRole`` list the span_ebus path also produces, plus ``Gap`` records that
name what hass-atlas could not determine and how the user can state it.

Roles the payload does not carry (what a remote CT measures, which circuit
feeds the battery or another panel) come from ``--circuit-role`` overrides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import click

from hass_atlas.ha_client import HAClient, HAClientError
from hass_atlas.models import Gap, HADevice, HAEntity, SpanDeviceTree
from hass_atlas.topology import (
    EnergyIntegration,
    EnergyRole,
    EnergyTopology,
    _circuit_node_id,
    _find_platform_for_vendor,
    _find_production_power,
    integration_hint,
)

DOMAIN = "span_panel"
EBUS_DOMAIN = "span_ebus"

KNOWN_MODELS = ("MAIN_16", "MLO_24", "MAIN_32", "MAIN_40", "MLO_48")

# Payload keys the released 2.1.2 integration does not send.
CIRCUIT_KEYS_ABSENT_IN_2_1_2 = (
    "outside_panel",
    "is_generation",
    "shared_meter_group",
    "shared_relay_group",
)
PANEL_KEYS_ABSENT_IN_2_1_2 = ("first_position", "last_position")

ROLE_NAMES = ("grid", "load", "solar", "battery", "ignore")
ROLE_SYNTAX = "grid|load|solar|battery|subpanel:<serial>|ignore"
# Pseudo circuit id for a panel's feed-through lugs, valid only with a subpanel role.
FEEDTHROUGH_ID = "feedthrough"
CIRCUIT_ROLE_HELP = (
    "State a circuit's role, [SERIAL:]CIRCUIT-ID=ROLE with ROLE one of "
    "grid, load, solar, battery, subpanel:<serial>, ignore. "
    f"SERIAL:{FEEDTHROUGH_ID}=subpanel:<serial> states a panel fed from SERIAL's "
    "feed-through lugs. On span_ebus only a remote CT takes a role, and not subpanel. "
    "Repeatable."
)

_ROLE_SPEC_RE = re.compile(r"^(?:(?P<serial>[^:=]+):)?(?P<cid>[^:=]+)=(?P<role>.+)$")


class SpanPanelAdminRequired(HAClientError):
    """``span_panel/panel_topology`` refused a non-admin token."""


class CircuitRoleError(click.UsageError):
    """A ``--circuit-role`` value is malformed or names nothing known."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PanelCircuit:
    """One circuit record from ``span_panel/panel_topology``."""

    id: str
    name: str | None
    tabs: list[int]
    device_type: str  # "circuit" | "pv" | "evse"
    entities: dict[str, str]  # role -> entity_id (power, consumed_energy, ...)
    outside_panel: bool | None  # None: payload from 2.1.2
    is_generation: bool | None
    shared_meter_group: str | None
    shared_relay_group: str | None

    @property
    def generation(self) -> bool:
        """Generation, falling back to the device type when the payload lacks the flag."""
        if self.is_generation is not None:
            return self.is_generation
        return self.device_type == "pv"

    @property
    def remote_ct(self) -> bool:
        """A meter outside the panel.

        When the payload predates ``outside_panel``, a circuit with no breaker
        position is taken to be one, so it is never counted as a load by default.
        """
        if self.outside_panel is not None:
            return self.outside_panel
        return not self.tabs

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass
class PanelTopology:
    """One panel's ``span_panel/panel_topology`` response, parsed."""

    panel: HADevice
    serial: str
    model: str
    firmware: str
    panel_size: int
    panel_entities: dict[str, str]  # role -> entity_id
    circuits: list[PanelCircuit]
    sub_devices: list[dict[str, Any]]  # type, name, model, entities, optional solar block
    main_meter_consumed: HAEntity | None = None
    main_meter_produced: HAEntity | None = None
    missing_fields: list[str] = field(default_factory=list)

    @property
    def known_model(self) -> bool:
        return self.model in KNOWN_MODELS and self.panel_size > 0

    def sub_devices_of_type(self, kind: str) -> list[dict[str, Any]]:
        return [s for s in self.sub_devices if s.get("type") == kind]

    def counted_meter_members(self) -> set[str]:
        """Circuit ids counted for their shared meter group: the group-key member."""
        members: dict[str, list[str]] = {}
        for c in self.circuits:
            if c.shared_meter_group:
                members.setdefault(c.shared_meter_group, []).append(c.id)
        counted: set[str] = set()
        for key, ids in members.items():
            counted.add(key if key in ids else sorted(ids)[0])
        return counted

    def pv_feed_circuit_ids(self) -> set[str]:
        """Circuits a PV sub-device's ``solar`` block names as its feed."""
        return {
            str(fid)
            for s in self.sub_devices_of_type("pv")
            if (fid := (s.get("solar") or {}).get("feed_circuit_id"))
        }

    def is_generation_circuit(self, circuit: PanelCircuit) -> bool:
        return circuit.generation or circuit.id in self.pv_feed_circuit_ids()

    def non_key_meter_member(self, circuit: PanelCircuit) -> bool:
        return bool(circuit.shared_meter_group) and (
            circuit.id not in self.counted_meter_members()
        )


@dataclass
class CircuitRoleOverride:
    """A role the user stated for one circuit."""

    role: str  # grid | load | solar | battery | subpanel | ignore
    subpanel_serial: str | None = None

    def __str__(self) -> str:
        if self.role == "subpanel":
            return f"subpanel:{self.subpanel_serial}"
        return self.role


# ---------------------------------------------------------------------------
# Detection and fetch
# ---------------------------------------------------------------------------


def detect_integrations(devices: list[HADevice]) -> set[str]:
    """Return the SPAN integration domains present among device identifiers."""
    return {
        domain
        for d in devices
        for domain, _ in d.identifiers
        if domain in (DOMAIN, EBUS_DOMAIN)
    }


def span_panel_only(devices: list[HADevice]) -> bool:
    """True when span_panel is present and span_ebus is not."""
    found = detect_integrations(devices)
    return DOMAIN in found and EBUS_DOMAIN not in found


def exit_writer_unsupported(command: str) -> None:
    """Exit a writer command that has no span_panel support yet."""
    from hass_atlas.output import print_error

    print_error(
        f"span_panel support for '{command}' is not yet available. "
        "hass-atlas supports span_panel read-only: audit, energy-topology, "
        "and energy-audit (without --remap)."
    )
    raise SystemExit(1)


def find_panel_devices(devices: list[HADevice]) -> list[HADevice]:
    """Panel devices of the span_panel integration: its identifier and no parent device."""
    return [
        d
        for d in devices
        if d.via_device_id is None and any(dom == DOMAIN for dom, _ in d.identifiers)
    ]


def _panel_serial(device: HADevice) -> str:
    for domain, ident in device.identifiers:
        if domain == DOMAIN:
            return ident
    return device.id


def resolve_main_meter(
    serial: str,
    entities: list[HAEntity],
) -> tuple[HAEntity | None, HAEntity | None]:
    """Find the panel's main meter consumed and produced energy entities by unique_id."""
    prefix = f"span_{serial.lower()}_main_meter_"
    by_uid = {e.unique_id: e for e in entities if e.platform == DOMAIN}
    return (
        by_uid.get(f"{prefix}consumed_energy"),
        by_uid.get(f"{prefix}produced_energy"),
    )


def parse_panel_topology(
    payload: dict[str, Any],
    panel: HADevice,
    entities: list[HAEntity] | None = None,
) -> PanelTopology:
    """Parse one ``span_panel/panel_topology`` response."""
    serial = str(payload.get("serial") or _panel_serial(panel))
    missing: set[str] = {k for k in PANEL_KEYS_ABSENT_IN_2_1_2 if k not in payload}
    circuits: list[PanelCircuit] = []
    for cid, raw in (payload.get("circuits") or {}).items():
        missing.update(k for k in CIRCUIT_KEYS_ABSENT_IN_2_1_2 if k not in raw)
        circuits.append(
            PanelCircuit(
                id=cid,
                name=raw.get("name"),
                tabs=sorted(raw.get("tabs") or []),
                device_type=raw.get("device_type") or "circuit",
                entities=dict(raw.get("entities") or {}),
                outside_panel=raw.get("outside_panel"),
                is_generation=raw.get("is_generation"),
                shared_meter_group=raw.get("shared_meter_group"),
                shared_relay_group=raw.get("shared_relay_group"),
            )
        )
    circuits.sort(key=lambda c: (min(c.tabs) if c.tabs else 10_000, c.id))
    sub_devices = [
        {"device_id": did, **rec} for did, rec in (payload.get("sub_devices") or {}).items()
    ]
    consumed, produced = resolve_main_meter(serial, entities or [])
    return PanelTopology(
        panel=panel,
        serial=serial,
        model=panel.model or "",
        firmware=str(payload.get("firmware") or ""),
        panel_size=int(payload.get("panel_size") or 0),
        panel_entities=dict(payload.get("panel_entities") or {}),
        circuits=circuits,
        sub_devices=sub_devices,
        main_meter_consumed=consumed,
        main_meter_produced=produced,
        missing_fields=sorted(missing),
    )


async def fetch_panel_topologies(
    client: HAClient,
    devices: list[HADevice],
    entities: list[HAEntity] | None = None,
) -> list[PanelTopology]:
    """Call ``span_panel/panel_topology`` once per span_panel panel device."""
    panels: list[PanelTopology] = []
    for device in find_panel_devices(devices):
        try:
            payload = await client.send_command(
                "span_panel/panel_topology", device_id=device.id
            )
        except HAClientError as exc:
            if exc.code == "unauthorized" or "unauthorized" in str(exc).lower():
                raise SpanPanelAdminRequired(
                    "span_panel/panel_topology requires an administrator token. "
                    "Create a long-lived access token while logged in as a Home "
                    "Assistant admin user and pass it with --token (or HASS_API_TOKEN).",
                    code="unauthorized",
                ) from None
            raise
        panels.append(parse_panel_topology(payload or {}, device, entities))
    return panels


# ---------------------------------------------------------------------------
# --circuit-role
# ---------------------------------------------------------------------------


def parse_role(text: str) -> CircuitRoleOverride:
    """Parse the role half of a ``--circuit-role`` value."""
    if text.startswith("subpanel:"):
        serial = text.split(":", 1)[1]
        if not serial:
            raise CircuitRoleError(f"Role 'subpanel:' needs a panel serial ({ROLE_SYNTAX})")
        return CircuitRoleOverride(role="subpanel", subpanel_serial=serial)
    if text not in ROLE_NAMES:
        raise CircuitRoleError(f"Unknown circuit role '{text}' (expected {ROLE_SYNTAX})")
    return CircuitRoleOverride(role=text)


def resolve_circuit_roles(
    specs: tuple[str, ...] | list[str],
    panels: list[PanelTopology],
) -> dict[tuple[str, str], CircuitRoleOverride]:
    """Validate ``[<serial>:]<circuit-id>=<role>`` values against the panels.

    Returns ``{(serial, circuit_id): override}``.  Raises ``CircuitRoleError``
    for malformed values, unknown serials or circuit ids, ids that repeat
    across panels without a serial, conflicting values, and feed cycles.
    """
    by_serial = {p.serial: p for p in panels}
    lower_serial = {s.lower(): s for s in by_serial}
    result: dict[tuple[str, str], CircuitRoleOverride] = {}

    for spec in specs:
        m = _ROLE_SPEC_RE.match(spec.strip())
        if not m:
            raise CircuitRoleError(
                f"Invalid --circuit-role '{spec}' (expected [<serial>:]<circuit-id>={ROLE_SYNTAX})"
            )
        override = parse_role(m.group("role"))
        cid = m.group("cid")
        serial = m.group("serial")
        if cid == FEEDTHROUGH_ID:
            if override.role != "subpanel":
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': {FEEDTHROUGH_ID} takes only subpanel:<serial>"
                )
            if serial is None:
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': {FEEDTHROUGH_ID} needs the feeding panel's "
                    f"serial, e.g. <serial>:{FEEDTHROUGH_ID}={override}"
                )
        if serial is not None:
            resolved = lower_serial.get(serial.lower())
            if resolved is None:
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': no span_panel panel with serial '{serial}' "
                    f"(panels: {', '.join(sorted(by_serial)) or 'none'})"
                )
            if cid != FEEDTHROUGH_ID and not any(
                c.id == cid for c in by_serial[resolved].circuits
            ):
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': panel {resolved} has no circuit '{cid}'"
                )
            serial = resolved
        else:
            owners = [p.serial for p in panels if any(c.id == cid for c in p.circuits)]
            if not owners:
                raise CircuitRoleError(f"--circuit-role '{spec}': no panel has circuit '{cid}'")
            if len(owners) > 1:
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': circuit '{cid}' exists on panels "
                    f"{', '.join(sorted(owners))}; prefix it with the serial, e.g. "
                    f"{sorted(owners)[0]}:{cid}={override}"
                )
            serial = owners[0]
        if override.role == "subpanel":
            sub = lower_serial.get((override.subpanel_serial or "").lower())
            if sub is None:
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': no span_panel panel with serial "
                    f"'{override.subpanel_serial}'"
                )
            if sub == serial:
                raise CircuitRoleError(f"--circuit-role '{spec}': a panel cannot feed itself")
            override.subpanel_serial = sub
        key = (serial, cid)
        if key in result and str(result[key]) != str(override):
            raise CircuitRoleError(
                f"--circuit-role gives {serial}:{cid} two roles: {result[key]} and {override}"
            )
        result[key] = override

    _check_feeds(result)
    return result


def resolve_ebus_circuit_roles(
    specs: tuple[str, ...] | list[str],
    trees: list[SpanDeviceTree],
) -> dict[tuple[str, str], CircuitRoleOverride]:
    """Validate ``[<serial>:]<node-id>=<role>`` values for span_ebus remote CTs.

    Returns ``{(serial, node_id): override}``.  On span_ebus only a remote CT
    takes a role (grid, load, solar, battery, or ignore): panel feeds come
    from the published connection data and ``link-panels``.
    """
    remote: dict[str, set[str]] = {
        t.serial: {n for ct in t.remote_cts if (n := _circuit_node_id(ct))}
        for t in trees if t.serial
    }
    branch: dict[str, set[str]] = {
        t.serial: {n for c in t.circuits if (n := _circuit_node_id(c))}
        for t in trees if t.serial
    }
    lower_serial = {s.lower(): s for s in remote}
    result: dict[tuple[str, str], CircuitRoleOverride] = {}
    for spec in specs:
        m = _ROLE_SPEC_RE.match(spec.strip())
        if not m:
            raise CircuitRoleError(
                f"Invalid --circuit-role '{spec}' (expected [<serial>:]<ct-id>="
                "grid|load|solar|battery|ignore)"
            )
        override = parse_role(m.group("role"))
        cid = m.group("cid")
        if override.role == "subpanel" or cid == FEEDTHROUGH_ID:
            raise CircuitRoleError(
                f"--circuit-role '{spec}': span_ebus takes no subpanel role; use "
                "link-panels to state which panel feeds another"
            )
        if m.group("serial") is not None:
            serial = lower_serial.get(m.group("serial").lower())
            if serial is None:
                raise CircuitRoleError(
                    f"--circuit-role '{spec}': no span_ebus panel with serial "
                    f"'{m.group('serial')}' (panels: {', '.join(sorted(remote)) or 'none'})"
                )
            owners = [serial] if cid in remote[serial] | branch[serial] else []
        else:
            owners = sorted(s for s in remote if cid in remote[s] | branch[s])
        if not owners:
            raise CircuitRoleError(f"--circuit-role '{spec}': no panel has circuit '{cid}'")
        if len(owners) > 1:
            raise CircuitRoleError(
                f"--circuit-role '{spec}': circuit '{cid}' exists on panels "
                f"{', '.join(owners)}; prefix it with the serial, e.g. {owners[0]}:{cid}={override}"
            )
        serial = owners[0]
        if cid not in remote[serial]:
            raise CircuitRoleError(
                f"--circuit-role '{spec}': {serial}:{cid} is a breaker circuit; on span_ebus "
                "only a remote CT takes a role"
            )
        key = (serial, cid)
        if key in result and str(result[key]) != str(override):
            raise CircuitRoleError(
                f"--circuit-role gives {serial}:{cid} two roles: {result[key]} and {override}"
            )
        result[key] = override
    return result


def _check_feeds(roles: dict[tuple[str, str], CircuitRoleOverride]) -> None:
    """Reject a panel fed twice, or a cycle of panels feeding each other."""
    fed_by: dict[str, str] = {}
    for (serial, cid), ov in roles.items():
        if ov.role != "subpanel" or ov.subpanel_serial is None:
            continue
        if ov.subpanel_serial in fed_by and fed_by[ov.subpanel_serial] != serial:
            raise CircuitRoleError(
                f"--circuit-role names two panels feeding {ov.subpanel_serial}"
            )
        fed_by[ov.subpanel_serial] = serial
    for start in fed_by:
        seen = {start}
        cur = fed_by.get(start)
        while cur is not None:
            if cur in seen:
                raise CircuitRoleError(f"--circuit-role subpanel feeds form a cycle at {cur}")
            seen.add(cur)
            cur = fed_by.get(cur)


# ---------------------------------------------------------------------------
# State readings
# ---------------------------------------------------------------------------


def _reading(states: dict[str, dict[str, Any]], entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    entry = states.get(entity_id)
    if not entry:
        return None
    try:
        return float(entry.get("state"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt(states: dict[str, dict[str, Any]], entity_id: str | None) -> str:
    value = _reading(states, entity_id)
    if value is None:
        return "unknown"
    unit = (states.get(entity_id or "", {}).get("attributes") or {}).get(
        "unit_of_measurement", ""
    )
    return f"{value:g} {unit}".rstrip()


def at_service_entrance(
    panel: PanelTopology,
    states: dict[str, dict[str, Any]],
) -> bool | None:
    """The ``at_service_entrance`` attribute of the panel's current_power sensor, if published."""
    eid = panel.panel_entities.get("current_power")
    attrs = (states.get(eid or "", {}) or {}).get("attributes") or {}
    value = attrs.get("at_service_entrance")
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Energy roles + gaps
# ---------------------------------------------------------------------------


def _sort_parents_first(
    panels: list[PanelTopology],
    fed_by: dict[str, tuple[str, str]],
) -> list[PanelTopology]:
    by_serial = {p.serial: p for p in panels}
    result: list[PanelTopology] = []
    queue = [p.serial for p in panels if p.serial not in fed_by]
    while queue:
        s = queue.pop(0)
        result.append(by_serial[s])
        queue.extend(sub for sub, (parent, _) in fed_by.items() if parent == s)
    result.extend(p for p in panels if p not in result)
    return result


def _vendor_of(subs: list[dict[str, Any]]) -> str | None:
    for s in subs:
        solar = s.get("solar") or {}
        vendor = solar.get("vendor") or s.get("manufacturer")
        if vendor:
            return str(vendor)
    return None


def build_energy_topology_span_panel(
    panels: list[PanelTopology],
    states: dict[str, dict[str, Any]],
    integrations: list[EnergyIntegration],
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> tuple[EnergyTopology, list[Gap]]:
    """Build Energy Dashboard role assignments and gaps for span_panel panels."""
    roles = roles or {}
    assignments: list[EnergyRole] = []
    warnings: list[str] = []
    gaps: list[Gap] = []

    def role_of(p: PanelTopology, c: PanelCircuit) -> CircuitRoleOverride | None:
        return roles.get((p.serial, c.id))

    fed_by: dict[str, tuple[str, str]] = {
        ov.subpanel_serial: (serial, cid)
        for (serial, cid), ov in roles.items()
        if ov.role == "subpanel" and ov.subpanel_serial
    }
    entrance = {p.serial: at_service_entrance(p, states) for p in panels}
    lead = [p for p in panels if p.serial not in fed_by and entrance[p.serial] is not False]
    if not lead:
        lead = [p for p in panels if p.serial not in fed_by]
    lead_serials = {p.serial for p in lead}
    # Lead panels whose lugs are not the utility connection: the main meter is
    # the panel's own feed, downstream of an upstream battery or another panel.
    off_entrance = [p for p in lead if entrance[p.serial] is False]
    off_entrance_serials = {p.serial for p in off_entrance}
    unknown_entrance = [p for p in lead if entrance[p.serial] is None]

    grid_cts = [
        (p, c)
        for p in panels
        for c in p.circuits
        if (ov := role_of(p, c)) is not None and ov.role == "grid"
    ]

    bess_subs = [s for p in panels for s in p.sub_devices_of_type("bess")]
    bess_vendor = _vendor_of(bess_subs)
    bess_integration = _find_platform_for_vendor(bess_vendor, integrations)

    # --- Panel payload / model checks ---
    for p in panels:
        if p.missing_fields:
            message = (
                f"The span_panel integration on {p.serial} does not send "
                f"{', '.join(p.missing_fields)}."
            )
            if set(p.missing_fields) & set(CIRCUIT_KEYS_ABSENT_IN_2_1_2):
                message += (
                    " Remote CT and shared-meter checks were unavailable: circuits with no "
                    "breaker position are treated as remote CTs, and generation is taken "
                    "from device type 'pv'."
                )
            gaps.append(Gap(
                kind="payload_fields_missing",
                serial=p.serial,
                circuit_id=None,
                message=message,
                hint="Update the span_panel integration to 2.2 or later.",
            ))
        if not p.known_model:
            gaps.append(Gap(
                kind="unknown_panel_model",
                serial=p.serial,
                circuit_id=None,
                message=(
                    f"Panel {p.serial} reports model '{p.model or 'unknown'}' and size "
                    f"{p.panel_size}; hass-atlas cannot check its breaker layout."
                ),
                hint="Circuits are used as published; no action needed unless tabs look wrong.",
            ))
        if p.main_meter_consumed is None or p.main_meter_consumed.disabled_by:
            gaps.append(Gap(
                kind="main_meter_missing",
                serial=p.serial,
                circuit_id=None,
                message=(
                    f"No enabled main meter consumed-energy sensor for {p.serial} "
                    f"(unique_id span_{p.serial.lower()}_main_meter_consumed_energy)."
                ),
                hint="Enable the panel's Main Meter Consumed Energy sensor in Home Assistant.",
            ))
        if p.serial in lead_serials and not grid_cts and (
            p.main_meter_produced is None or p.main_meter_produced.disabled_by
        ):
            gaps.append(Gap(
                kind="main_meter_produced_missing",
                serial=p.serial,
                circuit_id=None,
                message=(
                    f"No enabled main meter produced-energy sensor for {p.serial} "
                    f"(unique_id span_{p.serial.lower()}_main_meter_produced_energy), so "
                    "return to grid is left out of the dashboard."
                ),
                hint="Enable the panel's Main Meter Produced Energy sensor in Home Assistant.",
            ))

    # --- Grid from the battery vendor's site meter (lead lugs not at the entrance) ---
    site_grid: dict[str, HAEntity] = {}
    if off_entrance and not grid_cts:
        site_grid = _site_grid_entities(bess_integration)
    if site_grid and bess_integration:
        site_rate = next(
            (q.panel_entities["grid_power"] for q in off_entrance
             if q.panel_entities.get("grid_power")),
            None,
        )
        for role, direction in (("grid_import", "import"), ("grid_export", "export")):
            if entity := site_grid.get(direction):
                assignments.append(EnergyRole(
                    role=role, entity_id=entity.entity_id,
                    platform=bess_integration.platform, preferred=True,
                    reason=(
                        f"Panel lugs not at the service entrance: {bess_integration.platform} "
                        "meters the grid"
                    ),
                    rate_entity_id=site_rate,
                ))

    # --- Grid + panel-level consumption (parents before sub-panels) ---
    parent_eids: dict[str, str | None] = {}
    for p in _sort_parents_first(panels, fed_by):
        consumed, produced = p.main_meter_consumed, p.main_meter_produced
        consumed_eid = consumed.entity_id if consumed and not consumed.disabled_by else None
        produced_eid = produced.entity_id if produced and not produced.disabled_by else None
        current_power = p.panel_entities.get("current_power")
        if p.serial in fed_by:
            parent_serial, feed_cid = fed_by[p.serial]
            reason = f"Sub-panel main meter, fed by {parent_serial}:{feed_cid}"
            parent = parent_eids.get(parent_serial)
        elif grid_cts:
            reason = "Panel main meter: grid comes from a remote CT given role grid"
            parent = None
        elif p.serial in lead_serials:
            preferred = True
            reason = "Panel main meter"
            if p.serial in off_entrance_serials:
                if site_grid and bess_integration:
                    preferred = False
                    reason = (
                        "Panel main meter: lugs not at the service entrance, "
                        f"{bess_integration.platform} meters the grid"
                    )
                else:
                    gaps.append(_grid_not_at_lugs_gap(p, bess_vendor, bess_integration))
            elif p in unknown_entrance:
                gaps.append(_service_entrance_unknown_gap(p))
            for role, eid, what in (
                ("grid_import", consumed_eid, "consumed"),
                ("grid_export", produced_eid, "produced"),
            ):
                if eid:
                    assignments.append(EnergyRole(
                        role=role, entity_id=eid, platform=DOMAIN,
                        preferred=preferred, reason=f"{reason}: {what} energy",
                        rate_entity_id=current_power,
                    ))
            parent_eids[p.serial] = None
            continue
        else:
            reason = "Panel main meter: feed unknown, counted without a parent"
            parent = None
        if consumed_eid:
            assignments.append(EnergyRole(
                role="device_consumption", entity_id=consumed_eid, platform=DOMAIN,
                preferred=True, reason=reason, parent_entity_id=parent,
                rate_entity_id=current_power,
            ))
        parent_eids[p.serial] = consumed_eid

    for p, c in grid_cts:
        if eid := c.entities.get("consumed_energy"):
            assignments.append(EnergyRole(
                role="grid_import", entity_id=eid, platform=DOMAIN, preferred=True,
                reason=f"Remote CT {c.id} given role grid: consumed energy",
                rate_entity_id=c.entities.get("power"),
            ))
        if eid := c.entities.get("produced_energy"):
            assignments.append(EnergyRole(
                role="grid_export", entity_id=eid, platform=DOMAIN, preferred=True,
                reason=f"Remote CT {c.id} given role grid: produced energy",
                rate_entity_id=c.entities.get("power"),
            ))

    # --- Several panels with an unknown feed relationship ---
    if len(panels) > 1:
        unknown = [p for p in panels if p.serial not in fed_by]
        if len(unknown) > 1:
            for p in unknown:
                if p.serial in lead_serials:
                    continue
                gaps.append(_subpanel_gap(p, lead, states))
            if len(lead) > 1:
                serials = ", ".join(sorted(lead_serials))
                gaps.append(Gap(
                    kind="feed_unknown",
                    serial=None,
                    circuit_id=None,
                    message=(
                        f"{len(panels)} panels and their feed relationship is unknown. Grid is "
                        f"taken from each of {serials}. If one panel feeds another, that panel's "
                        "flow is counted as grid twice, and a feeding circuit is also counted "
                        "as a consumer."
                    ),
                    hint=(
                        "--circuit-role <lead-serial>:<circuit-id>=subpanel:<sub-serial>, or "
                        f"<lead-serial>:{FEEDTHROUGH_ID}=subpanel:<sub-serial> when the lead's "
                        "feed-through lugs feed it"
                    ),
                ))

    # --- Battery ---
    battery_circuits = [
        (p, c) for p in panels for c in p.circuits
        if (ov := role_of(p, c)) is not None and ov.role == "battery"
    ]
    for p, c in battery_circuits:
        rate = p.panel_entities.get("battery_power")
        # A remote CT counts from the panel's side: discharge flows into the
        # panel (consumed), charge flows out (produced).  A breaker circuit is
        # the reverse.
        if c.remote_ct:
            discharge, charge = "consumed_energy", "produced_energy"
            what = "Remote CT"
        else:
            discharge, charge = "produced_energy", "consumed_energy"
            what = "Circuit"
        if eid := c.entities.get(discharge):
            assignments.append(EnergyRole(
                role="battery_discharge", entity_id=eid, platform=DOMAIN, preferred=True,
                reason=f"{what} {c.id} given role battery: {discharge} = discharge",
                rate_entity_id=rate,
            ))
        if eid := c.entities.get(charge):
            assignments.append(EnergyRole(
                role="battery_charge", entity_id=eid, platform=DOMAIN, preferred=True,
                reason=f"{what} {c.id} given role battery: {charge} = charge",
                rate_entity_id=rate,
            ))
    if battery_circuits and bess_integration:
        for entity in bess_integration.energy_entities:
            if "battery" in entity.entity_id:
                assignments.append(EnergyRole(
                    role="battery_discharge" if "export" in entity.entity_id else "battery_charge",
                    entity_id=entity.entity_id, platform=bess_integration.platform,
                    preferred=False,
                    reason="Battery circuit given: SPAN circuit is preferred",
                ))
    elif not battery_circuits and bess_subs:
        added = False
        if bess_integration:
            for entity in bess_integration.energy_entities:
                if "battery" not in entity.entity_id:
                    continue
                if "export" in entity.entity_id:
                    role = "battery_discharge"
                elif "import" in entity.entity_id:
                    role = "battery_charge"
                else:
                    continue
                added = True
                assignments.append(EnergyRole(
                    role=role, entity_id=entity.entity_id,
                    platform=bess_integration.platform, preferred=True,
                    reason=f"No battery circuit given: {bess_integration.platform} meters battery",
                ))
        if not added:
            for p in panels:
                if not p.sub_devices_of_type("bess"):
                    continue
                if bess_integration:
                    source = (
                        f"the {bess_integration.platform} integration has no energy sensor "
                        "hass-atlas recognizes as battery import or export"
                    )
                    otherwise = (
                        f"Otherwise add the {bess_integration.platform} battery charge and "
                        "discharge energy sensors to the Energy Dashboard by hand."
                    )
                else:
                    source = "no vendor battery integration was found"
                    vendor = _vendor_of(p.sub_devices_of_type("bess"))
                    otherwise = f"Otherwise {integration_hint(vendor, 'battery')}."
                suggestions, note = _closest_circuits(
                    [p], states, p.panel_entities.get("battery_power"), "battery power",
                    "battery", "it is charging or discharging",
                )
                gaps.append(Gap(
                    kind="battery_no_energy",
                    serial=p.serial,
                    circuit_id=None,
                    message=(
                        f"Panel {p.serial} has a battery, but span_panel publishes no battery "
                        f"charge/discharge energy and {source}. The dashboard battery slot "
                        "stays empty." + note
                    ),
                    hint=(
                        f"If a breaker feeds the battery: --circuit-role {p.serial}:"
                        f"<circuit-id>=battery. {otherwise}"
                    ),
                    suggestions=suggestions,
                ))

    # --- Solar ---
    pv_subs = [s for p in panels for s in p.sub_devices_of_type("pv")]
    pv_integration = _find_platform_for_vendor(_vendor_of(pv_subs), integrations)
    solar_circuits: list[tuple[PanelTopology, PanelCircuit]] = []
    for p in panels:
        for c in p.circuits:
            ov = role_of(p, c)
            if ov is not None:
                if ov.role == "solar":
                    solar_circuits.append((p, c))
            elif (
                not c.remote_ct
                and p.is_generation_circuit(c)
                and not p.non_key_meter_member(c)
            ):
                solar_circuits.append((p, c))
    for p, c in solar_circuits:
        # A remote CT counts from the panel's side: PV output flows into the
        # panel, so it is the CT's consumed energy.
        key = "consumed_energy" if c.remote_ct else "produced_energy"
        eid = c.entities.get(key)
        if not eid:
            continue
        rate = _solar_rate(p, c, sum(1 for q, _ in solar_circuits if q is p), pv_integration)
        if rate is None:
            warnings.append(
                f"Solar circuit {p.serial}:{c.id} has no power sensor for stat_rate, so the "
                "Energy Dashboard 'Now' tab will not show live solar power."
            )
        if c.remote_ct:
            reason = f"Remote CT {c.id} given role solar: consumed energy = solar production"
        else:
            reason = f"Generation circuit {c.id}: produced energy = solar production"
        assignments.append(EnergyRole(
            role="solar", entity_id=eid, platform=DOMAIN, preferred=True,
            reason=reason, rate_entity_id=rate,
        ))
    if solar_circuits and pv_integration:
        for entity in pv_integration.energy_entities:
            assignments.append(EnergyRole(
                role="solar", entity_id=entity.entity_id, platform=pv_integration.platform,
                preferred=False, reason="SPAN generation circuit is preferred",
            ))
    elif not solar_circuits:
        has_pv = [p for p in panels if p.sub_devices_of_type("pv")]
        if pv_integration and has_pv:
            for entity in pv_integration.energy_entities:
                assignments.append(EnergyRole(
                    role="solar", entity_id=entity.entity_id,
                    platform=pv_integration.platform, preferred=True,
                    reason=f"No generation circuit: {pv_integration.platform} meters solar",
                ))
        else:
            for p in has_pv:
                suggestions, note = _closest_circuits(
                    [p], states, p.panel_entities.get("pv_power"), "PV power", "solar",
                    "it is producing",
                )
                vendor = _vendor_of(p.sub_devices_of_type("pv"))
                gaps.append(Gap(
                    kind="solar_no_energy",
                    serial=p.serial,
                    circuit_id=None,
                    message=(
                        f"Panel {p.serial} has a PV system, but no circuit is marked as "
                        "generation and no vendor solar integration was found." + note
                    ),
                    hint=(
                        f"--circuit-role {p.serial}:<circuit-id>=solar, or "
                        f"{integration_hint(vendor, 'solar')}"
                    ),
                    suggestions=suggestions,
                ))

    # --- Circuits: device consumption and unclassified remote CTs ---
    for p in panels:
        parent = parent_eids.get(p.serial)
        for c in p.circuits:
            ov = role_of(p, c)
            if ov is None:
                if c.remote_ct:
                    gaps.append(_remote_ct_gap(p, c, states))
                    continue
                if p.is_generation_circuit(c) or p.non_key_meter_member(c):
                    continue
            elif ov.role != "load":
                continue
            eid = c.entities.get("consumed_energy")
            if not eid:
                continue
            if ov is not None:
                reason = f"Circuit given role load ({c.label})"
            elif c.shared_meter_group:
                reason = f"Shared meter group {c.shared_meter_group}: counted once"
            else:
                reason = "Circuit consumed energy"
            assignments.append(EnergyRole(
                role="device_consumption", entity_id=eid, platform=DOMAIN, preferred=True,
                reason=reason, parent_entity_id=parent,
                rate_entity_id=c.entities.get("power"),
            ))

    topo = EnergyTopology(
        panels=[],
        integrations=integrations,
        circuit_roles=[],
        role_assignments=assignments,
        warnings=warnings,
    )
    return topo, gaps


def _site_grid_entities(integration: EnergyIntegration | None) -> dict[str, HAEntity]:
    """A vendor integration's site (grid) import and export energy entities.

    Prefers ids containing ``site_import``/``site_export``; never a battery entity.
    """
    found: dict[str, HAEntity] = {}
    if integration is None:
        return found
    for direction in ("import", "export"):
        candidates = [
            e for e in integration.energy_entities
            if direction in e.entity_id and "battery" not in e.entity_id
        ]
        candidates.sort(key=lambda e: f"site_{direction}" not in e.entity_id)
        if candidates:
            found[direction] = candidates[0]
    return found


def _service_entrance_unknown_gap(p: PanelTopology) -> Gap:
    return Gap(
        kind="service_entrance_unknown",
        serial=p.serial,
        circuit_id=None,
        message=(
            f"hass-atlas does not know where the service entrance is for panel {p.serial}: "
            "the panel does not say whether its main lugs connect to the utility. Grid "
            "import and export come from this panel's main meter, which is correct only if "
            "nothing (a battery, another panel) sits between it and the utility. Please "
            "confirm, or tell hass-atlas what feeds this panel."
        ),
        hint=(
            f"If a remote CT measures the service: --circuit-role {p.serial}:<ct-id>=grid. "
            "If another panel feeds it: --circuit-role <lead-serial>:<circuit-id>="
            f"subpanel:{p.serial}. If a battery sits upstream, add its vendor integration."
        ),
    )


def _grid_not_at_lugs_gap(
    p: PanelTopology,
    bess_vendor: str | None,
    bess_integration: EnergyIntegration | None,
) -> Gap:
    message = (
        f"hass-atlas does not know where the service entrance is for panel {p.serial}. "
        "The panel reports that its main lugs are fed by something other than the utility "
        "(for example a battery ahead of the main lugs, or another panel), but not what. "
        "Until you say, grid import and export come from this panel's main meter, which "
        "leaves out whatever sits upstream. Please tell hass-atlas what feeds this panel."
    )
    if bess_integration:
        message += (
            f" The {bess_integration.platform} integration has no site import/export energy "
            "sensor, and its battery energy is also counted inside this grid import."
        )
    hints = [f"If a remote CT measures the service: --circuit-role {p.serial}:<ct-id>=grid."]
    if p.sub_devices_of_type("bess") and not bess_integration:
        hints.append(
            f"If the battery is upstream, {integration_hint(bess_vendor, 'battery')} for "
            "site grid energy."
        )
    hints.append(
        "If another panel feeds it: --circuit-role <lead-serial>:<circuit-id>="
        f"subpanel:{p.serial}."
    )
    return Gap(
        kind="grid_not_at_lugs",
        serial=p.serial,
        circuit_id=None,
        message=message,
        hint=" ".join(hints),
    )


def _solar_rate(
    p: PanelTopology,
    c: PanelCircuit,
    solar_count: int,
    pv_integration: EnergyIntegration | None,
) -> str | None:
    """stat_rate for a solar circuit: PV site power, panel pv_power, then the circuit's power.

    The site-wide sensors are used only when this is the panel's only solar
    circuit; with several, each gets its own circuit power.  A circuit's power
    reads positive while producing only for a generation circuit (span_panel
    flips its sign) or an import-positive remote CT that is not one.
    """
    if solar_count == 1:
        for s in p.sub_devices_of_type("pv"):
            site = (s.get("solar") or {}).get("site_power_entity_id")
            if site:
                return str(site)
        if pv := p.panel_entities.get("pv_power"):
            return pv
    if c.remote_ct != c.generation and (power := c.entities.get("power")):
        return power
    if pv_integration and (prod := _find_production_power(pv_integration)):
        return prod.entity_id
    return None


def _remote_ct_gap(
    p: PanelTopology,
    c: PanelCircuit,
    states: dict[str, dict[str, Any]],
) -> Gap:
    power = c.entities.get("power")
    reference = p.panel_entities.get("grid_power")
    label = "panel Grid Power"
    if not reference:
        reference = p.panel_entities.get("current_power")
        label = "panel Current Power"
    message = (
        f"{c.label} ({c.id}) on {p.serial} is a meter outside the panel; hass-atlas cannot "
        "tell what it measures, so it is not counted. "
        f"Power {_fmt(states, power)} ({label} {_fmt(states, reference)}); "
        f"consumed {_fmt(states, c.entities.get('consumed_energy'))}, "
        f"produced {_fmt(states, c.entities.get('produced_energy'))}."
    )
    suggestions: list[str] = []
    ct, grid = _reading(states, power), _reading(states, reference)
    if ct is not None and grid is not None:
        if ct != 0.0 and ct == grid:
            suggestions.append(
                f"Its power equals the {label}; it is likely the service (grid) CT: "
                f"--circuit-role {p.serial}:{c.id}=grid"
            )
        elif abs(grid) >= 50.0 and abs(abs(ct) - abs(grid)) <= 0.05 * abs(grid):
            suggestions.append(
                f"Its power is within 5% of the {label}; it may be the service (grid) CT: "
                f"--circuit-role {p.serial}:{c.id}=grid"
            )
    return Gap(
        kind="unclassified_remote_ct",
        serial=p.serial,
        circuit_id=c.id,
        message=message,
        hint=f"--circuit-role {p.serial}:{c.id}={ROLE_SYNTAX}",
        suggestions=suggestions,
    )


def _closest_circuits(
    panels: list[PanelTopology],
    states: dict[str, dict[str, Any]],
    target_entity_id: str | None,
    target_label: str,
    role: str,
    activity: str,
    limit: int = 3,
) -> tuple[list[str], str]:
    """Suggest circuits whose power magnitude is closest to a reference power reading.

    Only offered when the reference reads at least 50 W, so an idle battery
    or a night-time PV reading does not rank every quiet circuit.  Returns the
    suggestions and, when there are none, a sentence saying why.
    """
    target = _reading(states, target_entity_id)
    if target is None:
        return [], f" No suggestion: {target_label} reading unavailable."
    if abs(target) < 50.0:
        return [], f" No suggestion: {target_label} reads {target:g} W; rerun while {activity}."
    candidates: list[tuple[float, str, str, PanelCircuit, float]] = []
    for p in panels:
        for c in p.circuits:
            value = _reading(states, c.entities.get("power"))
            if value is not None:
                candidates.append((abs(abs(value) - abs(target)), p.serial, c.id, c, value))
    if not candidates:
        return [], " No suggestion: no circuit power reading available."
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    return [
        f"{serial}:{cid} ({c.label}) at {value:g} W vs {target_label} {target:g} W: "
        f"--circuit-role {serial}:{cid}={role}"
        for _, serial, cid, c, value in candidates[:limit]
    ], ""


def _subpanel_gap(
    sub: PanelTopology,
    lead: list[PanelTopology],
    states: dict[str, dict[str, Any]],
) -> Gap:
    """Gap for a panel whose lugs name a feeding device, with closest-power suggestions."""
    sub_power_eid = sub.panel_entities.get("current_power")
    sub_power = _reading(states, sub_power_eid)
    feedthrough: list[str] = []
    disabled: list[str] = []
    for p in lead:
        eid = p.panel_entities.get("feedthrough_power")
        value = _reading(states, eid)
        if value is None:
            if eid:
                disabled.append(f"{eid} on {p.serial}")
            continue
        if sub_power is not None and abs(sub_power) >= 50.0 and (
            abs(abs(value) - abs(sub_power)) <= 0.05 * abs(sub_power)
        ):
            feedthrough.append(
                f"{p.serial} feed-through lugs at {value:g} W vs {sub.serial} power "
                f"{sub_power:g} W: --circuit-role {p.serial}:{FEEDTHROUGH_ID}="
                f"subpanel:{sub.serial}"
            )
    suggestions, note = _closest_circuits(
        lead, states, sub_power_eid, f"{sub.serial} power", f"subpanel:{sub.serial}",
        "it draws power",
    )
    message = (
        f"Panel {sub.serial} is not at the service entrance, but what feeds it is unknown. "
        "Its main meter is counted without a parent; if a circuit feeds it, that circuit is "
        "also counted as a consumer (double-count)."
    )
    if not feedthrough:
        message += note
    if disabled:
        message += (
            " To compare against a lead panel's feed-through lugs, enable "
            f"{', '.join(disabled)}."
        )
    return Gap(
        kind="subpanel_feed_unknown",
        serial=sub.serial,
        circuit_id=None,
        message=message,
        hint=(
            f"--circuit-role <lead-serial>:<circuit-id>=subpanel:{sub.serial}, or "
            f"<lead-serial>:{FEEDTHROUGH_ID}=subpanel:{sub.serial} when the lead's "
            "feed-through lugs feed it"
        ),
        suggestions=feedthrough + suggestions,
    )


# ---------------------------------------------------------------------------
# Checks against current prefs (energy-audit, audit)
# ---------------------------------------------------------------------------


def find_misplaced_consumers(
    panels: list[PanelTopology],
    prefs: dict[str, Any],
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> list[Gap]:
    """Circuits listed as device consumers in current prefs that should not be."""
    roles = roles or {}
    listed = {
        d.get("stat_consumption") for d in prefs.get("device_consumption", []) or []
    }
    found: list[Gap] = []
    for p in panels:
        for c in p.circuits:
            eid = c.entities.get("consumed_energy")
            if not eid or eid not in listed:
                continue
            ov = roles.get((p.serial, c.id))
            if ov is not None:
                if ov.role == "load":
                    continue
                kind = "consumer_role_mismatch"
                why = f"is given role {ov}"
            elif c.remote_ct:
                kind = "consumer_remote_ct"
                why = "is an unclassified meter outside the panel"
            elif p.non_key_meter_member(c):
                kind = "consumer_shared_meter_member"
                why = (
                    f"shares meter group {c.shared_meter_group}, whose readings are "
                    "already counted by the group-key member"
                )
            elif p.is_generation_circuit(c):
                kind = "consumer_generation"
                why = "is a generation circuit (solar, not a consumer)"
            else:
                continue
            hint = "Remove it from the Energy Dashboard's individual devices"
            if ov is None:
                hint += f", or --circuit-role {p.serial}:{c.id}=load if it is a load"
            found.append(Gap(
                kind=kind,
                serial=p.serial,
                circuit_id=c.id,
                message=f"{eid} ({c.label}) is listed as a device consumer but {why}.",
                hint=hint + ".",
            ))
    return found
