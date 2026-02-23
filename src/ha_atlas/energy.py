"""Energy commands — auto-configure and audit HA Energy Dashboard."""

from __future__ import annotations

import copy
from typing import Any

import click

from ha_atlas.context import Context, pass_ctx, run_async
from ha_atlas.models import HADevice, HAEntity, SpanDeviceTree
from ha_atlas.output import (
    console,
    print_dry_run,
    print_info,
    print_ok,
    print_warn,
    render_topology,
)
from ha_atlas.registry import (
    build_span_trees,
    enrich_entities_from_states,
    fetch_energy_prefs,
    fetch_entity_states,
    fetch_registries,
    fetch_span_trees,
)
from ha_atlas.topology import (
    EnergyTopology,
    build_energy_topology,
    classify_circuits,
    discover_energy_integrations,
    extract_span_topology,
)


@click.command()
@click.option("--topology", is_flag=True, default=False,
              help="Use topology-aware configuration (detects overlaps with other integrations)")
@pass_ctx
def energy(ctx: Context, topology: bool) -> None:
    """Auto-configure the Energy Dashboard for SPAN devices."""
    if topology:
        run_async(_energy_topology_config(ctx))
    else:
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


async def _energy_topology_config(ctx: Context) -> None:
    """Topology-aware energy dashboard configuration."""
    async with ctx.client() as client:
        devices, entities, areas = await fetch_registries(client)
        states = await fetch_entity_states(client)
        current_prefs = await fetch_energy_prefs(client)

    # Enrich entities with device_class/state_class from states
    # (entity registry doesn't include these — they're runtime properties)
    enrich_entities_from_states(entities, states)
    trees = build_span_trees(devices, entities)

    if not trees:
        print_warn("No SPAN devices found")
        return

    topo = _build_topology(trees, devices, entities, states)
    render_topology(topo)

    cleaned = apply_topology_prefs(current_prefs, topo)

    _show_topology_diff(current_prefs, cleaned)

    if ctx.dry_run:
        print_dry_run("Would save topology-aware energy dashboard config (use without --dry-run to apply)")
        return

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **cleaned)

    print_ok("Topology-aware energy dashboard config saved")


def _find_entity_by_property(device: HADevice, property_suffix: str) -> HAEntity | None:
    """Find an entity on a device whose unique_id ends with a given property name.

    Matches by unique_id suffix alone without requiring device_class/state_class.
    The span_ebus integration does not set these in the entity registry.
    """
    for entity in device.entities:
        if not entity.disabled_by and entity.unique_id.endswith(property_suffix):
            return entity
    return None


def build_energy_config(trees: list[SpanDeviceTree]) -> dict:
    """Build proposed energy dashboard config from SPAN device trees."""
    energy_sources: list[dict] = []
    device_consumption: list[dict] = []

    for tree in trees:
        # Grid — upstream energy entities.
        # In production: live on panel device (node: lugs-upstream)
        # In some setups: live on site_metering child device
        # Fallback chain: panel lugs-upstream → site_metering → panel generic
        imported = _find_entity_by_property(tree.panel, "lugs-upstream_imported-energy")
        if not imported and tree.site_metering:
            imported = _find_entity_by_property(tree.site_metering, "imported-energy")
        if not imported:
            imported = _find_entity_by_property(tree.panel, "imported-energy")

        exported = _find_entity_by_property(tree.panel, "lugs-upstream_exported-energy")
        if not exported and tree.site_metering:
            exported = _find_entity_by_property(tree.site_metering, "exported-energy")
        if not exported:
            exported = _find_entity_by_property(tree.panel, "exported-energy")

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

        # Circuit consumption — SPAN convention (panel perspective):
        # "exported-energy" = energy delivered TO circuit = consumption
        # "imported-energy" = backfeed FROM circuit (generation)
        for circuit in tree.circuits:
            circuit_energy = _find_entity_by_property(circuit, "exported-energy")
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


# ---------------------------------------------------------------------------
# Topology-aware configuration
# ---------------------------------------------------------------------------


def _build_topology(
    trees: list[SpanDeviceTree],
    devices: list[HADevice],
    entities: list[HAEntity],
    states: dict[str, Any],
) -> EnergyTopology:
    """Build complete energy topology from SPAN trees, registries, and entity states."""
    topologies = extract_span_topology(trees, states)
    integrations = discover_energy_integrations(devices, entities)
    circuit_roles = classify_circuits(trees, topologies)
    return build_energy_topology(trees, topologies, integrations, circuit_roles)


def build_topology_aware_config(topo: EnergyTopology) -> dict:
    """Convert topology role assignments to Energy Dashboard config dict."""
    energy_sources: list[dict] = []
    device_consumption: list[dict] = []

    # Collect preferred role assignments
    preferred = [a for a in topo.role_assignments if a.preferred]

    # Grid — aggregate import/export into one grid source
    grid_imports = [a for a in preferred if a.role == "grid_import"]
    grid_exports = [a for a in preferred if a.role == "grid_export"]
    if grid_imports or grid_exports:
        grid_source: dict = {"type": "grid", "flow_from": [], "flow_to": []}
        for a in grid_imports:
            grid_source["flow_from"].append({"stat_energy_from": a.entity_id})
        for a in grid_exports:
            grid_source["flow_to"].append({"stat_energy_to": a.entity_id})
        energy_sources.append(grid_source)

    # Solar
    solar_assignments = [a for a in preferred if a.role == "solar"]
    for a in solar_assignments:
        energy_sources.append({
            "type": "solar",
            "stat_energy_from": a.entity_id,
        })

    # Battery — aggregate charge/discharge into one battery source
    batt_discharge = [a for a in preferred if a.role == "battery_discharge"]
    batt_charge = [a for a in preferred if a.role == "battery_charge"]
    if batt_discharge or batt_charge:
        batt_source: dict = {"type": "battery"}
        if batt_discharge:
            batt_source["stat_energy_from"] = batt_discharge[0].entity_id
        if batt_charge:
            batt_source["stat_energy_to"] = batt_charge[0].entity_id
        energy_sources.append(batt_source)

    # Device consumption
    consumption_assignments = [a for a in preferred if a.role == "device_consumption"]
    for a in consumption_assignments:
        device_consumption.append({"stat_consumption": a.entity_id})

    return {
        "energy_sources": energy_sources,
        "device_consumption": device_consumption,
    }


def apply_topology_prefs(current: dict, topo: EnergyTopology) -> dict:
    """Apply topology decisions to current prefs: add wanted, remove unwanted.

    Unlike ``merge_prefs`` (additive-only), this replaces the SPAN-managed
    portion of the config while preserving non-SPAN entries the user configured.

    HA's ``energy/save_prefs`` rejects source objects missing optional fields
    (stat_cost, cost_adjustment_day, etc.), so existing source objects are
    preserved when their entity_ids match a preferred assignment.
    """
    result = copy.deepcopy(current)

    # Build sets from topology decisions
    preferred = [a for a in topo.role_assignments if a.preferred]
    skipped = [a for a in topo.role_assignments if not a.preferred]
    skipped_eids = {a.entity_id for a in skipped}
    wanted_consumption = {a.entity_id for a in preferred if a.role == "device_consumption"}
    wanted_source_eids = {a.entity_id for a in preferred if a.role != "device_consumption"}

    # --- Device consumption: keep wanted + non-SPAN user entries ---
    existing_consumption = result.get("device_consumption", [])
    keep_consumption = []
    for entry in existing_consumption:
        stat = entry.get("stat_consumption", "")
        if stat in wanted_consumption:
            keep_consumption.append(entry)
            wanted_consumption.discard(stat)  # mark as already present
        elif stat not in skipped_eids:
            # Not in wanted or skipped — user-configured entry, preserve it
            keep_consumption.append(entry)
    # Add new entries not yet present
    for stat in sorted(wanted_consumption):
        keep_consumption.append({"stat_consumption": stat})
    result["device_consumption"] = keep_consumption

    # --- Energy sources: filter + preserve existing objects ---
    existing_sources = result.get("energy_sources", [])
    keep_sources = []
    matched_preferred_eids: set[str] = set()

    for source in existing_sources:
        source_eids = _extract_source_entity_ids(source)
        # If ANY entity in this source is in the skipped set, remove the source
        if source_eids & skipped_eids:
            continue
        # If ALL entities in this source are in the wanted set, keep it as-is
        # (preserves extra fields like stat_cost, cost_adjustment_day)
        if source_eids and source_eids <= wanted_source_eids:
            keep_sources.append(source)
            matched_preferred_eids |= source_eids
            continue
        # Source has entities not in wanted or skipped — user-configured, keep
        keep_sources.append(source)

    # Add new sources for preferred entities not already matched
    proposed = build_topology_aware_config(topo)
    for source in proposed.get("energy_sources", []):
        source_eids = _extract_source_entity_ids(source)
        if not (source_eids <= matched_preferred_eids):
            keep_sources.append(source)
            matched_preferred_eids |= source_eids

    result["energy_sources"] = keep_sources
    return result


def _extract_source_entity_ids(source: dict) -> set[str]:
    """Extract all entity_ids from an energy source dict."""
    eids: set[str] = set()
    for flow in source.get("flow_from", []):
        if eid := flow.get("stat_energy_from"):
            eids.add(eid)
    for flow in source.get("flow_to", []):
        if eid := flow.get("stat_energy_to"):
            eids.add(eid)
    if eid := source.get("stat_energy_from"):
        eids.add(eid)
    if eid := source.get("stat_energy_to"):
        eids.add(eid)
    return eids


def _show_topology_diff(current: dict, cleaned: dict) -> None:
    """Show what the topology-aware config will change."""
    console.rule("[bold]Energy Dashboard Changes[/bold]")

    current_sources = current.get("energy_sources", [])
    cleaned_sources = cleaned.get("energy_sources", [])
    current_consumption = current.get("device_consumption", [])
    cleaned_consumption = cleaned.get("device_consumption", [])

    current_consumption_ids = {d.get("stat_consumption") for d in current_consumption}
    cleaned_consumption_ids = {d.get("stat_consumption") for d in cleaned_consumption}
    current_source_eids = set()
    for s in current_sources:
        current_source_eids |= _extract_source_entity_ids(s)
    cleaned_source_eids = set()
    for s in cleaned_sources:
        cleaned_source_eids |= _extract_source_entity_ids(s)

    added_consumption = cleaned_consumption_ids - current_consumption_ids
    removed_consumption = current_consumption_ids - cleaned_consumption_ids
    added_source_eids = cleaned_source_eids - current_source_eids
    removed_source_eids = current_source_eids - cleaned_source_eids

    if not added_consumption and not removed_consumption and not added_source_eids and not removed_source_eids:
        print_ok("No changes needed — energy dashboard is up to date")
        return

    if removed_consumption:
        print_info(f"Removing {len(removed_consumption)} device consumption entry/ies:")
        for eid in sorted(removed_consumption):
            console.print(f"  - {eid}")

    if added_consumption:
        print_info(f"Adding {len(added_consumption)} device consumption entry/ies:")
        for eid in sorted(added_consumption):
            console.print(f"  + {eid}")

    if removed_source_eids:
        print_info("Removing energy source entity/ies:")
        for eid in sorted(removed_source_eids):
            console.print(f"  - {eid}")

    if added_source_eids:
        print_info("Adding energy source entity/ies:")
        for eid in sorted(added_source_eids):
            console.print(f"  + {eid}")


@click.command("energy-topology")
@pass_ctx
def energy_topology(ctx: Context) -> None:
    """Show energy system topology and recommendations."""
    run_async(_energy_topology_show(ctx))


async def _energy_topology_show(ctx: Context) -> None:
    """Display-only topology view."""
    async with ctx.client() as client:
        devices, entities, areas = await fetch_registries(client)
        states = await fetch_entity_states(client)

    # Enrich entities with device_class/state_class from states
    enrich_entities_from_states(entities, states)
    trees = build_span_trees(devices, entities)

    if not trees:
        print_warn("No SPAN devices found")
        return

    topo = _build_topology(trees, devices, entities, states)
    render_topology(topo)


# ---------------------------------------------------------------------------
# Stale reference detection
# ---------------------------------------------------------------------------


def extract_energy_entity_ids(prefs: dict[str, Any]) -> set[str]:
    """Extract all entity_ids referenced in energy dashboard config."""
    ids: set[str] = set()
    for source in prefs.get("energy_sources", []):
        for flow in source.get("flow_from", []):
            if stat := flow.get("stat_energy_from"):
                ids.add(stat)
        for flow in source.get("flow_to", []):
            if stat := flow.get("stat_energy_to"):
                ids.add(stat)
        if stat := source.get("stat_energy_from"):
            ids.add(stat)
        if stat := source.get("stat_energy_to"):
            ids.add(stat)
    for device in prefs.get("device_consumption", []):
        if stat := device.get("stat_consumption"):
            ids.add(stat)
    return ids


def find_stale_references(
    prefs: dict[str, Any],
    all_entity_ids: set[str],
) -> dict[str, list[str]]:
    """Find energy dashboard references pointing to non-existent entities.

    Returns a dict mapping section name to list of stale entity_ids.
    """
    stale: dict[str, list[str]] = {}

    for source in prefs.get("energy_sources", []):
        stype = source.get("type", "unknown")
        for flow in source.get("flow_from", []):
            if (stat := flow.get("stat_energy_from")) and stat not in all_entity_ids:
                stale.setdefault(f"{stype} (grid import)", []).append(stat)
        for flow in source.get("flow_to", []):
            if (stat := flow.get("stat_energy_to")) and stat not in all_entity_ids:
                stale.setdefault(f"{stype} (grid export)", []).append(stat)
        if (stat := source.get("stat_energy_from")) and stat not in all_entity_ids:
            stale.setdefault(stype, []).append(stat)
        if (stat := source.get("stat_energy_to")) and stat not in all_entity_ids:
            stale.setdefault(stype, []).append(stat)

    for device in prefs.get("device_consumption", []):
        if (stat := device.get("stat_consumption")) and stat not in all_entity_ids:
            stale.setdefault("device_consumption", []).append(stat)

    return stale


def remove_stale_references(
    prefs: dict[str, Any],
    stale_ids: set[str],
) -> dict[str, Any]:
    """Return a copy of prefs with stale entity_id references removed."""
    cleaned = copy.deepcopy(prefs)

    # Clean energy_sources
    clean_sources = []
    for source in cleaned.get("energy_sources", []):
        # Filter flow lists (only if originally present)
        if "flow_from" in source:
            source["flow_from"] = [
                f for f in source["flow_from"]
                if f.get("stat_energy_from") not in stale_ids
            ]
            if not source["flow_from"]:
                del source["flow_from"]
        if "flow_to" in source:
            source["flow_to"] = [
                f for f in source["flow_to"]
                if f.get("stat_energy_to") not in stale_ids
            ]
            if not source["flow_to"]:
                del source["flow_to"]
        # Remove solar/battery sources whose primary stat is stale
        if source.get("stat_energy_from") in stale_ids:
            source.pop("stat_energy_from", None)
        if source.get("stat_energy_to") in stale_ids:
            source.pop("stat_energy_to", None)
        # Drop sources that have lost all entity references
        has_refs = (
            source.get("flow_from")
            or source.get("flow_to")
            or source.get("stat_energy_from")
            or source.get("stat_energy_to")
            or source.get("stat_cost")
            or source.get("stat_compensation")
        )
        if has_refs:
            clean_sources.append(source)
    cleaned["energy_sources"] = clean_sources

    # Clean device_consumption
    cleaned["device_consumption"] = [
        d for d in cleaned.get("device_consumption", [])
        if d.get("stat_consumption") not in stale_ids
    ]

    return cleaned


@click.command("energy-audit")
@click.option("--prune", is_flag=True, default=False, help="Remove stale entries from energy dashboard")
@pass_ctx
def energy_audit(ctx: Context, prune: bool) -> None:
    """Find stale/orphaned entity references in the Energy Dashboard."""
    run_async(_energy_audit(ctx, prune))


async def _energy_audit(ctx: Context, prune: bool) -> None:
    async with ctx.client() as client:
        prefs = await fetch_energy_prefs(client)
        raw_entities = await client.send_command("config/entity_registry/list")
        all_entity_ids = {e["entity_id"] for e in raw_entities}

    ed_refs = extract_energy_entity_ids(prefs)
    stale = find_stale_references(prefs, all_entity_ids)

    print_info(f"Energy dashboard references {len(ed_refs)} entity ID(s)")

    if not stale:
        print_ok("No stale references — all energy dashboard entities exist")
        return

    total = sum(len(v) for v in stale.values())
    print_warn(f"{total} stale reference(s) found:")
    for section, ids in sorted(stale.items()):
        console.print(f"\n  [bold]{section}[/bold]")
        for entity_id in sorted(ids):
            console.print(f"    - {entity_id}")
    console.print()

    if not prune:
        print_info("Run with --prune to remove stale entries")
        return

    if ctx.dry_run:
        print_dry_run(f"Would remove {total} stale reference(s)")
        return

    stale_ids = {eid for ids in stale.values() for eid in ids}
    cleaned = remove_stale_references(prefs, stale_ids)

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **cleaned)

    print_ok(f"Removed {total} stale reference(s) from energy dashboard")
