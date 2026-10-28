"""``export`` command: write Home Assistant configuration to YAML files."""

from __future__ import annotations

import difflib
import importlib.metadata
from pathlib import Path
from typing import Any

import click

from hass_atlas.atlas import DOMAINS, FORMAT, MANIFEST
from hass_atlas.atlas.domains.base import Domain, ExportContext, Row
from hass_atlas.atlas.yamlio import (
    AtlasError,
    check_out_dir,
    load_atlas,
    render_atlas,
    write_atlas,
)
from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.output import print_dry_run, print_ok


def _package_version() -> str:
    return importlib.metadata.version("hass-atlas")


def _parse_only(value: str | None) -> tuple[Domain, ...]:
    if value is None:
        return DOMAINS
    names = {name.strip() for name in value.split(",") if name.strip()}
    known = {domain.name for domain in DOMAINS}
    unknown = sorted(names - known)
    if unknown or not names:
        raise click.BadParameter(
            f"unknown domain(s) {', '.join(unknown) or '(none given)'}; "
            f"choose from {', '.join(d.name for d in DOMAINS)}",
            param_hint="--only",
        )
    return tuple(domain for domain in DOMAINS if domain.name in names)


def _manifest(ha_version: str | None, domains: list[str]) -> dict[str, Any]:
    manifest: dict[str, Any] = {"format": FORMAT, "hass_atlas": _package_version()}
    if ha_version:
        manifest["ha_version"] = ha_version
    manifest["domains"] = domains
    return manifest


def _read_old(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        raise AtlasError(f"cannot read {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise AtlasError(f"cannot read {path}: not valid UTF-8") from None


def _diff(out: Path, files: dict[str, str]) -> list[str]:
    changed = []
    for name, new in files.items():
        path = out / name
        old = _read_old(path)
        lines = list(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=str(path) if path.is_file() else "/dev/null",
                tofile=str(path),
            )
        )
        if lines:
            changed.append(name)
            click.echo("".join(lines), nl=False)
    return changed


def _kept_domains(out: Path, selected: tuple[Domain, ...]) -> tuple[list[str], dict[str, Any]]:
    """Return the domains an earlier export in *out* holds that this run keeps."""
    if len(selected) == len(DOMAINS) or not (out / MANIFEST).is_file():
        return [], {}
    old = load_atlas(out).manifest
    names = {domain.name for domain in selected}
    return [name for name in old["domains"] if name not in names], old


def _check_same_source(
    out: Path, kept: list[str], old: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """Refuse an --only run whose manifest would not describe the kept files."""
    if not kept:
        return
    for key in ("ha_version", "hass_atlas"):
        if old.get(key) != manifest.get(key):
            raise AtlasError(
                f"{out} holds {', '.join(kept)} from {key} {old.get(key)!r}, but this run "
                f"has {manifest.get(key)!r}; run export without --only to refresh every domain"
            )


async def _export(ctx: Context, out: Path, selected: tuple[Domain, ...]) -> None:
    check_out_dir(out)
    kept, old_manifest = _kept_domains(out, selected)
    rows: dict[str, list[Row]] = {}
    async with ctx.client() as client:
        for domain in selected:
            rows[domain.name] = await domain.fetch(client)
        ha_version = client.ha_version

    export_ctx = ExportContext(ha_version=ha_version, rows=rows)
    docs = {domain.name: domain.export(rows[domain.name], export_ctx) for domain in selected}
    domains = [d.name for d in DOMAINS if d.name in docs or d.name in kept]
    manifest = _manifest(ha_version, domains)
    _check_same_source(out, kept, old_manifest, manifest)

    if ctx.dry_run:
        files = render_atlas(docs, manifest)
        changed = _diff(out, files)
        if changed:
            print_dry_run(f"Would change {', '.join(changed)} in {out}. Nothing written.")
        else:
            print_dry_run(f"No changes to {out}; nothing written.")
        return

    write_atlas(out, docs, manifest)
    counts = ", ".join(f"{name} ({len(items)})" for name, items in docs.items())
    print_ok(f"Exported {counts} to {out}/")


@click.command("export")
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("atlas"),
    show_default=True,
    help="Directory to write. Its files are replaced only once every new file is ready.",
)
@click.option(
    "--only",
    default=None,
    metavar="D,..",
    help=(
        f"Comma-separated domains to export ({', '.join(d.name for d in DOMAINS)}). "
        "Other domains from an earlier export are kept if it came from the same "
        "Home Assistant and hass-atlas versions."
    ),
)
@pass_ctx
def export(ctx: Context, out: Path, only: str | None) -> None:
    """Export floors, labels and areas to YAML files (reads only).

    With --dry-run, prints a unified diff against the files in --out and
    writes nothing.
    """
    selected = _parse_only(only)
    try:
        run_async(_export(ctx, out, selected))
    except AtlasError as exc:
        raise click.ClickException(str(exc)) from None
