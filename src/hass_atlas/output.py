"""Rich-based output formatters."""

from __future__ import annotations

import json as json_mod
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

if TYPE_CHECKING:
    from hass_atlas.models import HADevice, HAEntity, SpanDeviceTree
    from hass_atlas.span_panel import CircuitRoleOverride, Gap, PanelTopology
    from hass_atlas.topology import EnergyTopology

console = Console()


def _entity_label(entity: HAEntity, energy_entity_ids: set[str] | None = None) -> str:
    """Format an entity for display."""
    parts = [entity.entity_id]
    tags: list[str] = []
    if entity.device_class:
        tags.append(entity.device_class)
    if entity.state_class:
        tags.append(entity.state_class)
    if entity.disabled_by:
        tags.append(f"[dim]disabled:{entity.disabled_by}[/dim]")
    if energy_entity_ids and entity.entity_id in energy_entity_ids:
        tags.append("[green]energy-dashboard[/green]")
    if tags:
        parts.append(f"({', '.join(tags)})")
    return " ".join(parts)


def _device_label(device: HADevice) -> str:
    """Format a device for display."""
    parts = [f"[bold]{device.display_name}[/bold]"]
    if device.model:
        parts.append(f"[dim]{device.model}[/dim]")
    if device.area_id:
        parts.append(f"[cyan]area:{device.area_id}[/cyan]")
    return " ".join(parts)


def render_tree(
    trees: list[SpanDeviceTree],
    energy_entity_ids: set[str] | None = None,
) -> None:
    """Render device trees using Rich Tree."""
    for span_tree in trees:
        serial = span_tree.serial or "unknown"
        root = Tree(f"[bold magenta]SPAN Panel[/bold magenta] ({serial})")
        _add_device_branch(root, span_tree.panel, energy_entity_ids)

        for label, device in [
            ("Upstream Lugs", span_tree.lugs_up),
            ("Downstream Lugs", span_tree.lugs_dn),
            ("Solar PV", span_tree.solar),
            ("Battery", span_tree.battery),
            ("EV Charger", span_tree.ev_charger),
        ]:
            if device:
                branch = root.add(f"[bold yellow]{label}[/bold yellow]")
                _add_device_branch(branch, device, energy_entity_ids)

        # MID is a grandchild of Battery — nest under it in the visual tree.
        if span_tree.mid:
            branch = root.add("[bold yellow]Microgrid Interconnect Device[/bold yellow]")
            _add_device_branch(branch, span_tree.mid, energy_entity_ids)

        if span_tree.circuits:
            circuits_branch = root.add(
                f"[bold blue]Circuits[/bold blue] ({len(span_tree.circuits)})"
            )
            for circuit in sorted(span_tree.circuits, key=lambda d: d.display_name):
                _add_device_branch(circuits_branch, circuit, energy_entity_ids)

        if span_tree.remote_cts:
            ct_branch = root.add(
                f"[bold blue]Remote CTs[/bold blue] ({len(span_tree.remote_cts)}, "
                "meters outside the panel)"
            )
            for ct in sorted(span_tree.remote_cts, key=lambda d: d.display_name):
                _add_device_branch(ct_branch, ct, energy_entity_ids)

        console.print(root)


def _add_device_branch(
    parent: Tree,
    device: HADevice,
    energy_entity_ids: set[str] | None = None,
) -> None:
    """Add a device and its entities as tree branches."""
    branch = parent.add(_device_label(device))
    for entity in sorted(device.entities, key=lambda e: e.entity_id):
        branch.add(_entity_label(entity, energy_entity_ids))


def render_table(
    trees: list[SpanDeviceTree],
    energy_entity_ids: set[str] | None = None,
) -> None:
    """Render devices and entities as a table."""
    table = Table(title="SPAN Devices & Entities")
    table.add_column("Device", style="bold")
    table.add_column("Model", style="dim")
    table.add_column("Area")
    table.add_column("Entity ID")
    table.add_column("Class")
    table.add_column("State Class")
    table.add_column("Energy?", justify="center")

    for span_tree in trees:
        all_devices = [span_tree.panel] + span_tree.all_child_devices + span_tree.circuits
        for device in all_devices:
            for i, entity in enumerate(sorted(device.entities, key=lambda e: e.entity_id)):
                in_energy = (
                    "Y" if energy_entity_ids and entity.entity_id in energy_entity_ids else ""
                )
                table.add_row(
                    device.display_name if i == 0 else "",
                    device.model if i == 0 else "",
                    device.area_id or "" if i == 0 else "",
                    entity.entity_id,
                    entity.device_class or "",
                    entity.state_class or "",
                    in_energy,
                )
            if not device.entities:
                table.add_row(
                    device.display_name, device.model or "", device.area_id or "",
                    "", "", "", "",
                )

    console.print(table)


def render_json(trees: list[SpanDeviceTree]) -> None:
    """Render as JSON."""
    import dataclasses

    data = [dataclasses.asdict(t) for t in trees]
    console.print_json(json_mod.dumps(data, default=str))


def print_info(msg: str) -> None:
    console.print(f"[bold blue]INFO[/bold blue] {msg}")


def print_warn(msg: str) -> None:
    console.print(f"[bold yellow]WARN[/bold yellow] {msg}")


def print_ok(msg: str) -> None:
    console.print(f"[bold green] OK [/bold green] {msg}")


def print_dry_run(msg: str) -> None:
    console.print(f"[bold cyan]DRY-RUN[/bold cyan] {msg}")


def print_error(msg: str) -> None:
    console.print(f"[bold red]ERROR[/bold red] {msg}")


# ---------------------------------------------------------------------------
# Topology rendering
# ---------------------------------------------------------------------------


_CONSUMPTION_REASON: dict[str, str] = {
    "span_ebus": "Circuit exported-energy = consumption",
    "span_panel": "Circuit consumed_energy = consumption",
}


def render_topology(
    topo: EnergyTopology,
    span_panels: list[PanelTopology] | None = None,
    states: dict[str, dict] | None = None,
) -> None:
    """Render energy topology as a Rich tree showing physical hierarchy and decisions.

    ``span_panels`` adds a span_panel panel summary where span_ebus panels
    are otherwise shown.
    """
    console.rule("[bold]Energy System Topology[/bold]")

    if span_panels:
        render_span_panel_summary(span_panels, states or {})

    # Panel topology
    for panel in topo.panels:
        lead = " [green](LEAD)[/green]" if panel.is_lead_panel else ""
        root = Tree(f"[bold magenta]SPAN Panel[/bold magenta] {panel.serial}{lead}")

        # Battery info
        if panel.battery_position:
            batt_label = f"[bold yellow]Battery[/bold yellow] position={panel.battery_position}"
            if panel.battery_vendor:
                batt_label += f" vendor={panel.battery_vendor}"
            if panel.battery_model:
                batt_label += f" model={panel.battery_model}"
            batt_branch = root.add(batt_label)
            if panel.battery_feed_circuit_name:
                batt_branch.add(f"feed-circuit: {panel.battery_feed_circuit_name}")

        # Solar info
        if panel.solar_position:
            solar_label = f"[bold cyan]Solar PV[/bold cyan] position={panel.solar_position}"
            if panel.solar_vendor:
                solar_label += f" vendor={panel.solar_vendor}"
            if panel.solar_product:
                solar_label += f" product={panel.solar_product}"
            solar_branch = root.add(solar_label)
            if panel.solar_feed_circuit_name:
                solar_branch.add(f"feed-circuit: {panel.solar_feed_circuit_name}")

        for name, node_id in panel.remote_cts:
            root.add(f"[bold blue]Remote CT[/bold blue] {escape(name)} [dim]{escape(node_id)}[/dim]")

        console.print(root)

    # Other energy integrations
    if topo.integrations:
        console.print()
        console.rule("[bold]Other Energy Integrations[/bold]")
        for integration in topo.integrations:
            int_tree = Tree(
                f"[bold green]{integration.platform}[/bold green] "
                f"({len(integration.energy_entities)} energy entities)"
            )
            for entity in integration.energy_entities:
                int_tree.add(entity.entity_id)
            console.print(int_tree)

    # Circuit roles
    if topo.circuit_roles:
        console.print()
        console.rule("[bold]Circuit Roles[/bold]")
        role_table = Table(show_header=True)
        role_table.add_column("Circuit", style="bold")
        role_table.add_column("Role")
        role_table.add_column("Return Energy")
        role_table.add_column("Consumption")
        role_table.add_column("Reason", style="dim")

        for cr in sorted(topo.circuit_roles, key=lambda c: c.circuit.display_name):
            role_style = {
                "load": "",
                "pv_feed": "[cyan]",
                "bess_feed": "[yellow]",
                "ev_feed": "[green]",
                "shared_meter_member": "[dim]",
            }.get(cr.role, "")
            role_end = "[/]" if role_style else ""
            return_status = "[red]suppressed[/red]" if cr.skip_return_energy else "[green]included[/green]"
            consumption_status = "[red]excluded[/red]" if cr.skip_consumption else "[green]included[/green]"
            role_table.add_row(
                cr.circuit.display_name,
                f"{role_style}{cr.role}{role_end}",
                return_status,
                consumption_status,
                cr.reason,
            )
        console.print(role_table)

    # Role assignments (preferred)
    console.print()
    console.rule("[bold]Energy Dashboard Assignments[/bold]")
    preferred = [a for a in topo.role_assignments if a.preferred]
    skipped = [a for a in topo.role_assignments if not a.preferred]

    if preferred:
        assign_table = Table(show_header=True, title="Preferred (will be configured)")
        assign_table.add_column("Role", style="bold")
        assign_table.add_column("Entity ID")
        assign_table.add_column("Platform")
        assign_table.add_column("Reason", style="dim")
        for a in preferred:
            if a.role == "device_consumption":
                continue  # Too many — summarize instead
            assign_table.add_row(a.role, a.entity_id, a.platform, a.reason)
        # Summarize device_consumption
        consumption = [a for a in preferred if a.role == "device_consumption"]
        if consumption:
            platforms = sorted({a.platform for a in consumption})
            assign_table.add_row(
                "device_consumption",
                f"({len(consumption)} circuits)",
                ", ".join(platforms),
                _CONSUMPTION_REASON.get(platforms[0], "Circuit consumption")
                if len(platforms) == 1 else "Circuit consumption",
            )
        console.print(assign_table)

    if skipped:
        skip_table = Table(show_header=True, title="Skipped (overlap detected)")
        skip_table.add_column("Role", style="bold")
        skip_table.add_column("Entity ID")
        skip_table.add_column("Platform")
        skip_table.add_column("Reason", style="dim")
        for a in skipped:
            skip_table.add_row(a.role, a.entity_id, a.platform, a.reason)
        console.print(skip_table)

    # Warnings
    if topo.warnings:
        console.print()
        for warning in topo.warnings:
            print_warn(warning)


# ---------------------------------------------------------------------------
# span_panel rendering
# ---------------------------------------------------------------------------


def render_span_panel_summary(
    panels: list[PanelTopology],
    states: dict[str, dict],
) -> None:
    """Render each span_panel panel with its sub-devices and circuit counts."""
    from hass_atlas.span_panel import at_service_entrance

    for panel in panels:
        size = f" size={panel.panel_size}" if panel.panel_size else ""
        root = Tree(
            f"[bold magenta]SPAN Panel[/bold magenta] {panel.serial} "
            f"[dim]{escape(panel.model or 'unknown model')}{size} {escape(panel.firmware)}[/dim]"
        )
        entrance = at_service_entrance(panel, states)
        if entrance is not None:
            root.add(f"at service entrance: {'yes' if entrance else 'no'}")
        meter = panel.main_meter_consumed
        root.add(f"main meter: {meter.entity_id if meter else '[red]not found[/red]'}")
        for sub in panel.sub_devices:
            label = (
                f"[bold yellow]{escape(str(sub.get('type', 'unknown')))}[/bold yellow] "
                f"{escape(str(sub.get('name')))}"
            )
            if sub.get("model"):
                label += f" [dim]{escape(str(sub.get('model')))}[/dim]"
            branch = root.add(label)
            solar = sub.get("solar")
            if solar:
                branch.add(escape(
                    f"vendor={solar.get('vendor')} feed-circuit={solar.get('feed_circuit_id')}"
                ))
        remote = sum(1 for c in panel.circuits if c.remote_ct)
        generation = sum(1 for c in panel.circuits if c.generation)
        grouped = sum(1 for c in panel.circuits if c.shared_meter_group)
        root.add(
            f"[bold blue]Circuits[/bold blue] ({len(panel.circuits)}): "
            f"{remote} remote CT, {generation} generation, {grouped} in shared meter groups"
        )
        console.print(root)


def render_span_panel_circuits(
    panels: list[PanelTopology],
    entity_areas: dict[str, str],
    area_names: dict[str, str],
    energy_entity_ids: set[str],
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
    assigned: set[str] | None = None,
) -> None:
    """Render a circuit table per span_panel panel.

    ``assigned`` holds the entity ids hass-atlas would use; the Energy? column
    reports whether a circuit's ones are in the dashboard ("Y", "partial"), and adds
    "extra" when the dashboard lists a circuit energy entity hass-atlas would not use.
    """
    roles = roles or {}
    assigned = assigned or set()

    def flag(value: bool | None) -> str:
        return "?" if value is None else ("Y" if value else "")

    # "*" marks a remote CT inferred from a missing breaker position (payload
    # without outside_panel).

    for panel in panels:
        table = Table(title=f"Circuits on {panel.serial}")
        table.add_column("Tabs")
        table.add_column("Circuit", style="bold")
        table.add_column("Type")
        table.add_column("Remote CT", justify="center")
        table.add_column("Generation", justify="center")
        table.add_column("Meter Group")
        table.add_column("Role")
        table.add_column("Areas")
        table.add_column("Energy?", justify="center")
        for c in panel.circuits:
            areas = sorted({
                area_names.get(entity_areas[eid], entity_areas[eid])
                for eid in c.entities.values()
                if eid in entity_areas
            })
            override = roles.get((panel.serial, c.id))
            energy_eids = [
                eid for key in ("consumed_energy", "produced_energy")
                if (eid := c.entities.get(key))
            ]
            used = [eid for eid in energy_eids if eid in assigned]
            in_dashboard = [eid for eid in used if eid in energy_entity_ids]
            mark = ""
            if used:
                mark = "Y" if len(in_dashboard) == len(used) else (
                    "partial" if in_dashboard else ""
                )
            if any(e in energy_entity_ids for e in energy_eids if e not in used):
                mark = f"{mark} extra".strip()
            table.add_row(
                ",".join(str(t) for t in c.tabs) or "-",
                f"{escape(c.label)} [dim]{escape(c.id)}[/dim]" if c.name else escape(c.id),
                c.device_type,
                ("Y" if c.remote_ct else "") + ("*" if c.outside_panel is None else ""),
                flag(c.is_generation),
                c.shared_meter_group or "",
                str(override) if override else "",
                ", ".join(areas),
                mark,
            )
        console.print(table)


def render_gaps(gaps: list[Gap]) -> None:
    """Render what hass-atlas could not determine, with how to state it."""
    console.print()
    console.rule("[bold]Gaps[/bold]")
    if not gaps:
        print_ok("Nothing left undetermined")
        return
    for gap in gaps:
        where = ":".join(x for x in (gap.serial, gap.circuit_id) if x)
        print_warn(escape(f"[{gap.kind}]{' ' + where if where else ''} {gap.message}"))
        for suggestion in gap.suggestions:
            console.print(f"    suggestion (not applied): {escape(suggestion)}")
        if gap.hint:
            console.print(f"    fix: {escape(gap.hint)}")
