"""Reading and writing atlas directories."""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hass_atlas.atlas import DOMAINS, FORMAT, MANIFEST
from hass_atlas.atlas.domains.base import Domain, FlowDict, FlowList, Row


class AtlasError(Exception):
    """An atlas directory cannot be read or replaced."""


class AtlasFormatError(AtlasError):
    """A file in an atlas directory is malformed."""

    def __init__(self, path: Path, message: str, index: int | None = None) -> None:
        where = f"{path}: item {index}" if index is not None else str(path)
        super().__init__(f"{where}: {message}")
        self.path = path
        self.index = index


@dataclass
class Atlas:
    manifest: dict[str, Any]
    docs: dict[str, list[Row]] = field(default_factory=dict)


def _domain(name: str) -> Domain:
    for domain in DOMAINS:
        if domain.name == name:
            return domain
    raise KeyError(name)


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(
    FlowList,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)
_Dumper.add_representer(
    FlowDict,
    lambda dumper, data: dumper.represent_mapping(
        "tag:yaml.org,2002:map", data, flow_style=True
    ),
)


def _represent_str(dumper: _Dumper, data: str) -> yaml.ScalarNode:
    # PyYAML writes U+0085 raw in other styles and reads it back as a line break.
    if "\x85" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_str(data)


_Dumper.add_representer(str, _represent_str)


def _safe_dump(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=float("inf"),
    )


def dump(items: list[Row], field_order: tuple[str, ...]) -> str:
    """Render *items* as YAML, each item's fields in *field_order*."""
    if not items:
        return "[]\n"
    ordered = []
    for item in items:
        unknown = set(item) - set(field_order)
        if unknown:
            raise ValueError(f"fields not in field order: {sorted(unknown)}")
        ordered.append({name: item[name] for name in field_order if name in item})
    return _safe_dump(ordered)


def render_atlas(docs: dict[str, list[Row]], manifest: dict[str, Any]) -> dict[str, str]:
    """Return the text of each file, keyed by file name, manifest first."""
    files = {MANIFEST: _safe_dump(manifest)}
    for domain in DOMAINS:
        if domain.name in docs:
            files[domain.filename] = dump(docs[domain.name], domain.field_order)
    return files


TMP_PREFIX = ".hass-atlas-tmp-"


def _os_message(exc: OSError) -> str:
    reason = exc.strerror or str(exc)
    return f"{exc.filename}: {reason}" if exc.filename else reason


def check_out_dir(out: Path) -> None:
    """Refuse to write into *out* unless it is missing, empty or an atlas export.

    Hidden files, such as a ``.DS_Store``, do not count.
    """
    try:
        if not out.exists():
            return
        if not out.is_dir():
            raise AtlasError(f"{out} exists and is not a directory")
        visible = any(not entry.name.startswith(".") for entry in out.iterdir())
        has_manifest = (out / MANIFEST).is_file()
    except OSError as exc:
        raise AtlasError(f"cannot read {out}: {_os_message(exc)}") from None
    if visible and not has_manifest:
        raise AtlasError(f"{out} is not empty and holds no {MANIFEST}; refusing to write into it")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _roll_back(
    real: Path, done: list[str], staged: dict[str, Path], backups: dict[str, Path], created: bool
) -> list[str]:
    """Undo a failed write as far as possible; return the files that could not be restored."""
    unrestored: list[str] = []
    for name in reversed(done):
        try:
            if name in backups:
                os.replace(backups[name], real / name)
                del backups[name]
            else:
                (real / name).unlink(missing_ok=True)
        except OSError:
            unrestored.append(name)
    for path in staged.values():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for name, path in backups.items():
        if name in unrestored:
            continue  # keep it: it is the only copy of the old file
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if created and not unrestored:
        shutil.rmtree(real, ignore_errors=True)
    return unrestored


def _remove_stale(real: Path) -> None:
    for entry in real.glob(f"{TMP_PREFIX}*"):
        if entry.is_file() or entry.is_symlink():
            entry.unlink()


def write_atlas(out: Path, docs: dict[str, list[Row]], manifest: dict[str, Any]) -> None:
    """Write an atlas into *out*, replacing each file only once every file is staged.

    *out* is resolved first, so ``.`` and symlinks to a directory work and a
    symlink is never replaced. Files are staged as ``.hass-atlas-tmp-*`` in
    the directory, then renamed over the old ones, the manifest last. Staged
    files left by an interrupted run are removed first. Files in *out* that
    this export does not write are left alone. If an error is raised, the old
    files are put back; a process killed during the renames can leave a mix.
    """
    files = render_atlas(docs, manifest)
    check_out_dir(out)
    try:
        real = out.resolve()
        created = not real.exists()
        real.mkdir(parents=True, exist_ok=True)
        _remove_stale(real)
    except OSError as exc:
        raise AtlasError(f"cannot write {out}: {_os_message(exc)}") from None

    tag = secrets.token_hex(4)
    names = [name for name in files if name != MANIFEST] + [MANIFEST]
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    done: list[str] = []
    try:
        for name in names:
            target = real / name
            tmp = real / f"{TMP_PREFIX}{tag}-{name}"
            staged[name] = tmp
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(files[name])
                handle.flush()
                os.fsync(handle.fileno())
            if target.is_file():
                shutil.copymode(target, tmp)
                backup = real / f"{TMP_PREFIX}{tag}-old-{name}"
                shutil.copy2(target, backup)
                backups[name] = backup
        for name in names:
            os.replace(staged[name], real / name)
            done.append(name)
        _fsync_dir(real)
    except OSError as exc:
        unrestored = _roll_back(real, done, staged, backups, created)
        message = f"cannot write {out}: {_os_message(exc)}"
        if unrestored:
            message += "; could not restore " + ", ".join(unrestored) + " (their backups are left in place)"
        raise AtlasError(message) from None
    for path in backups.values():
        path.unlink(missing_ok=True)


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AtlasFormatError(path, exc.strerror or str(exc)) from None
    except UnicodeDecodeError:
        raise AtlasFormatError(path, "not valid UTF-8") from None
    except yaml.YAMLError as exc:
        raise AtlasFormatError(path, f"invalid YAML: {exc}") from None


def load_atlas(path: Path) -> Atlas:
    """Read and validate the atlas directory at *path*."""
    manifest_path = path / MANIFEST
    manifest = _load_yaml(manifest_path)
    if not isinstance(manifest, dict):
        raise AtlasFormatError(manifest_path, "expected a mapping")
    fmt = manifest.get("format")
    if type(fmt) is not int or fmt != FORMAT:
        raise AtlasFormatError(manifest_path, f"unsupported format {fmt!r}; expected {FORMAT}")
    names = manifest.get("domains")
    known = {domain.name for domain in DOMAINS}
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise AtlasFormatError(manifest_path, "domains must be a list of names")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise AtlasFormatError(manifest_path, f"unknown domains {unknown}")

    atlas = Atlas(manifest=manifest)
    for name in names:
        domain = _domain(name)
        doc_path = path / domain.filename
        items = _load_yaml(doc_path)
        if not isinstance(items, list):
            raise AtlasFormatError(doc_path, "expected a list")
        key = domain.field_order[0]
        seen: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise AtlasFormatError(doc_path, "expected a mapping", index)
            if error := domain.key_error(item):
                raise AtlasFormatError(doc_path, error, index)
            ident = domain.item_key(item)
            if ident in seen:
                raise AtlasFormatError(doc_path, f"duplicate {key} {item[key]!r}", index)
            seen.add(ident)
            unknown_fields = sorted(set(item) - set(domain.field_order))
            if unknown_fields:
                raise AtlasFormatError(doc_path, f"unknown fields {unknown_fields}", index)
        atlas.docs[name] = items
    return atlas
