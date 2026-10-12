"""Audit command — device hierarchy + misconfiguration report."""

from __future__ import annotations

import copy
import json as json_mod
from typing import Any

import click

from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.energy import _build_topology, extract_energy_entity_ids
from hass_atlas.models import HADevice, HAEntity
from hass_atlas.output import (
    console,
    print_info,
    print_ok,
    print_warn,
    render_gaps,
    render_json,
    render_span_panel_circuits,
    render_span_panel_summary,
    render_table,
    render_tree,
)
from hass_atlas.registry import (
    build_span_trees,
    enrich_entities_from_states,
    entity_area_ids,
    fetch_energy_prefs,
    fetch_entity_states,
    fetch_raw_registries,
    parse_registries,
)
from hass_atlas.span_panel import (
    CIRCUIT_ROLE_HELP,
    CircuitRoleOverride,
    Gap,
    PanelTopology,
    build_energy_topology_span_panel,
    fetch_panel_topologies,
    find_misplaced_consumers,
    resolve_circuit_roles,
    resolve_ebus_circuit_roles,
    span_panel_only,
)
from hass_atlas.topology import discover_energy_integrations


@click.command()
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["tree", "table", "json"]),
    default="tree",
    help="Output format",
)
@click.option(
    "--circuit-role", "circuit_role", multiple=True, metavar="[SERIAL:]CIRCUIT=ROLE",
    help=CIRCUIT_ROLE_HELP,
)
@pass_ctx
def audit(ctx: Context, output_format: str, circuit_role: tuple[str, ...]) -> None:
    """Display SPAN device tree and report misconfigurations."""
    run_async(_audit(ctx, output_format, circuit_role))


async def _audit(
    ctx: Context, output_format: str, circuit_role: tuple[str, ...] = ()
) -> None:
    async with ctx.client() as client:
        raw_devices, raw_entities, raw_areas = await fetch_raw_registries(client)
        devices, entities, areas = parse_registries(raw_devices, raw_entities, raw_areas)
        trees = build_span_trees(devices, entities)
        energy_prefs = await fetch_energy_prefs(client)
        if not trees and span_panel_only(devices):
            states = await fetch_entity_states(client)
            panels = await fetch_panel_topologies(client, devices, entities)
            enrich_entities_from_states(entities, states)
            area_names = {a.area_id: a.name for a in areas}
            _audit_span_panel(
                panels, devices, entities, states, energy_prefs,
                entity_area_ids(raw_entities), area_names, circuit_role, output_format,
            )
            return
        states = await fetch_entity_states(client) if trees else {}

    if not trees:
        print_warn("No SPAN devices found in Home Assistant")
        return

    # Enrich a copy, for vendor integration discovery, so the diagnostics below
    # keep reporting the registry's own device and state classes.
    topo_entities = copy.deepcopy(entities)
    enrich_entities_from_states(topo_entities, states)
    roles = resolve_ebus_circuit_roles(circuit_role, trees)
    gaps = _build_topology(trees, devices, topo_entities, states, roles).gaps

    # Collect entity_ids currently in energy dashboard
    energy_entity_ids = extract_energy_entity_ids(energy_prefs)

    print_info(f"Found {len(trees)} SPAN panel(s)")
    console.print()

    if output_format == "tree":
        render_tree(trees, energy_entity_ids)
    elif output_format == "table":
        render_table(trees, energy_entity_ids)
    else:
        render_json(trees)

    # Diagnostics
    console.print()
    console.rule("[bold]Diagnostics[/bold]")
    _report_no_area(trees)
    _report_energy_gaps(trees, energy_entity_ids)
    _report_disabled(trees)
    render_gaps(gaps)


def _report_no_area(trees: list) -> None:
    """Report circuit devices with no area assigned (remote CTs are not circuits)."""
    missing = []
    for tree in trees:
        for circuit in tree.circuits:
            if not circuit.area_id:
                missing.append(circuit)
    if missing:
        print_warn(f"{len(missing)} circuit(s) have no area assigned:")
        for d in missing:
            console.print(f"  - {d.display_name}")
    else:
        print_ok("All circuits have areas assigned")


def _report_energy_gaps(trees: list, energy_entity_ids: set[str]) -> None:
    """Report energy sensors not in the energy dashboard."""
    missing = []
    for tree in trees:
        all_devices = [tree.panel] + tree.all_child_devices + tree.circuits
        for device in all_devices:
            for entity in device.entities:
                if (
                    entity.device_class == "energy"
                    and entity.state_class == "total_increasing"
                    and not entity.disabled_by
                    and entity.entity_id not in energy_entity_ids
                ):
                    missing.append((device, entity))
    if missing:
        print_warn(f"{len(missing)} energy sensor(s) NOT in energy dashboard:")
        for device, entity in missing:
            console.print(f"  - {entity.entity_id} ({device.display_name})")
    else:
        print_ok("All enabled energy sensors are in the energy dashboard")


def _report_disabled(trees: list) -> None:
    """Report disabled energy-relevant entities."""
    disabled = []
    for tree in trees:
        all_devices = [tree.panel] + tree.all_child_devices + tree.circuits
        for device in all_devices:
            for entity in device.entities:
                if entity.disabled_by and entity.device_class == "energy":
                    disabled.append((device, entity))
    if disabled:
        print_warn(f"{len(disabled)} energy sensor(s) are disabled:")
        for device, entity in disabled:
            console.print(f"  - {entity.entity_id} (disabled by: {entity.disabled_by})")


# ---------------------------------------------------------------------------
# span_panel
# ---------------------------------------------------------------------------


def _audit_span_panel(
    panels: list[PanelTopology],
    devices: list[HADevice],
    entities: list[HAEntity],
    states: dict[str, dict[str, Any]],
    energy_prefs: dict[str, Any],
    entity_areas: dict[str, str],
    area_names: dict[str, str],
    circuit_role: tuple[str, ...],
    output_format: str,
) -> None:
    """Audit span_panel panels: tree, circuit table, energy gaps, and Gap list."""
    if not panels:
        print_warn("No span_panel panels found in Home Assistant")
        return
    roles = resolve_circuit_roles(circuit_role, panels)
    integrations = discover_energy_integrations(devices, entities, exclude_platform="span_panel")
    topo, gaps = build_energy_topology_span_panel(panels, states, integrations, roles)
    gaps += find_misplaced_consumers(panels, energy_prefs, roles)
    energy_entity_ids = extract_energy_entity_ids(energy_prefs)
    missing = sorted(
        {a.entity_id for a in topo.role_assignments if a.preferred and a.platform == "span_panel"}
        - energy_entity_ids
    )

    if output_format == "json":
        console.print_json(json_mod.dumps(
            _span_panel_json(panels, roles, entity_areas, area_names, missing, gaps),
            default=str,
        ))
        return

    print_info(f"Found {len(panels)} span_panel panel(s)")
    console.print()
    if output_format == "tree":
        render_span_panel_summary(panels, states)
    render_span_panel_circuits(
        panels, entity_areas, area_names, energy_entity_ids, roles,
        assigned={a.entity_id for a in topo.role_assignments if a.preferred},
    )

    console.print()
    console.rule("[bold]Diagnostics[/bold]")
    if missing:
        print_warn(f"{len(missing)} energy sensor(s) hass-atlas would use are NOT in the energy "
                   "dashboard:")
        for eid in missing:
            console.print(f"  - {eid}")
    else:
        print_ok("All energy sensors hass-atlas would use are in the energy dashboard")
    render_gaps(gaps)


def _span_panel_json(
    panels: list[PanelTopology],
    roles: dict[tuple[str, str], CircuitRoleOverride],
    entity_areas: dict[str, str],
    area_names: dict[str, str],
    missing: list[str],
    gaps: list[Gap],
) -> dict[str, Any]:
    out_panels = []
    for p in panels:
        circuits = []
        for c in p.circuits:
            override = roles.get((p.serial, c.id))
            circuits.append({
                "id": c.id,
                "name": c.name,
                "tabs": c.tabs,
                "device_type": c.device_type,
                "outside_panel": c.outside_panel,
                "remote_ct": c.remote_ct,
                "is_generation": c.is_generation,
                "shared_meter_group": c.shared_meter_group,
                "shared_relay_group": c.shared_relay_group,
                "role": str(override) if override else None,
                "areas": sorted({
                    area_names.get(entity_areas[eid], entity_areas[eid])
                    for eid in c.entities.values() if eid in entity_areas
                }),
                "entities": c.entities,
            })
        out_panels.append({
            "serial": p.serial,
            "device_id": p.panel.id,
            "model": p.model,
            "panel_size": p.panel_size,
            "firmware": p.firmware,
            "main_meter_consumed": (
                p.main_meter_consumed.entity_id if p.main_meter_consumed else None
            ),
            "main_meter_produced": (
                p.main_meter_produced.entity_id if p.main_meter_produced else None
            ),
            "panel_entities": p.panel_entities,
            "sub_devices": [
                {k: v for k, v in s.items() if k != "entities"} for s in p.sub_devices
            ],
            "circuits": circuits,
            "missing_payload_fields": p.missing_fields,
        })
    return {
        "integration": "span_panel",
        "panels": out_panels,
        "energy_entities_missing_from_dashboard": missing,
        "gaps": [g.to_dict() for g in gaps],
    }
