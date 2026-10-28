"""Click CLI group and global options."""

from __future__ import annotations

from typing import Any

import click
from dotenv import load_dotenv

from hass_atlas.context import Context
from hass_atlas.ha_client import HAClientError

load_dotenv()


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
    "--proxy",
    envvar="HASS_ATLAS_PROXY",
    default=None,
    help=(
        "Proxy for the WebSocket connection, e.g. socks5h://localhost:1080 or "
        "http://proxy:8080 (or HASS_ATLAS_PROXY env var). Defaults to the proxy "
        "websockets finds in the environment or OS settings, then ALL_PROXY."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show planned changes without applying them",
)
@click.pass_context
def cli(
    ctx: click.Context, url: str | None, token: str, proxy: str | None, dry_run: bool
) -> None:
    """Home Assistant energy & area configuration CLI."""
    ctx.ensure_object(dict)
    ctx.obj = Context(url=url, token=token, dry_run=dry_run, proxy=proxy)


# Register subcommands (imported after cli is defined to avoid circular deps)
from hass_atlas.audit import audit  # noqa: E402
from hass_atlas.areas import areas  # noqa: E402
from hass_atlas.atlas.apply_cmd import apply  # noqa: E402
from hass_atlas.atlas.export_cmd import export  # noqa: E402
from hass_atlas.cascade import cascade  # noqa: E402
from hass_atlas.energy import energy, energy_audit, energy_topology  # noqa: E402
from hass_atlas.normalize import normalize  # noqa: E402
from hass_atlas.panels import link_panels  # noqa: E402
from hass_atlas.water import water  # noqa: E402

cli.add_command(audit)
cli.add_command(apply)
cli.add_command(areas)
cli.add_command(cascade)
cli.add_command(energy)
cli.add_command(energy_audit)
cli.add_command(energy_topology)
cli.add_command(export)
cli.add_command(link_panels)
cli.add_command(normalize)
cli.add_command(water)
