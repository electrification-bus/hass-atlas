"""Areas command — assign circuit devices to HA areas."""

from __future__ import annotations

import json

import click

from ha_atlas.context import Context, pass_ctx, run_async
from ha_atlas.models import HAArea
from ha_atlas.output import print_dry_run, print_info, print_ok, print_warn
from ha_atlas.registry import fetch_areas, fetch_span_trees


@click.command()
@click.option(
    "--mapping",
    type=click.Path(exists=True),
    default=None,
    help="JSON file mapping device names to area names (null to skip)",
)
@click.option(
    "--create-missing",
    is_flag=True,
    default=False,
    help="Create areas that don't exist",
)
@pass_ctx
def areas(ctx: Context, mapping: str | None, create_missing: bool) -> None:
    """Assign SPAN circuit devices to Home Assistant areas."""
    run_async(_areas(ctx, mapping, create_missing))


async def _areas(ctx: Context, mapping_file: str | None, create_missing: bool) -> None:
    # Load optional mapping
    name_to_area: dict[str, str | None] = {}
    if mapping_file:
        with open(mapping_file) as f:
            name_to_area = json.load(f)
        print_info(f"Loaded mapping with {len(name_to_area)} entries")

    async with ctx.client() as client:
        trees = await fetch_span_trees(client)
        existing_areas = await fetch_areas(client)

        if not trees:
            print_warn("No SPAN devices found")
            return

        area_by_name: dict[str, HAArea] = {a.name: a for a in existing_areas}

        actions = _plan_assignments(trees, name_to_area, area_by_name)

        if not actions:
            print_ok("All circuit devices already have correct area assignments")
            return

        # Show plan
        areas_to_create: set[str] = {
            a.area_name for a in actions if a.needs_create and a.area_name
        }
        assignments = [(a.device_name, a.area_name) for a in actions if a.area_name]

        if areas_to_create:
            print_info(f"Areas to create: {', '.join(sorted(areas_to_create))}")
        for dev_name, target_area in assignments:
            msg = f"{dev_name} → {target_area}"
            if ctx.dry_run:
                print_dry_run(msg)
            else:
                print_info(msg)

        if ctx.dry_run:
            print_info(f"Would assign {len(assignments)} device(s) ({len(areas_to_create)} area(s) to create)")
            return

        # Execute
        if areas_to_create and create_missing:
            for name in sorted(areas_to_create):
                result = await client.send_command(
                    "config/area_registry/create", name=name
                )
                new_area = HAArea(area_id=result["area_id"], name=result["name"])
                area_by_name[new_area.name] = new_area
                print_ok(f"Created area: {new_area.name}")
        elif areas_to_create and not create_missing:
            print_warn(
                f"Missing areas: {', '.join(sorted(areas_to_create))}. "
                "Use --create-missing to create them."
            )
            return

        # Assign devices
        assigned = 0
        for action in actions:
            if not action.area_name:
                continue
            area = area_by_name.get(action.area_name)
            if not area:
                print_warn(f"Area '{action.area_name}' not found, skipping {action.device_name}")
                continue
            await client.send_command(
                "config/device_registry/update",
                device_id=action.device_id,
                area_id=area.area_id,
            )
            assigned += 1
            print_ok(f"Assigned {action.device_name} → {action.area_name}")

        print_ok(f"Assigned {assigned} device(s)")


class _AssignAction:
    """Planned area assignment for a device."""

    def __init__(
        self, device_id: str, device_name: str, area_name: str | None, needs_create: bool
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.area_name = area_name
        self.needs_create = needs_create


def _plan_assignments(
    trees: list,
    name_to_area: dict[str, str | None],
    area_by_name: dict[str, HAArea],
) -> list[_AssignAction]:
    """Plan area assignments for all circuit devices."""
    actions: list[_AssignAction] = []

    for tree in trees:
        for circuit in tree.circuits:
            device_name = circuit.display_name

            # Determine target area name
            if device_name in name_to_area:
                target_area_name = name_to_area[device_name]
            else:
                target_area_name = device_name

            # null in mapping means skip
            if target_area_name is None:
                continue

            # Check if already assigned correctly
            if circuit.area_id:
                current_area = next(
                    (a for a in area_by_name.values() if a.area_id == circuit.area_id), None
                )
                if current_area and current_area.name == target_area_name:
                    continue

            needs_create = target_area_name not in area_by_name
            actions.append(_AssignAction(
                device_id=circuit.id,
                device_name=device_name,
                area_name=target_area_name,
                needs_create=needs_create,
            ))

    return actions
