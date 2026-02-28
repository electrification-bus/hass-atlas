"""Water commands — auto-configure HA Energy Dashboard water tab."""

from __future__ import annotations

import copy
from typing import Any

import click

from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.output import console, print_dry_run, print_info, print_ok, print_warn
from hass_atlas.registry import fetch_energy_prefs, fetch_entity_states


@click.command()
@click.argument("entity_ids", nargs=-1)
@pass_ctx
def water(ctx: Context, entity_ids: tuple[str, ...]) -> None:
    """Add water sensors to the Energy Dashboard water tab.

    Auto-discovers sensors with device_class=water and state_class=total_increasing,
    or accepts explicit ENTITY_IDS as arguments.
    """
    run_async(_water(ctx, entity_ids))


async def _water(ctx: Context, explicit_ids: tuple[str, ...]) -> None:
    async with ctx.client() as client:
        states = await fetch_entity_states(client)
        current_prefs = await fetch_energy_prefs(client)

    if explicit_ids:
        # Validate explicit IDs exist and are water sensors
        water_ids = _validate_explicit(explicit_ids, states)
    else:
        water_ids = _discover_water_sensors(states)

    if not water_ids:
        print_warn("No water sensors found")
        return

    print_info(f"Found {len(water_ids)} water sensor(s):")
    for eid in sorted(water_ids):
        attrs = states.get(eid, {}).get("attributes", {})
        unit = attrs.get("unit_of_measurement", "?")
        friendly = attrs.get("friendly_name", eid)
        console.print(f"  - {eid} ({friendly}, {unit})")

    merged = merge_water_prefs(current_prefs, water_ids)
    _show_diff(current_prefs, merged)

    if ctx.dry_run:
        print_dry_run("Would save energy dashboard config (use without --dry-run to apply)")
        return

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **merged)

    print_ok("Energy dashboard water config saved")


def _discover_water_sensors(states: dict[str, dict]) -> list[str]:
    """Find sensors with device_class=water and state_class=total_increasing."""
    water_ids = []
    for eid, state in states.items():
        if not eid.startswith("sensor."):
            continue
        attrs = state.get("attributes", {})
        if (
            attrs.get("device_class") == "water"
            and attrs.get("state_class") == "total_increasing"
        ):
            water_ids.append(eid)
    return sorted(water_ids)


def _validate_explicit(
    entity_ids: tuple[str, ...], states: dict[str, dict]
) -> list[str]:
    """Validate explicit entity IDs exist in HA."""
    valid = []
    for eid in entity_ids:
        if eid not in states:
            print_warn(f"Entity not found: {eid}")
        else:
            valid.append(eid)
    return valid


def merge_water_prefs(current: dict, water_ids: list[str]) -> dict:
    """Merge water sensors into current energy prefs, only adding missing entries."""
    merged = copy.deepcopy(current)

    existing_water = merged.get("device_consumption_water", [])
    existing_stats = {w.get("stat_consumption") for w in existing_water}

    for eid in water_ids:
        if eid not in existing_stats:
            existing_water.append({
                "stat_consumption": eid,
            })
            existing_stats.add(eid)

    merged["device_consumption_water"] = existing_water
    return merged


def _show_diff(current: dict, merged: dict) -> None:
    """Show what would change."""
    current_water = current.get("device_consumption_water", [])
    merged_water = merged.get("device_consumption_water", [])

    current_ids = {w.get("stat_consumption") for w in current_water}
    merged_ids = {w.get("stat_consumption") for w in merged_water}
    new_ids = merged_ids - current_ids

    console.rule("[bold]Energy Dashboard Water Changes[/bold]")

    if not new_ids:
        print_ok("No changes needed — water tab is up to date")
        return

    print_info(f"Adding {len(new_ids)} water source(s):")
    for eid in sorted(new_ids):
        console.print(f"  + {eid}")
