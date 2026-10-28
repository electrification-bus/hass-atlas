"""The snapshot an apply writes before its first write: before/, plan.json, journal.jsonl."""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from hass_atlas.atlas import DOMAINS
from hass_atlas.atlas import export_cmd
from hass_atlas.atlas.domains.base import ExportContext, Row
from hass_atlas.atlas.plan import Plan
from hass_atlas.atlas.yamlio import AtlasError, write_atlas


class SnapshotError(AtlasError):
    """The snapshot cannot be written, so apply writes nothing."""


def default_dir(url: str, now: datetime | None = None) -> Path:
    host = urllib.parse.urlsplit(url).hostname or "unknown"
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / ".local" / "state" / "hass-atlas" / "snapshots" / host / stamp


def _make_dir(path: Path) -> Path:
    """Create *path* with mode 0700; if it exists, add ``-2``, ``-3`` and so on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate, tries = path, 1
    while True:
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            tries += 1
            candidate = path.with_name(f"{path.name}-{tries}")
            continue
        candidate.chmod(0o700)
        return candidate


def _write_synced(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


class Journal:
    """``journal.jsonl``: one line per write, flushed and fsynced before the next write."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO = open(path, "a", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        try:
            self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            raise SnapshotError(f"cannot write {self.path}: {exc.strerror or exc}") from None

    def close(self) -> None:
        self._handle.close()


class Snapshot:
    def __init__(self, path: Path, journal: Journal) -> None:
        self.path = path
        self.journal = journal

    @classmethod
    def create(
        cls,
        path: Path,
        plan: Plan,
        live: dict[str, list[Row]],
        explicit: bool,
    ) -> Snapshot:
        """Write before/ and plan.json into a new directory at *path* and open the journal.

        An *explicit* path must be missing or empty; the default one gets a
        suffix if a run in the same second made it already.
        """
        try:
            if explicit:
                if path.exists() and (not path.is_dir() or any(path.iterdir())):
                    raise SnapshotError(f"snapshot directory {path} exists and is not empty")
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.chmod(0o700)
            else:
                path = _make_dir(path)
            written = [
                d for d in DOMAINS if any(c.domain == d.name and c.writes for c in plan.changes)
            ]
            rows = {d.name: live.get(d.name, []) for d in written}
            ctx = ExportContext(ha_version=plan.ha_version, rows=rows)
            docs = {d.name: d.export(rows[d.name], ctx) for d in written}
            manifest = export_cmd.build_manifest(plan.ha_version, list(docs))
            write_atlas(path / "before", docs, manifest)
            _write_synced(
                path / "plan.json", json.dumps(plan.as_dict(), indent=2, default=str) + "\n"
            )
            journal = Journal(path / "journal.jsonl")
        except OSError as exc:
            where = f"{exc.filename}: " if exc.filename else ""
            raise SnapshotError(
                f"cannot write snapshot {path}: {where}{exc.strerror or exc}"
            ) from None
        except AtlasError as exc:
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError(f"cannot write snapshot {path}: {exc}") from None
        return cls(path, journal)
