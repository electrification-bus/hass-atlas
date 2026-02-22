"""Rich-based output formatters."""

from __future__ import annotations

import json as json_mod
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

if TYPE_CHECKING:
    from ha_atlas.models import HADevice, HAEntity, SpanDeviceTree

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
            ("Site Metering", span_tree.site_metering),
            ("Solar PV", span_tree.solar),
            ("Battery", span_tree.battery),
            ("EV Charger", span_tree.ev_charger),
        ]:
            if device:
                branch = root.add(f"[bold yellow]{label}[/bold yellow]")
                _add_device_branch(branch, device, energy_entity_ids)

        if span_tree.circuits:
            circuits_branch = root.add(
                f"[bold blue]Circuits[/bold blue] ({len(span_tree.circuits)})"
            )
            for circuit in sorted(span_tree.circuits, key=lambda d: d.display_name):
                _add_device_branch(circuits_branch, circuit, energy_entity_ids)

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
