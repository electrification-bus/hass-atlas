"""Energy command — auto-configure HA Energy Dashboard for SPAN."""

from __future__ import annotations

import copy

import click

from ha_atlas.context import Context, pass_ctx, run_async
from ha_atlas.models import HADevice, HAEntity, SpanDeviceTree
from ha_atlas.output import console, print_dry_run, print_info, print_ok, print_warn
from ha_atlas.registry import fetch_energy_prefs, fetch_span_trees


@click.command()
@pass_ctx
def energy(ctx: Context) -> None:
    """Auto-configure the Energy Dashboard for SPAN devices."""
    run_async(_energy(ctx))


async def _energy(ctx: Context) -> None:
    async with ctx.client() as client:
        trees = await fetch_span_trees(client)
        current_prefs = await fetch_energy_prefs(client)

    if not trees:
        print_warn("No SPAN devices found")
        return

    proposed = build_energy_config(trees)
    merged = merge_prefs(current_prefs, proposed)

    # Show diff
    _show_diff(current_prefs, merged)

    if ctx.dry_run:
        print_dry_run("Would save energy dashboard config (use without --dry-run to apply)")
        return

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **merged)

    print_ok("Energy dashboard config saved")


def _find_energy_entities(device: HADevice) -> list[HAEntity]:
    """Find enabled energy entities on a device."""
    return [
        e for e in device.entities
        if e.device_class == "energy"
        and e.state_class == "total_increasing"
        and not e.disabled_by
    ]


def _find_entity_by_property(device: HADevice, property_suffix: str) -> HAEntity | None:
    """Find an energy entity whose unique_id ends with a given property name."""
    for entity in _find_energy_entities(device):
        if entity.unique_id.endswith(property_suffix):
            return entity
    return None


def build_energy_config(trees: list[SpanDeviceTree]) -> dict:
    """Build proposed energy dashboard config from SPAN device trees."""
    energy_sources: list[dict] = []
    device_consumption: list[dict] = []

    for tree in trees:
        # Grid — look for upstream energy on site_metering or panel device
        grid_device = tree.site_metering or tree.panel
        imported = _find_entity_by_property(grid_device, "imported-energy")
        exported = _find_entity_by_property(grid_device, "exported-energy")

        if imported or exported:
            grid_source: dict = {"type": "grid", "flow_from": [], "flow_to": []}
            if imported:
                grid_source["flow_from"].append({
                    "stat_energy_from": imported.entity_id,
                })
            if exported:
                grid_source["flow_to"].append({
                    "stat_energy_to": exported.entity_id,
                })
            energy_sources.append(grid_source)

        # Solar PV
        if tree.solar:
            solar_energy = _find_entity_by_property(tree.solar, "imported-energy")
            if not solar_energy:
                # Try any energy entity on the solar device
                solar_entities = _find_energy_entities(tree.solar)
                solar_energy = solar_entities[0] if solar_entities else None
            if solar_energy:
                energy_sources.append({
                    "type": "solar",
                    "stat_energy_from": solar_energy.entity_id,
                })

        # Battery
        if tree.battery:
            discharge = _find_entity_by_property(tree.battery, "imported-energy")
            charge = _find_entity_by_property(tree.battery, "exported-energy")
            if discharge or charge:
                batt_source: dict = {"type": "battery"}
                if discharge:
                    batt_source["stat_energy_from"] = discharge.entity_id
                if charge:
                    batt_source["stat_energy_to"] = charge.entity_id
                energy_sources.append(batt_source)

        # Circuit consumption
        for circuit in tree.circuits:
            circuit_energy = _find_entity_by_property(circuit, "imported-energy")
            if circuit_energy:
                device_consumption.append({
                    "stat_consumption": circuit_energy.entity_id,
                })

    return {
        "energy_sources": energy_sources,
        "device_consumption": device_consumption,
    }


def merge_prefs(current: dict, proposed: dict) -> dict:
    """Merge proposed config into current, only adding missing entries."""
    merged = copy.deepcopy(current)

    # Merge energy_sources — add sources not already present (by type + entity_id)
    existing_sources = merged.get("energy_sources", [])
    existing_source_keys = _source_keys(existing_sources)
    for source in proposed.get("energy_sources", []):
        key = _source_key(source)
        if key not in existing_source_keys:
            existing_sources.append(source)
            existing_source_keys.add(key)
    merged["energy_sources"] = existing_sources

    # Merge device_consumption — add entries not already present
    existing_consumption = merged.get("device_consumption", [])
    existing_stats = {d.get("stat_consumption") for d in existing_consumption}
    for entry in proposed.get("device_consumption", []):
        if entry.get("stat_consumption") not in existing_stats:
            existing_consumption.append(entry)
            existing_stats.add(entry.get("stat_consumption"))
    merged["device_consumption"] = existing_consumption

    return merged


def _source_key(source: dict) -> str:
    """Generate a dedup key for an energy source."""
    stype = source.get("type", "")
    if stype == "grid":
        from_ids = sorted(f.get("stat_energy_from", "") for f in source.get("flow_from", []))
        to_ids = sorted(f.get("stat_energy_to", "") for f in source.get("flow_to", []))
        return f"grid:{','.join(from_ids)}:{','.join(to_ids)}"
    elif stype == "solar":
        return f"solar:{source.get('stat_energy_from', '')}"
    elif stype == "battery":
        return f"battery:{source.get('stat_energy_from', '')}:{source.get('stat_energy_to', '')}"
    return f"{stype}:{id(source)}"


def _source_keys(sources: list[dict]) -> set[str]:
    return {_source_key(s) for s in sources}


def _show_diff(current: dict, merged: dict) -> None:
    """Show what would change."""
    current_sources = current.get("energy_sources", [])
    merged_sources = merged.get("energy_sources", [])
    new_sources = len(merged_sources) - len(current_sources)

    current_consumption = current.get("device_consumption", [])
    merged_consumption = merged.get("device_consumption", [])
    new_consumption = len(merged_consumption) - len(current_consumption)

    console.rule("[bold]Energy Dashboard Changes[/bold]")

    if new_sources == 0 and new_consumption == 0:
        print_ok("No changes needed — energy dashboard is up to date")
        return

    if new_sources > 0:
        print_info(f"Adding {new_sources} energy source(s):")
        existing_keys = _source_keys(current_sources)
        for source in merged_sources:
            if _source_key(source) not in existing_keys:
                _print_source(source)

    if new_consumption > 0:
        existing_stats = {d.get("stat_consumption") for d in current_consumption}
        new_entries = [
            e for e in merged_consumption if e.get("stat_consumption") not in existing_stats
        ]
        print_info(f"Adding {new_consumption} circuit consumption sensor(s):")
        for entry in new_entries:
            console.print(f"  - {entry.get('stat_consumption')}")


def _print_source(source: dict) -> None:
    """Pretty-print an energy source."""
    stype = source.get("type", "unknown")
    if stype == "grid":
        for flow in source.get("flow_from", []):
            console.print(f"  - Grid import: {flow.get('stat_energy_from')}")
        for flow in source.get("flow_to", []):
            console.print(f"  - Grid export: {flow.get('stat_energy_to')}")
    elif stype == "solar":
        console.print(f"  - Solar: {source.get('stat_energy_from')}")
    elif stype == "battery":
        if f := source.get("stat_energy_from"):
            console.print(f"  - Battery discharge: {f}")
        if t := source.get("stat_energy_to"):
            console.print(f"  - Battery charge: {t}")
