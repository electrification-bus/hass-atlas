"""Click CLI group and global options."""

from __future__ import annotations

import sys
from typing import Any

import click
from dotenv import load_dotenv

from hass_atlas.context import Context
from hass_atlas.discovery import discover_ha
from hass_atlas.ha_client import HAClientError
from hass_atlas.output import print_error, print_info

load_dotenv()


def _resolve_url(url: str | None) -> str:
    """Resolve HA URL: CLI/env override, otherwise mDNS discovery."""
    if url:
        return url

    print_info("No --url specified, discovering Home Assistant via mDNS...")
    instances = discover_ha()

    if not instances:
        print_error("No Home Assistant instances found via mDNS. Specify --url or set HA_URL.")
        sys.exit(1)

    if len(instances) == 1:
        ha = instances[0]
        label = ha.location_name or ha.host
        print_info(f"Found: {label} at {ha.url} (v{ha.version or '?'})")
        return ha.url

    # Multiple instances — let user pick
    print_info(f"Found {len(instances)} Home Assistant instances:")
    for i, ha in enumerate(instances, 1):
        label = ha.location_name or ha.host
        click.echo(f"  {i}. {label} — {ha.url} (v{ha.version or '?'})")
    choice: int = click.prompt("Select instance", type=click.IntRange(1, len(instances)))
    return instances[choice - 1].url


class _ErrorHandlingGroup(click.Group):
    """Click group that catches connection errors and prints clean messages."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except HAClientError as exc:
            raise click.ClickException(str(exc)) from None
        except RuntimeError as exc:
            if "mDNS" in str(exc):
                raise click.ClickException(str(exc)) from None
            raise


@click.group(cls=_ErrorHandlingGroup)
@click.option(
    "--url",
    envvar="HA_URL",
    default=None,
    help="Home Assistant URL (or HA_URL env var). Auto-discovered via mDNS if omitted.",
)
@click.option(
    "--token",
    envvar="HASS_API_TOKEN",
    required=True,
    help="Long-lived access token (or HASS_API_TOKEN env var)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show planned changes without applying them",
)
@click.pass_context
def cli(ctx: click.Context, url: str | None, token: str, dry_run: bool) -> None:
    """Home Assistant energy & area configuration CLI."""
    resolved_url = _resolve_url(url)
    ctx.ensure_object(dict)
    ctx.obj = Context(url=resolved_url, token=token, dry_run=dry_run)


# Register subcommands (imported after cli is defined to avoid circular deps)
from hass_atlas.audit import audit  # noqa: E402
from hass_atlas.areas import areas  # noqa: E402
from hass_atlas.energy import energy, energy_audit, energy_topology  # noqa: E402
from hass_atlas.normalize import normalize  # noqa: E402
from hass_atlas.panels import link_panels  # noqa: E402
from hass_atlas.water import water  # noqa: E402

cli.add_command(audit)
cli.add_command(areas)
cli.add_command(energy)
cli.add_command(energy_audit)
cli.add_command(energy_topology)
cli.add_command(link_panels)
cli.add_command(normalize)
cli.add_command(water)
