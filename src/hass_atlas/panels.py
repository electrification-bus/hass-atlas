"""Panel linking command — set via_device_id for sub-panels."""

from __future__ import annotations

import re

import click

from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.output import print_dry_run, print_error, print_info, print_ok
from hass_atlas.registry import fetch_registries
from hass_atlas.span_panel import exit_writer_unsupported, span_panel_only


_SERIAL_RE = re.compile(r"^nt-\d{4}-[a-z0-9]+$")


@click.command("link-panels")
@click.argument("links", nargs=-1, required=True)
@pass_ctx
def link_panels(ctx: Context, links: tuple[str, ...]) -> None:
    """Link sub-panels to parent panels via device registry.

    Each LINK is child_serial:parent_serial.

    Example: hass-atlas link-panels nt-2024-d3e4f:nt-2024-a1b2c nt-2024-g5h6j:nt-2024-d3e4f
    """
    run_async(_link_panels(ctx, links))


async def _link_panels(ctx: Context, links: tuple[str, ...]) -> None:
    pairs: list[tuple[str, str]] = []
    for link in links:
        if ":" not in link:
            print_error(f"Invalid link format '{link}' — expected child:parent")
            raise SystemExit(1)
        child, parent = link.split(":", 1)
        if not _SERIAL_RE.match(child):
            print_error(f"Invalid child serial '{child}' — expected nt-NNNN-xxxxx")
            raise SystemExit(1)
        if not _SERIAL_RE.match(parent):
            print_error(f"Invalid parent serial '{parent}' — expected nt-NNNN-xxxxx")
            raise SystemExit(1)
        pairs.append((child, parent))

    for child, parent in pairs:
        if ctx.dry_run:
            print_dry_run(f"Would link {child} → {parent}")
        else:
            print_info(f"Linking {child} → {parent}")

    if ctx.dry_run:
        return

    async with ctx.client() as client:
        devices, _, _ = await fetch_registries(client)
        if span_panel_only(devices):
            exit_writer_unsupported("link-panels")
        for child, parent in pairs:
            await client.send_command(
                "call_service",
                domain="span_ebus",
                service="link_subpanel",
                service_data={"sub_serial": child, "parent_serial": parent},
            )
            print_ok(f"Linked {child} → {parent}")
