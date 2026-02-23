"""Normalize command — fix entity IDs to match current device names."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import click

from ha_atlas.context import Context, pass_ctx, run_async
from ha_atlas.ha_client import HAClient
from ha_atlas.models import HADevice, HAEntity
from ha_atlas.output import console, print_dry_run, print_error, print_info, print_ok, print_warn
from ha_atlas.registry import fetch_energy_prefs, fetch_span_trees


def slugify(text: str) -> str:
    """Replicate HA's slugify: NFD normalize, ASCII fold, lowercase, non-alnum → underscore."""
    text = unicodedata.normalize("NFD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "_", text.lower())
    return text.strip("_")


def _expected_entity_id(domain: str, device: HADevice, entity: HAEntity) -> str:
    """Compute the entity_id that HA would generate for a has_entity_name entity."""
    device_name = device.name_by_user or device.name or ""
    entity_name = entity.original_name or ""
    if entity_name:
        object_id = slugify(f"{device_name} {entity_name}")
    else:
        object_id = slugify(device_name)
    return f"{domain}.{object_id}"


def _compute_renames(
    trees: list,
    all_entity_ids: set[str],
) -> list[tuple[HADevice, HAEntity, str]]:
    """Compute (device, entity, new_entity_id) for entities needing rename.

    Returns only entities whose current entity_id differs from the expected one.
    Skips renames that would collide with existing entity_ids outside the rename set.
    """
    # First pass: collect all proposed renames
    proposed: list[tuple[HADevice, HAEntity, str]] = []
    for tree in trees:
        for device in tree.circuits + tree.all_child_devices:
            for entity in device.entities:
                if not entity.has_entity_name:
                    continue
                domain = entity.entity_id.split(".")[0]
                expected = _expected_entity_id(domain, device, entity)
                if expected != entity.entity_id:
                    proposed.append((device, entity, expected))

    # Build set of entity_ids being vacated (current IDs of entities we're renaming)
    vacating = {entity.entity_id for _, entity, _ in proposed}

    # Build set of entity_ids being claimed by renames
    claiming: dict[str, list[tuple[HADevice, HAEntity]]] = {}
    for device, entity, new_id in proposed:
        claiming.setdefault(new_id, []).append((device, entity))

    # Check for collisions
    renames: list[tuple[HADevice, HAEntity, str]] = []
    for device, entity, new_id in proposed:
        # Collision with an existing entity that's NOT being renamed away
        if new_id in all_entity_ids and new_id not in vacating:
            print_warn(
                f"Skip {entity.entity_id} → {new_id} (collides with existing entity)"
            )
            continue
        # Collision within our own rename set (duplicate device/entity names)
        if len(claiming.get(new_id, [])) > 1:
            print_warn(
                f"Skip {entity.entity_id} → {new_id} (multiple entities claim same ID)"
            )
            continue
        renames.append((device, entity, new_id))

    return renames


async def _apply_renames(
    client: HAClient, renames: list[tuple[HADevice, HAEntity, str]]
) -> tuple[int, int]:
    """Apply entity_id renames via WebSocket. Returns (success_count, error_count)."""
    ok = 0
    errors = 0
    for _device, entity, new_id in renames:
        try:
            await client.send_command(
                "config/entity_registry/update",
                entity_id=entity.entity_id,
                new_entity_id=new_id,
            )
            ok += 1
        except Exception as exc:
            print_error(f"Failed to rename {entity.entity_id}: {exc}")
            errors += 1
    return ok, errors


def _update_energy_prefs(
    prefs: dict[str, Any],
    rename_map: dict[str, str],
) -> tuple[dict[str, Any], int]:
    """Replace stale entity_id references in energy dashboard prefs.

    Returns (updated_prefs, count_of_replacements).
    """
    prefs_json = json.dumps(prefs)
    count = 0
    for old_id, new_id in rename_map.items():
        if old_id in prefs_json:
            prefs_json = prefs_json.replace(f'"{old_id}"', f'"{new_id}"')
            count += 1
    return json.loads(prefs_json), count


@click.command()
@pass_ctx
def normalize(ctx: Context) -> None:
    """Normalize entity IDs to match current device names.

    Computes the expected entity_id for each span_ebus entity based on
    its device name and entity name, then renames any that don't match.
    This fixes entity IDs created before device names were available
    (e.g., circuit_050299_power → server_rack_1_spare_power).
    """
    run_async(_normalize(ctx))


async def _normalize(ctx: Context) -> None:
    async with ctx.client() as client:
        trees = await fetch_span_trees(client)
        if not trees:
            print_warn("No SPAN devices found in Home Assistant")
            return

        # Gather ALL entity_ids for collision detection
        raw_entities = await client.send_command("config/entity_registry/list")
        all_entity_ids = {e["entity_id"] for e in raw_entities}

        renames = _compute_renames(trees, all_entity_ids)

        if not renames:
            print_ok("All entity IDs already match device names — nothing to do")
            return

        # Group by device for display
        by_device: dict[str, list[tuple[HAEntity, str]]] = {}
        for device, entity, new_id in renames:
            by_device.setdefault(device.display_name, []).append((entity, new_id))

        console.print()
        for dev_name in sorted(by_device):
            console.print(f"[bold]{dev_name}[/bold]")
            for entity, new_id in sorted(by_device[dev_name], key=lambda x: x[0].entity_id):
                console.print(f"  {entity.entity_id} → [green]{new_id}[/green]")
        console.print()

        print_info(f"{len(renames)} entity ID(s) to rename")

        # Check if energy dashboard prefs need updating
        rename_map = {entity.entity_id: new_id for _, entity, new_id in renames}
        energy_prefs = await fetch_energy_prefs(client)
        new_prefs, energy_fixes = _update_energy_prefs(energy_prefs, rename_map)
        if energy_fixes:
            print_info(f"{energy_fixes} energy dashboard reference(s) to update")

        if ctx.dry_run:
            print_dry_run("No changes applied")
            return

        ok, errors = await _apply_renames(client, renames)
        if errors:
            print_warn(f"Renamed {ok}, failed {errors}")
        else:
            print_ok(f"Renamed {ok} entity ID(s)")

        # Update energy dashboard prefs
        if energy_fixes:
            try:
                await client.send_command("energy/save_prefs", **new_prefs)
                print_ok(f"Updated {energy_fixes} energy dashboard reference(s)")
            except Exception as exc:
                print_error(f"Failed to update energy dashboard: {exc}")
