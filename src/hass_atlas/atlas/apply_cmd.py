"""``apply`` command: make Home Assistant match an atlas directory."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from hass_atlas.atlas.domains.base import Row
from hass_atlas.atlas.executor import Executor
from hass_atlas.atlas.export_cmd import parse_only
from hass_atlas.atlas.plan import Plan, registry, render, summary
from hass_atlas.atlas.planner import build_plan, domains_to_read, needs_states
from hass_atlas.atlas.snapshot import Snapshot, SnapshotError, default_dir
from hass_atlas.atlas.yamlio import Atlas, AtlasError, load_atlas
from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.ha_client import HAClientError
from hass_atlas.output import print_error, print_info, print_ok, print_warn


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _major_minor(version: str | None) -> str | None:
    return ".".join(version.split(".")[:2]) if version else None


def _warn_version(atlas: Atlas, live: str | None) -> None:
    exported = atlas.manifest.get("ha_version")
    exported = str(exported) if exported is not None else None
    if exported and live and _major_minor(exported) != _major_minor(live):
        print_warn(f"The atlas was exported from Home Assistant {exported}; this one runs {live}.")


def _applied(atlas: Atlas, only: str | None) -> set[str]:
    if only is None:
        return set(atlas.docs)
    names = {domain.name for domain in parse_only(only)}
    missing = sorted(names - set(atlas.docs))
    if missing:
        raise click.BadParameter(
            f"{', '.join(missing)} not in the atlas, which holds {', '.join(atlas.docs)}",
            param_hint="--only",
        )
    return names


def _print_plan(plan: Plan, verbose: bool) -> None:
    for line in render(plan, verbose):
        click.echo(line)


async def _apply(
    ctx: Context,
    atlas: Atlas,
    applied: set[str],
    *,
    yes: bool,
    check: bool,
    snapshot_dir: Path | None,
    verbose: bool,
) -> int:
    async with ctx.client() as client:
        live: dict[str, list[Row]] = {}
        for name in domains_to_read(atlas.docs, applied):
            live[name] = await registry(name).fetch(client)
        states = None
        if needs_states(atlas.docs, applied):
            rows = await client.send_command("get_states")
            states = {row["entity_id"]: row for row in rows}
        _warn_version(atlas, client.ha_version)
        plan = build_plan(
            atlas.docs, applied, live, states, url=ctx.url, ha_version=client.ha_version
        )
        _print_plan(plan, verbose)

        if check or ctx.dry_run:
            click.echo(summary(plan) + " Dry run: nothing written.")
            if not check:
                return 0
            return 2 if plan.writes else 3 if plan.refused else 0
        if not plan.writes:
            click.echo(summary(plan) + " Nothing to write.")
            return 3 if plan.refused else 0

        click.echo(summary(plan))
        count = sum(1 for change in plan.changes if change.writes)
        if not yes:
            if not _stdin_is_tty():
                print_error(
                    "Not a terminal: pass --yes to write, or --dry-run to only show the plan."
                )
                return 1
            if not click.confirm(f"Apply {count} changes to {ctx.url}?", default=False):
                print_info("Nothing written.")
                return 1

        path = snapshot_dir or default_dir(ctx.url)
        snapshot = Snapshot.create(path, plan, live, explicit=snapshot_dir is not None)
        try:
            code = await Executor(client, plan, snapshot.journal, click.echo).run()
        except (HAClientError, SnapshotError) as exc:
            print_error(str(exc))
            code = 1
        finally:
            snapshot.journal.close()
        click.echo(summary(plan, done=True))
        print_info(f"Snapshot and journal: {snapshot.path}")
        if code == 0:
            print_ok("Applied.")
        return code


@click.command("apply")
@click.argument("directory", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--only",
    default=None,
    metavar="D,..",
    help="Comma-separated domains to apply. Other domains are read for references, never written.",
)
@click.option("--yes", is_flag=True, help="Write without asking for confirmation.")
@click.option(
    "--check",
    is_flag=True,
    help=(
        "Only plan (implies --dry-run). Exit 0 if nothing would change, 2 if changes "
        "are pending, 3 if items are refused."
    ),
)
@click.option(
    "--snapshot-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Where to save the snapshot and journal (default "
        "~/.local/state/hass-atlas/snapshots/<host>/<UTC time>). Must be missing or empty."
    ),
)
@click.option("-v", "--verbose", is_flag=True, help="List unchanged items individually.")
@pass_ctx
def apply(
    ctx: Context,
    directory: Path,
    only: str | None,
    yes: bool,
    check: bool,
    snapshot_dir: Path | None,
    verbose: bool,
) -> None:
    """Make Home Assistant's floors, labels and areas match an atlas directory.

    Creates missing items and updates differing fields; never deletes. A
    field absent from a file is left alone, and null clears it. Before the
    first write, saves the affected domains, the plan and a journal of every
    write to a snapshot directory. With --dry-run or --check, prints the plan
    and writes nothing.

    Exit codes: 0 done; 1 error, declined, failed write or failed verify;
    2 changes pending (--check); 3 done, but items were refused or skipped.
    """
    try:
        atlas = load_atlas(directory)
    except AtlasError as exc:
        raise click.ClickException(str(exc)) from None
    applied = _applied(atlas, only)
    try:
        code = run_async(
            _apply(
                ctx,
                atlas,
                applied,
                yes=yes,
                check=check,
                snapshot_dir=snapshot_dir,
                verbose=verbose,
            )
        )
    except AtlasError as exc:
        raise click.ClickException(str(exc)) from None
    if code:
        sys.exit(code)
