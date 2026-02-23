"""Audit command — device hierarchy + misconfiguration report."""

from __future__ import annotations

import click

from ha_atlas.context import Context, pass_ctx, run_async
from ha_atlas.energy import extract_energy_entity_ids
from ha_atlas.output import console, print_info, print_ok, print_warn, render_json, render_table, render_tree
from ha_atlas.registry import fetch_energy_prefs, fetch_span_trees


@click.command()
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["tree", "table", "json"]),
    default="tree",
    help="Output format",
)
@pass_ctx
def audit(ctx: Context, output_format: str) -> None:
    """Display SPAN device tree and report misconfigurations."""
    run_async(_audit(ctx, output_format))


async def _audit(ctx: Context, output_format: str) -> None:
    async with ctx.client() as client:
        trees = await fetch_span_trees(client)
        energy_prefs = await fetch_energy_prefs(client)

    if not trees:
        print_warn("No SPAN devices found in Home Assistant")
        return

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


def _report_no_area(trees: list) -> None:
    """Report circuit devices with no area assigned."""
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
