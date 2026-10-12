"""Energy commands — auto-configure and audit HA Energy Dashboard."""

from __future__ import annotations

import copy
import re
from typing import Any

import click

from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.energy_prefs import (
    UNIFIED,
    GridConnection,
    build_grid_sources,
    convert_sources,
    grid_connections,
    grid_export_ids,
    grid_format,
    grid_import_ids,
    is_grid,
    merge_grid_sources,
    power_config_ids,
    source_energy_ids,
    source_rate_ids,
)
from hass_atlas.models import HADevice, HAEntity, SpanDeviceTree
from hass_atlas.output import (
    console,
    print_dry_run,
    print_info,
    print_ok,
    print_warn,
    render_gaps,
    render_topology,
)
from hass_atlas.registry import (
    build_span_trees,
    enrich_entities_from_states,
    fetch_energy_prefs,
    fetch_entity_states,
    fetch_registries,
)
from hass_atlas.span_panel import (
    CIRCUIT_ROLE_HELP,
    CircuitRoleOverride,
    SpanPanelAdminRequired,
    build_energy_topology_span_panel,
    exit_writer_unsupported,
    fetch_panel_topologies,
    find_misplaced_consumers,
    resolve_circuit_roles,
    resolve_ebus_circuit_roles,
    span_panel_only,
)
from hass_atlas.topology import (
    EnergyRole,
    EnergyTopology,
    build_energy_topology,
    classify_circuits,
    discover_energy_integrations,
    extract_span_topology,
)


@click.command()
@click.option("--topology", is_flag=True, default=False,
              help="Use topology-aware configuration (detects overlaps with other integrations)")
@click.option(
    "--circuit-role", "circuit_role", multiple=True, metavar="[SERIAL:]CIRCUIT=ROLE",
    help=CIRCUIT_ROLE_HELP,
)
@pass_ctx
def energy(ctx: Context, topology: bool, circuit_role: tuple[str, ...]) -> None:
    """Auto-configure the Energy Dashboard for SPAN devices."""
    if topology:
        run_async(_energy_topology_config(ctx, circuit_role))
    else:
        run_async(_energy(ctx, circuit_role))


async def _energy(ctx: Context, circuit_role: tuple[str, ...] = ()) -> None:
    """Add the topology's preferred assignments to the dashboard, never removing any."""
    async with ctx.client() as client:
        devices, entities, _ = await fetch_registries(client)
        states = await fetch_entity_states(client)
        current_prefs = await fetch_energy_prefs(client)
        ha_version = getattr(client, "ha_version", None)

    enrich_entities_from_states(entities, states)
    trees = build_span_trees(devices, entities)

    if not trees:
        if span_panel_only(devices):
            exit_writer_unsupported("energy")
        print_warn("No SPAN devices found")
        return

    roles = resolve_ebus_circuit_roles(circuit_role, trees)
    topo = _build_topology(trees, devices, entities, states, roles)
    fmt = grid_format(ha_version, current_prefs)
    proposed = build_topology_aware_config(topo, fmt)
    merged = merge_prefs(current_prefs, proposed, fmt)

    _show_topology_diff(current_prefs, merged)
    _warn_skipped_present(merged, topo)
    for warning in topo.warnings:
        print_warn(warning)
    render_gaps(topo.gaps)

    if ctx.dry_run:
        print_dry_run("Would save energy dashboard config (use without --dry-run to apply)")
        return

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **merged)

    print_ok("Energy dashboard config saved")


async def _energy_topology_config(ctx: Context, circuit_role: tuple[str, ...] = ()) -> None:
    """Topology-aware energy dashboard configuration."""
    async with ctx.client() as client:
        devices, entities, areas = await fetch_registries(client)
        states = await fetch_entity_states(client)
        current_prefs = await fetch_energy_prefs(client)
        ha_version = getattr(client, "ha_version", None)

    # Enrich entities with device_class/state_class from states
    # (entity registry doesn't include these — they're runtime properties)
    enrich_entities_from_states(entities, states)
    trees = build_span_trees(devices, entities)

    if not trees:
        if span_panel_only(devices):
            exit_writer_unsupported("energy --topology")
        print_warn("No SPAN devices found")
        return

    roles = resolve_ebus_circuit_roles(circuit_role, trees)
    topo = _build_topology(trees, devices, entities, states, roles)
    render_topology(topo)
    render_gaps(topo.gaps)

    cleaned = apply_topology_prefs(current_prefs, topo, grid_format(ha_version, current_prefs))

    _show_topology_diff(current_prefs, cleaned)

    if ctx.dry_run:
        print_dry_run("Would save topology-aware energy dashboard config (use without --dry-run to apply)")
        return

    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **cleaned)

    print_ok("Topology-aware energy dashboard config saved")


def build_energy_config(
    trees: list[SpanDeviceTree],
    states: dict[str, Any] | None = None,
    devices: list[HADevice] | None = None,
    entities: list[HAEntity] | None = None,
    fmt: str = UNIFIED,
) -> dict:
    """Build proposed energy dashboard config from SPAN device trees.

    Uses the same topology assignments as ``energy --topology``.
    """
    topo = _build_topology(trees, devices or [], entities or [], states or {})
    return build_topology_aware_config(topo, fmt)


def merge_prefs(current: dict, proposed: dict, fmt: str | None = None) -> dict:
    """Merge proposed config into current, only adding missing entries.

    Grid sources are written in ``fmt`` (by default, the format ``current``
    uses); a proposed grid statistic already present is not added again.
    """
    fmt = fmt or grid_format(None, current)
    merged = copy.deepcopy(current)

    existing_sources = convert_sources(merged.get("energy_sources", []) or [], fmt)
    proposed_sources = proposed.get("energy_sources", []) or []
    wanted_grid = [c for s in proposed_sources for c in grid_connections(s)]
    grid_block = merge_grid_sources(
        [s for s in existing_sources if is_grid(s)], wanted_grid, set(), fmt
    )

    # Merge energy_sources — add sources not already present (by type + entity_id)
    sources: list[dict] = []
    grid_placed = False
    for source in existing_sources:
        if is_grid(source):
            if not grid_placed:
                sources.extend(grid_block)
                grid_placed = True
            continue
        sources.append(source)
    if not grid_placed:
        sources[:0] = grid_block
    existing_source_keys = _source_keys(sources)
    for source in proposed_sources:
        if is_grid(source):
            continue
        key = _source_key(source)
        if key not in existing_source_keys:
            sources.append(source)
            existing_source_keys.add(key)
    merged["energy_sources"] = sources

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
        from_ids = sorted(grid_import_ids(source))
        to_ids = sorted(grid_export_ids(source))
        return f"grid:{','.join(from_ids)}:{','.join(to_ids)}"
    elif stype == "solar":
        return f"solar:{source.get('stat_energy_from', '')}"
    elif stype == "battery":
        return f"battery:{source.get('stat_energy_from', '')}:{source.get('stat_energy_to', '')}"
    return f"{stype}:{id(source)}"


def _source_keys(sources: list[dict]) -> set[str]:
    return {_source_key(s) for s in sources}


def _warn_skipped_present(prefs: dict, topo: EnergyTopology) -> None:
    """Warn about entries the topology skips that the additive merge leaves in place."""
    present = extract_energy_entity_ids(prefs)
    skipped = sorted(
        {a.entity_id for a in topo.role_assignments if not a.preferred} & present
    )
    if skipped:
        print_warn(
            f"{len(skipped)} dashboard reference(s) that energy --topology would remove are "
            "kept (plain energy only adds):"
        )
        for eid in skipped:
            console.print(f"  - {eid}")


# ---------------------------------------------------------------------------
# Topology-aware configuration
# ---------------------------------------------------------------------------


def _build_topology(
    trees: list[SpanDeviceTree],
    devices: list[HADevice],
    entities: list[HAEntity],
    states: dict[str, Any],
    roles: dict[tuple[str, str], CircuitRoleOverride] | None = None,
) -> EnergyTopology:
    """Build complete energy topology from SPAN trees, registries, and entity states."""
    topologies = extract_span_topology(trees, states)
    integrations = discover_energy_integrations(devices, entities)
    circuit_roles = classify_circuits(trees, topologies, states)
    return build_energy_topology(
        trees, topologies, integrations, circuit_roles, states=states, roles=roles
    )


def _grid_connections(assignments: list[EnergyRole]) -> list[GridConnection]:
    """Pair preferred grid import and export assignments into grid connections.

    Assignments with the same ``grid_connection`` key (else the same platform)
    pair by order; each connection takes the first power sensor not already
    used by another, since HA rejects a power sensor shared by two grid sources.
    """
    order: list[str] = []
    imports: dict[str, list[EnergyRole]] = {}
    exports: dict[str, list[EnergyRole]] = {}
    for a in assignments:
        if not a.preferred or a.role not in ("grid_import", "grid_export"):
            continue
        key = a.grid_connection or a.platform
        if key not in imports:
            order.append(key)
            imports[key], exports[key] = [], []
        (imports if a.role == "grid_import" else exports)[key].append(a)
    connections: list[GridConnection] = []
    used_rates: set[str] = set()
    for key in order:
        imp, exp = imports[key], exports[key]
        for i in range(max(len(imp), len(exp))):
            members = [x[i] for x in (imp, exp) if i < len(x)]
            rate = next(
                (m.rate_entity_id for m in members
                 if m.rate_entity_id and m.rate_entity_id not in used_rates),
                None,
            )
            if rate:
                used_rates.add(rate)
            connections.append(GridConnection(
                stat_energy_from=imp[i].entity_id if i < len(imp) else None,
                stat_energy_to=exp[i].entity_id if i < len(exp) else None,
                stat_rate=rate,
            ))
    return connections


def build_topology_aware_config(topo: EnergyTopology, fmt: str = UNIFIED) -> dict:
    """Convert topology role assignments to Energy Dashboard config dict.

    Grid sources are written in ``fmt``: ``UNIFIED`` (one source per grid
    connection, HA 2026.3 and later) or ``LEGACY`` (one ``flow_from`` /
    ``flow_to`` source).
    """
    energy_sources: list[dict] = []
    device_consumption: list[dict] = []

    # Collect preferred role assignments
    preferred = [a for a in topo.role_assignments if a.preferred]

    # Grid
    energy_sources.extend(build_grid_sources(_grid_connections(preferred), fmt))

    # Solar
    solar_assignments = [a for a in preferred if a.role == "solar"]
    for a in solar_assignments:
        solar_source: dict = {
            "type": "solar",
            "stat_energy_from": a.entity_id,
        }
        if a.rate_entity_id:
            solar_source["stat_rate"] = a.rate_entity_id
        energy_sources.append(solar_source)

    # Battery — aggregate charge/discharge into one battery source
    batt_discharge = [a for a in preferred if a.role == "battery_discharge"]
    batt_charge = [a for a in preferred if a.role == "battery_charge"]
    if batt_discharge or batt_charge:
        batt_source: dict = {"type": "battery"}
        if batt_discharge:
            batt_source["stat_energy_from"] = batt_discharge[0].entity_id
        if batt_charge:
            batt_source["stat_energy_to"] = batt_charge[0].entity_id
        # Use discharge power sensor for battery stat_rate (positive=discharge, negative=charge)
        batt_rate = next(
            (a.rate_entity_id for a in (batt_discharge + batt_charge) if a.rate_entity_id),
            None,
        )
        if batt_rate:
            batt_source["stat_rate"] = batt_rate
        energy_sources.append(batt_source)

    # Device consumption
    consumption_assignments = [a for a in preferred if a.role == "device_consumption"]
    for a in consumption_assignments:
        entry: dict[str, str] = {"stat_consumption": a.entity_id}
        if a.parent_entity_id:
            entry["included_in_stat"] = a.parent_entity_id
        if a.rate_entity_id:
            entry["stat_rate"] = a.rate_entity_id
        device_consumption.append(entry)

    return {
        "energy_sources": energy_sources,
        "device_consumption": device_consumption,
    }


def apply_topology_prefs(
    current: dict, topo: EnergyTopology, fmt: str | None = None
) -> dict:
    """Apply topology decisions to current prefs: add wanted, remove unwanted.

    Unlike ``merge_prefs`` (additive-only), this replaces the SPAN-managed
    portion of the config while preserving non-SPAN entries the user configured.

    Existing source objects are preserved when their entity_ids match a
    preferred assignment, so their cost fields survive.  Grid sources are
    written in ``fmt`` (by default, the format ``current`` uses).
    """
    fmt = fmt or grid_format(None, current)
    result = copy.deepcopy(current)

    # Build sets from topology decisions
    preferred = [a for a in topo.role_assignments if a.preferred]
    skipped = [a for a in topo.role_assignments if not a.preferred]
    skipped_eids = {a.entity_id for a in skipped}
    wanted_consumption = {a.entity_id for a in preferred if a.role == "device_consumption"}
    wanted_source_eids = {a.entity_id for a in preferred if a.role != "device_consumption"}

    # Build consumption metadata mappings from topology
    consumption_parents: dict[str, str] = {
        a.entity_id: a.parent_entity_id
        for a in preferred
        if a.role == "device_consumption" and a.parent_entity_id
    }
    consumption_rates: dict[str, str] = {
        a.entity_id: a.rate_entity_id
        for a in preferred
        if a.role == "device_consumption" and a.rate_entity_id
    }

    # --- Device consumption: keep wanted + non-SPAN user entries ---
    existing_consumption = result.get("device_consumption", [])
    keep_consumption = []
    seen_stats: set[str] = set()
    for entry in existing_consumption:
        stat = entry.get("stat_consumption", "")
        if stat in seen_stats:
            continue  # dedupe pre-existing duplicates (e.g. from prior --remap)
        if stat in wanted_consumption:
            updated = copy.deepcopy(entry)
            if stat in consumption_parents:
                updated["included_in_stat"] = consumption_parents[stat]
            if stat in consumption_rates:
                updated["stat_rate"] = consumption_rates[stat]
            keep_consumption.append(updated)
            wanted_consumption.discard(stat)  # mark as already present
            seen_stats.add(stat)
        elif stat not in skipped_eids:
            # Not in wanted or skipped — user-configured entry, preserve it
            keep_consumption.append(entry)
            if stat:
                seen_stats.add(stat)
    # Add new entries not yet present
    for stat in sorted(wanted_consumption):
        new_entry: dict[str, str] = {"stat_consumption": stat}
        if stat in consumption_parents:
            new_entry["included_in_stat"] = consumption_parents[stat]
        if stat in consumption_rates:
            new_entry["stat_rate"] = consumption_rates[stat]
        keep_consumption.append(new_entry)
    result["device_consumption"] = keep_consumption

    # --- Energy sources: filter + preserve existing objects ---
    existing_sources = convert_sources(result.get("energy_sources", []) or [], fmt)
    keep_sources: list[dict] = []
    matched_preferred_eids: set[str] = set()
    grid_block = merge_grid_sources(
        [s for s in existing_sources if is_grid(s)],
        _grid_connections(preferred),
        skipped_eids,
        fmt,
    )
    grid_placed = False

    # Build proposed config to get updated stat_rate values
    proposed = build_topology_aware_config(topo, fmt)
    # Map source key → proposed stat_rate for updating existing sources
    proposed_source_rates: dict[str, str | None] = {}
    for source in proposed.get("energy_sources", []):
        key = _source_key(source)
        proposed_source_rates[key] = source.get("stat_rate")

    for source in existing_sources:
        if is_grid(source):
            if not grid_placed:
                keep_sources.extend(grid_block)
                grid_placed = True
            continue
        source_eids = _extract_source_entity_ids(source)
        # If ANY entity in this source is in the skipped set, remove the source
        if source_eids & skipped_eids:
            continue
        # If ALL entities in this source are in the wanted set, keep it
        # (preserves extra fields like stat_cost, cost_adjustment_day)
        # but update stat_rate if the topology proposes a different one.
        if source_eids and source_eids <= wanted_source_eids:
            key = _source_key(source)
            new_rate = proposed_source_rates.get(key)
            if new_rate and source.get("stat_rate") != new_rate:
                source = copy.deepcopy(source)
                source["stat_rate"] = new_rate
            keep_sources.append(source)
            matched_preferred_eids |= source_eids
            continue
        # Source has entities not in wanted or skipped — user-configured, keep
        keep_sources.append(source)

    if not grid_placed:
        keep_sources.extend(grid_block)

    # Add new sources for preferred entities not already matched
    for source in proposed.get("energy_sources", []):
        if is_grid(source):
            continue
        source_eids = _extract_source_entity_ids(source)
        if not (source_eids <= matched_preferred_eids):
            keep_sources.append(source)
            matched_preferred_eids |= source_eids

    result["energy_sources"] = keep_sources
    return result


def _extract_source_entity_ids(source: dict) -> set[str]:
    """Extract the energy entity_ids of an energy source, in either grid format."""
    return source_energy_ids(source)


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

    # Detect metadata updates on existing consumption entries (included_in_stat, stat_rate)
    current_consumption_map = {d.get("stat_consumption"): d for d in current_consumption}
    cleaned_consumption_map = {d.get("stat_consumption"): d for d in cleaned_consumption}
    updated_consumption: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for stat in current_consumption_ids & cleaned_consumption_ids:
        old_entry = current_consumption_map[stat]
        new_entry = cleaned_consumption_map[stat]
        diffs: dict[str, tuple[str | None, str | None]] = {}
        for field in ("included_in_stat", "stat_rate"):
            old_val = old_entry.get(field)
            new_val = new_entry.get(field)
            if old_val != new_val:
                diffs[field] = (old_val, new_val)
        if diffs:
            updated_consumption[stat] = diffs

    # Detect metadata updates on existing energy sources (stat_rate)
    current_source_map = {_source_key(s): s for s in current_sources}
    cleaned_source_map = {_source_key(s): s for s in cleaned_sources}
    updated_sources: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for key in set(current_source_map) & set(cleaned_source_map):
        old_src = current_source_map[key]
        new_src = cleaned_source_map[key]
        source_diffs: dict[str, tuple[str | None, str | None]] = {}
        for field in ("stat_rate",):
            old_val = old_src.get(field)
            new_val = new_src.get(field)
            if old_val != new_val:
                source_diffs[field] = (old_val, new_val)
        if source_diffs:
            updated_sources[key] = source_diffs

    if not added_consumption and not removed_consumption and not added_source_eids and not removed_source_eids and not updated_consumption and not updated_sources:
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

    if updated_consumption:
        print_info(f"Updating {len(updated_consumption)} device consumption entry/ies:")
        for stat in sorted(updated_consumption):
            for field, (old_val, new_val) in updated_consumption[stat].items():
                old_disp = old_val or "(none)"
                new_disp = new_val or "(none)"
                console.print(f"  ~ {stat}: {field} {old_disp} → {new_disp}")

    if updated_sources:
        print_info(f"Updating {len(updated_sources)} energy source(s):")
        for key in sorted(updated_sources):
            for field, (old_val, new_val) in updated_sources[key].items():
                old_disp = old_val or "(none)"
                new_disp = new_val or "(none)"
                console.print(f"  ~ {key}: {field} {old_disp} → {new_disp}")

    if removed_source_eids:
        print_info("Removing energy source entity/ies:")
        for eid in sorted(removed_source_eids):
            console.print(f"  - {eid}")

    if added_source_eids:
        print_info("Adding energy source entity/ies:")
        for eid in sorted(added_source_eids):
            console.print(f"  + {eid}")


@click.command("energy-topology")
@click.option(
    "--circuit-role", "circuit_role", multiple=True, metavar="[SERIAL:]CIRCUIT=ROLE",
    help=CIRCUIT_ROLE_HELP,
)
@pass_ctx
def energy_topology(ctx: Context, circuit_role: tuple[str, ...]) -> None:
    """Show energy system topology and recommendations."""
    run_async(_energy_topology_show(ctx, circuit_role))


async def _energy_topology_show(ctx: Context, circuit_role: tuple[str, ...] = ()) -> None:
    """Display-only topology view."""
    async with ctx.client() as client:
        devices, entities, areas = await fetch_registries(client)
        states = await fetch_entity_states(client)
        panels = None
        if span_panel_only(devices):
            panels = await fetch_panel_topologies(client, devices, entities)

    # Enrich entities with device_class/state_class from states
    enrich_entities_from_states(entities, states)

    if panels is not None:
        if not panels:
            print_warn("No span_panel panels found")
            return
        roles = resolve_circuit_roles(circuit_role, panels)
        integrations = discover_energy_integrations(
            devices, entities, exclude_platform="span_panel"
        )
        sp_topo, gaps = build_energy_topology_span_panel(panels, states, integrations, roles)
        render_topology(sp_topo, span_panels=panels, states=states)
        render_gaps(gaps)
        return

    trees = build_span_trees(devices, entities)

    if not trees:
        print_warn("No SPAN devices found")
        return

    roles = resolve_ebus_circuit_roles(circuit_role, trees)
    topo = _build_topology(trees, devices, entities, states, roles)
    render_topology(topo)
    render_gaps(topo.gaps)


# ---------------------------------------------------------------------------
# Stale reference detection
# ---------------------------------------------------------------------------


def extract_energy_entity_ids(prefs: dict[str, Any]) -> set[str]:
    """Extract all entity_ids referenced in energy dashboard config.

    Covers source ``stat_energy_from`` / ``stat_energy_to`` / ``stat_rate`` /
    ``power_config`` (and the legacy grid ``flow_from`` / ``flow_to`` / ``power``
    variants) and device_consumption ``stat_consumption`` / ``included_in_stat`` /
    ``stat_rate``.
    """
    ids: set[str] = set()
    for source in prefs.get("energy_sources", []):
        ids |= source_energy_ids(source)
        ids.update(source_rate_ids(source))
    for device in prefs.get("device_consumption", []):
        for k in ("stat_consumption", "included_in_stat", "stat_rate"):
            if stat := device.get(k):
                ids.add(stat)
    return ids


def find_stale_references(
    prefs: dict[str, Any],
    all_entity_ids: set[str],
) -> dict[str, list[str]]:
    """Find energy dashboard references pointing to non-existent entities.

    Returns a dict mapping section name to list of stale entity_ids.  Covers
    ``stat_energy_from`` / ``stat_energy_to`` on sources (grid sources in either
    format), ``stat_consumption`` / ``included_in_stat`` / ``stat_rate`` on
    device_consumption entries, and power sensors on sources.
    """
    stale: dict[str, list[str]] = {}

    for source in prefs.get("energy_sources", []):
        stype = source.get("type", "unknown")
        if stype == "grid":
            for stat in grid_import_ids(source):
                if stat not in all_entity_ids:
                    stale.setdefault(f"{stype} (grid import)", []).append(stat)
            for stat in grid_export_ids(source):
                if stat not in all_entity_ids:
                    stale.setdefault(f"{stype} (grid export)", []).append(stat)
        else:
            if (stat := source.get("stat_energy_from")) and stat not in all_entity_ids:
                stale.setdefault(stype, []).append(stat)
            if (stat := source.get("stat_energy_to")) and stat not in all_entity_ids:
                stale.setdefault(stype, []).append(stat)
        for stat in source_rate_ids(source):
            if stat not in all_entity_ids:
                stale.setdefault(f"{stype} (stat_rate)", []).append(stat)

    for device in prefs.get("device_consumption", []):
        if (stat := device.get("stat_consumption")) and stat not in all_entity_ids:
            stale.setdefault("device_consumption", []).append(stat)
        if (stat := device.get("included_in_stat")) and stat not in all_entity_ids:
            stale.setdefault("device_consumption (included_in_stat)", []).append(stat)
        if (stat := device.get("stat_rate")) and stat not in all_entity_ids:
            stale.setdefault("device_consumption (stat_rate)", []).append(stat)

    return stale


def remap_stale_references(
    stale_ids: set[str],
    all_entity_ids: set[str],
    trees: list[SpanDeviceTree],
) -> tuple[dict[str, str], list[str]]:
    """Find live replacements for stale Energy Dashboard entity references.

    Two replacement strategies are tried in order:

    1. **``_2`` sibling.** When ``sensor.foo_energy`` no longer exists but
       ``sensor.foo_energy_2`` does, HA picked up the orphan slug for the
       new entity at integration registration time.  Map the old reference
       to the new entity.
    2. **Panel-device upstream → Lugs device.** Old flat-model entity_ids
       like ``sensor.span_panel_nt_2026_c192x_upstream_imported_energy``
       are matched against the live Lugs device on the corresponding panel
       and remapped to the equivalent ``sensor.c192x_*lugs_imported_energy``.

    Returns ``(remap, drop)`` where ``remap`` maps stale → live and ``drop``
    lists stale entity_ids that no live equivalent could be found for.
    """
    remap: dict[str, str] = {}
    drop: list[str] = []

    for stale_id in sorted(stale_ids):
        v2 = stale_id + "_2"
        if v2 in all_entity_ids:
            remap[stale_id] = v2
            continue
        replacement = _remap_panel_upstream(stale_id, trees)
        if replacement and replacement in all_entity_ids:
            remap[stale_id] = replacement
            continue
        drop.append(stale_id)

    return remap, drop


def _remap_panel_upstream(stale_id: str, trees: list[SpanDeviceTree]) -> str | None:
    """Map an old flat-model panel-upstream entity_id to the new Lugs entity_id.

    Flat-model entity_id format (from span-hass 0.1.x):
        sensor.span_panel_{serial_with_underscores}_upstream_{prop}

    where ``{prop}`` is ``imported_energy``, ``exported_energy``, or
    ``active_power``.  The new home is the corresponding meter property on
    the panel's upstream-Lugs HA device.
    """
    m = re.match(
        r"^sensor\.span_panel_(?P<serial>nt_\d+_\w+)_upstream_(?P<prop>\w+)$",
        stale_id,
    )
    if not m:
        return None
    serial_with_underscores = m.group("serial")
    prop = m.group("prop")
    panel_serial = serial_with_underscores.replace("_", "-")  # nt_2026_c192x → nt-2026-c192x

    suffix_by_prop = {
        "imported_energy": "_meter_imported-energy",
        "exported_energy": "_meter_exported-energy",
        "active_power": "_meter_active-power",
    }
    uid_suffix = suffix_by_prop.get(prop)
    if not uid_suffix:
        return None

    for tree in trees:
        if tree.serial != panel_serial or tree.lugs_up is None:
            continue
        for e in tree.lugs_up.entities:
            if e.unique_id.endswith(uid_suffix) and not e.disabled_by:
                return e.entity_id
    return None


def apply_remap_to_prefs(prefs: dict[str, Any], remap: dict[str, str]) -> dict[str, Any]:
    """Rewrite all stale entity references in energy prefs using the remap dict.

    Covers the same reference fields as ``find_stale_references``.  After
    remapping, device_consumption is deduplicated by ``stat_consumption`` —
    if remapping caused two entries to point at the same stat, the first
    wins (preserving its ``included_in_stat`` / ``stat_rate``).
    """
    cleaned = copy.deepcopy(prefs)
    for source in cleaned.get("energy_sources", []):
        for flow in source.get("flow_from", []):
            if (e := flow.get("stat_energy_from")) in remap:
                flow["stat_energy_from"] = remap[e]
        for flow in source.get("flow_to", []):
            if (e := flow.get("stat_energy_to")) in remap:
                flow["stat_energy_to"] = remap[e]
        for k in ("stat_energy_from", "stat_energy_to", "stat_rate"):
            if (e := source.get(k)) and e in remap:
                source[k] = remap[e]
        for holder in [source, *source.get("power", [])]:
            if "stat_rate" in holder and holder["stat_rate"] in remap:
                holder["stat_rate"] = remap[holder["stat_rate"]]
            config = holder.get("power_config") or {}
            for k, e in list(config.items()):
                if e in remap:
                    config[k] = remap[e]
    for device in cleaned.get("device_consumption", []):
        for k in ("stat_consumption", "included_in_stat", "stat_rate"):
            if (e := device.get(k)) and e in remap:
                device[k] = remap[e]

    # Dedupe device_consumption — HA's Sankey rejects duplicate stat_consumption.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for device in cleaned.get("device_consumption", []):
        stat = device.get("stat_consumption")
        if stat and stat in seen:
            continue
        if stat:
            seen.add(stat)
        deduped.append(device)
    cleaned["device_consumption"] = deduped

    return cleaned


def remove_stale_references(
    prefs: dict[str, Any],
    stale_ids: set[str],
) -> dict[str, Any]:
    """Return a copy of prefs with stale entity_id references removed."""
    cleaned = copy.deepcopy(prefs)

    # Clean energy_sources
    clean_sources = []
    for source in cleaned.get("energy_sources", []):
        legacy_grid = "flow_from" in source or "flow_to" in source
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
        if "power" in source:
            source["power"] = [
                p for p in source["power"]
                if p.get("stat_rate") not in stale_ids
                and not set(power_config_ids(p.get("power_config"))) & stale_ids
            ]
            if not source["power"]:
                del source["power"]
        # Remove solar/battery sources whose primary stat is stale; a unified
        # grid source keeps the key with no statistic.
        for k in ("stat_energy_from", "stat_energy_to"):
            if source.get(k) in stale_ids:
                if source.get("type") == "grid":
                    source[k] = None
                else:
                    source.pop(k, None)
        # Power sensors are advisory: clear instead of drop the source.  HA
        # derives stat_rate from power_config, so both go together.
        if set(power_config_ids(source.get("power_config"))) & stale_ids:
            source.pop("power_config", None)
            source.pop("stat_rate", None)
        if source.get("stat_rate") in stale_ids:
            source.pop("stat_rate", None)
        # Drop sources that have lost all entity references
        if source.get("type") == "grid" and legacy_grid:
            if not source.get("flow_from") and not source.get("flow_to"):
                if not source.get("power"):
                    continue
                # A legacy grid source requires both lists, even when empty.
            source.setdefault("flow_from", [])
            source.setdefault("flow_to", [])
            has_refs = True
        elif source.get("type") == "grid":
            has_refs = bool(
                source.get("stat_energy_from")
                or source.get("stat_energy_to")
                or source.get("stat_rate")
                or source.get("power_config")
            )
        else:
            has_refs = bool(
                source.get("stat_energy_from")
                or source.get("stat_energy_to")
                or source.get("stat_cost")
                or source.get("stat_compensation")
            )
        if has_refs:
            clean_sources.append(source)
    cleaned["energy_sources"] = clean_sources

    # Clean device_consumption — drop entries whose stat_consumption is stale,
    # and clear (but don't drop the entry on) stale included_in_stat / stat_rate.
    clean_consumption = []
    for d in cleaned.get("device_consumption", []):
        if d.get("stat_consumption") in stale_ids:
            continue
        if d.get("included_in_stat") in stale_ids:
            d.pop("included_in_stat", None)
        if d.get("stat_rate") in stale_ids:
            d.pop("stat_rate", None)
        clean_consumption.append(d)
    cleaned["device_consumption"] = clean_consumption

    return cleaned


@click.command("energy-audit")
@click.option("--prune", is_flag=True, default=False, help="Drop stale references")
@click.option(
    "--remap", is_flag=True, default=False,
    help="Rewrite stale references to live equivalents where possible "
    "(falls back to dropping refs with no match — implies --prune semantics for unmatchable refs)",
)
@click.option(
    "--circuit-role", "circuit_role", multiple=True, metavar="[SERIAL:]CIRCUIT=ROLE",
    help=CIRCUIT_ROLE_HELP,
)
@pass_ctx
def energy_audit(
    ctx: Context, prune: bool, remap: bool, circuit_role: tuple[str, ...]
) -> None:
    """Find (and optionally fix) stale entity references in the Energy Dashboard."""
    run_async(_energy_audit(ctx, prune, remap, circuit_role))


async def _energy_audit(
    ctx: Context, prune: bool, remap: bool, circuit_role: tuple[str, ...] = ()
) -> None:
    async with ctx.client() as client:
        prefs = await fetch_energy_prefs(client)
        devices, entities, _ = await fetch_registries(client)
        panels = None
        is_span_panel = span_panel_only(devices)
        if is_span_panel:
            if remap:
                exit_writer_unsupported("energy-audit --remap")
            try:
                panels = await fetch_panel_topologies(client, devices, entities)
            except SpanPanelAdminRequired as exc:
                # Stale-reference detection and --prune do not need the payload.
                print_warn(f"Skipping the span_panel misplaced-consumer check: {exc}")
    all_entity_ids = {e.entity_id for e in entities}

    ed_refs = extract_energy_entity_ids(prefs)
    stale = find_stale_references(prefs, all_entity_ids)

    print_info(f"Energy dashboard references {len(ed_refs)} entity ID(s)")

    if panels is not None:
        roles = resolve_circuit_roles(circuit_role, panels)
        misplaced = find_misplaced_consumers(panels, prefs, roles)
        if misplaced:
            print_warn(f"{len(misplaced)} span_panel circuit(s) misplaced as device consumers:")
            render_gaps(misplaced)
            console.print()
        else:
            print_ok("No span_panel circuit is misplaced as a device consumer")
    elif circuit_role and not is_span_panel:
        print_warn("--circuit-role applies to energy-audit only on span_panel panels; ignored")

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

    if not prune and not remap:
        print_info("Run with --remap to rewrite stale references to live equivalents,")
        print_info("or --prune to drop them.")
        return

    stale_ids = {eid for ids in stale.values() for eid in ids}

    if remap:
        trees = build_span_trees(devices, entities)
        replacements, unmatchable = remap_stale_references(stale_ids, all_entity_ids, trees)

        if replacements:
            console.print()
            print_info(f"Would rewrite {len(replacements)} reference(s):")
            for old, new in sorted(replacements.items()):
                console.print(f"  - {old}")
                console.print(f"    → {new}")
        if unmatchable:
            console.print()
            print_warn(f"Would drop {len(unmatchable)} unmatchable reference(s):")
            for old in unmatchable:
                console.print(f"  - {old}")

        if ctx.dry_run:
            print_dry_run("Run without --dry-run to apply")
            return

        cleaned = apply_remap_to_prefs(prefs, replacements)
        cleaned = remove_stale_references(cleaned, set(unmatchable))
        async with ctx.client() as client:
            await client.send_command("energy/save_prefs", **cleaned)
        print_ok(
            f"Rewrote {len(replacements)} reference(s); dropped {len(unmatchable)} unmatchable"
        )
        return

    # --prune (no remap)
    if ctx.dry_run:
        print_dry_run(f"Would drop {total} stale reference(s)")
        return

    cleaned = remove_stale_references(prefs, stale_ids)
    async with ctx.client() as client:
        await client.send_command("energy/save_prefs", **cleaned)
    print_ok(f"Dropped {total} stale reference(s) from energy dashboard")
