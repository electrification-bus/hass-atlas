"""Stage 6: the Energy Dashboard prefs an apply writes.

Each file entry is matched to a live one by its key (``domains/energy.py``).
A grid source also matches a live grid source that shares its import or
export statistic, so a file can add the other side to a connection. Under
``merge``, one that holds a different statistic on the other side refuses
the entry, since Home Assistant allows each statistic in one grid source
only, and so do two live entries with the entry's key; under ``replace`` the
entry is matched to the first of them, or is new.

``merge`` keeps the live list: a matched entry gets the fields the file sets
(null removes one), new entries are appended in file order, and live entries
no file entry matches stay where they are. ``replace`` makes each list the
file holds the file's, except that a matched entry keeps the live fields a
file cannot hold (``config_entry_solar_forecast``, fields unknown to
hass-atlas). Home Assistant does not check
``included_in_stat`` targets or their order, so new entries need no ordering.

A new entry that references an entity missing from HA, or a file entity that
matches no live one, is skipped until a rerun finds it. Two file entries
whose entity ids resolve to the same key are refused, and so is an entry
that sets a source field the target release does not take. Only the lists that
change are written, in one ``energy/save_prefs``, with grid sources in the
live format.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from hass_atlas.atlas.domains import energy
from hass_atlas.atlas.domains.base import Row
from hass_atlas.atlas.domains.energy import SOURCES
from hass_atlas.atlas.plan import Change, EnergyStage
from hass_atlas.energy_prefs import LEGACY, UNIFIED, convert_sources

STAGE = 6
MODES = ("merge", "replace")

Check = Callable[[str], str | None]


@dataclass
class _Match:
    index: int | None = None
    refused: str | None = None


@dataclass
class _List:
    """One list being planned: the live entries and what the plan makes of them."""

    name: str
    live: list[Row]
    mode: str
    fmt: str = UNIFIED
    version: tuple[int, int] | None = None
    out: list[Row] = field(default_factory=list)
    # The change that put each entry of ``out`` there, None for a kept live entry.
    origin: list[Change | None] = field(default_factory=list)
    # The live index each update change was planned against, by id(change).
    updates: dict[int, int] = field(default_factory=dict)
    kept: set[int] = field(default_factory=set)

    def put(self, index: int | None, entry: Row, change: Change | None) -> None:
        """Place *entry* for live entry *index*: in place (merge) or appended (replace)."""
        if self.mode == "merge" and index is not None:
            self.out[index], self.origin[index] = entry, change
            return
        if index is not None:
            if index in self.kept:
                return
            self.kept.add(index)
        self.out.append(entry)
        self.origin.append(change)


def _without_nulls(entry: Row) -> Row:
    return {k: v for k, v in entry.items() if v is not None}


def _differs(a: Row, b: Row) -> list[str]:
    """The fields in which *a* and *b* differ once normalized, *a*'s order first."""
    left, right = energy.normalize(a), energy.normalize(b)
    names = list(dict.fromkeys([*a, *b]))
    return [n for n in names if left.get(n) != right.get(n)]


def _match(list_name: str, entry: Row, live: list[Row], mode: str) -> _Match:
    """The live entry *entry* (in live terms) matches, if any."""
    if list_name == SOURCES and entry.get("type") == "grid":
        return _match_grid(entry, live, mode)
    wanted = energy.key(list_name, entry)
    hits = [i for i, row in enumerate(live) if energy.key(list_name, row) == wanted]
    if len(hits) > 1 and mode == "merge":
        return _Match(refused=f"{len(hits)} live {list_name} entries have this key")
    return _Match(hits[0] if hits else None)


def _match_grid(entry: Row, live: list[Row], mode: str) -> _Match:
    sides = [
        (side, entry[side]) for side in ("stat_energy_from", "stat_energy_to") if entry.get(side)
    ]
    wanted = energy.key(SOURCES, entry)
    hits = []
    for i, row in enumerate(live):
        if row.get("type") != "grid":
            continue
        if sides:
            if any(row.get(side) == value for side, value in sides):
                hits.append(i)
        elif energy.key(SOURCES, row) == wanted:
            hits.append(i)
    if len(hits) > 1 and mode == "merge":
        return _Match(refused=f"{len(hits)} live grid sources share its statistics")
    if not hits:
        return _Match()
    row = live[hits[0]]
    for side, value in sides:
        if row.get(side) and row[side] != value:
            if mode == "replace":
                return _Match()
            return _Match(
                refused=f"the live grid source {energy.label(SOURCES, row)} shares a "
                f"statistic with it but has {side} {row[side]}"
            )
    return _Match(hits[0])


def _new_entry(list_name: str, entry: Row, live: Row | None) -> Row:
    """*entry* (in live terms) as a whole entry to send.

    The live entry *entry* replaces keeps the fields a file cannot hold, such
    as ``config_entry_solar_forecast`` and fields unknown to hass-atlas.
    """
    out = _without_nulls(entry)
    if list_name == SOURCES and out.get("type") == "grid" and "cost_adjustment_day" not in out:
        out["cost_adjustment_day"] = (live or {}).get("cost_adjustment_day", 0.0)
    if live is not None:
        known = energy.fields(list_name, live)
        out.update({k: v for k, v in live.items() if k not in known and v is not None})
    return energy.complete(list_name, out)


def _merged(list_name: str, live: Row, entry: Row) -> Row:
    """*live* with the fields *entry* (in live terms) sets; a null removes one."""
    out = dict(live)
    for name, value in entry.items():
        if value is None:
            out.pop(name, None)
        else:
            out[name] = value
    if "power_config" in live and "power_config" not in out and "stat_rate" not in entry:
        out.pop("stat_rate", None)  # HA derived it from the power_config just removed
    return energy.complete(list_name, out)


def _wait_reason(entry: Row, waits: Check | None) -> str | None:
    if waits is None:
        return None
    for ref in energy.entity_refs(entry):
        if reason := waits(ref):
            return f"{reason}; a rerun adds this entry once it is"
    return None


def _grid_duplicates(entries: list[Row]) -> set[int]:
    """Grid sources holding an import, export or ``stat_rate`` another grid source holds."""
    out: set[int] = set()
    for name in ("stat_energy_from", "stat_energy_to", "stat_rate"):
        values = Counter(e.get(name) for e in entries if e.get("type") == "grid" and e.get(name))
        out.update(
            i
            for i, e in enumerate(entries)
            if e.get("type") == "grid" and e.get(name) and values[e[name]] > 1
        )
    return out


def _refuse(work: _List, index: int, reason: str) -> None:
    """Refuse the change that put ``work.out[index]`` there, restoring the live entry."""
    change = work.origin[index]
    assert change is not None
    if change.op == "create":
        del work.out[index], work.origin[index]
    else:
        work.out[index] = work.live[work.updates[id(change)]]
        work.origin[index] = None
    change.op, change.reason = "refused", reason
    change.after, change.before = {}, {}


def _check_unique(work: _List) -> None:
    """Refuse the file changes that would give two grid sources one statistic."""
    if work.name != SOURCES:
        return
    while True:
        dupes = [i for i in sorted(_grid_duplicates(work.out)) if work.origin[i] is not None]
        if not dupes:
            return
        _refuse(work, dupes[-1], "another grid source holds one of its statistics")


def _check_legacy(work: _List) -> None:
    """Refuse the grid changes that the one legacy grid source cannot hold as planned.

    Home Assistant before 2026.3 stores one grid source, which pairs imports,
    exports and power sensors by position and has one ``cost_adjustment_day``;
    planned grid sources that do not come back from that form unchanged would
    be saved as other connections.
    """
    if work.name != SOURCES or work.fmt != LEGACY:
        return
    while True:
        grids = [i for i, e in enumerate(work.out) if e.get("type") == "grid"]
        entries = [work.out[i] for i in grids]
        back = convert_sources(convert_sources(entries, LEGACY), UNIFIED)
        changed = [i for i in grids if work.origin[i] is not None]
        if _canonical(back) == _canonical(entries) or not changed:
            return
        _refuse(
            work,
            changed[-1],
            "Home Assistant before 2026.3 keeps one grid source, which cannot hold this "
            "connection as planned (it pairs imports, exports and power by position "
            "and has one cost_adjustment_day)",
        )


def _twins(name: str, entries: list[Row]) -> list[bool]:
    """Whether each non-grid entry (in live terms) has the key of another one."""

    def ident(entry: Row) -> str | None:
        if name == SOURCES and entry.get("type") == "grid":
            return None  # _check_unique covers grid sources
        return energy.key(name, entry)

    keys = [ident(entry) for entry in entries]
    counts = Counter(k for k in keys if k is not None)
    return [k is not None and counts[k] > 1 for k in keys]


def _plan_list(
    work: _List,
    entries: list[Row],
    *,
    mode: str,
    resolve: Callable[[str], str],
    waits: Check | None,
    unbound: Check | None,
    held: dict[str, tuple[str, str]],
) -> tuple[list[Change], int]:
    """Plan one list; return its changes and how many live entries no file entry matches."""
    name = work.name
    resolved = [energy.map_refs(entry, resolve) for entry in entries]
    matches = [_match(name, entry, work.live, mode) for entry in resolved]
    claims = Counter(m.index for m in matches if m.index is not None and m.refused is None)
    twins = _twins(name, resolved)
    if mode == "merge":
        work.out = copy.deepcopy(work.live)
        work.origin = [None] * len(work.out)
    changes: list[Change] = []
    for entry, live_terms, found, twin in zip(entries, resolved, matches, twins, strict=True):
        change = Change(
            stage=STAGE,
            domain=energy.ENERGY.name,
            key=f"{name}/{energy.key(name, entry)}",
            op="refused",
            label=energy.label(name, live_terms),
            desired=dict(entry),
        )
        changes.append(change)
        row = work.live[found.index] if found.index is not None else None
        if change.key in held:
            change.op, change.reason = held[change.key]
        elif found.refused:
            change.reason = found.refused
        elif found.index is not None and claims[found.index] > 1:
            change.reason = "another file entry matches the same live entry"
        elif twin:
            change.reason = "another file entry resolves to the same statistics"
        elif found.index is None:
            _plan_new(work, change, entry, live_terms, waits)
            continue
        else:
            _plan_matched(work, change, entry, live_terms, found.index, unbound)
            continue
        if found.index is not None:
            work.put(found.index, work.live[found.index], None)  # refused: the live entry stays
    _check_unique(work)
    _check_legacy(work)
    matched = {m.index for m in matches if m.index is not None and m.refused is None}
    if mode == "replace":
        for index, row in enumerate(work.live):
            if index in matched:
                continue
            changes.append(
                Change(
                    stage=STAGE,
                    domain=energy.ENERGY.name,
                    key=f"{name}/{energy.key(name, row)}",
                    op="delete",
                    label=energy.label(name, row),
                    before=dict(row),
                )
            )
        if _canonical(work.out) != _canonical(work.live) and not any(c.writes for c in changes):
            changes.append(
                Change(
                    stage=STAGE,
                    domain=energy.ENERGY.name,
                    key=f"{name}/(order)",
                    op="update",
                    label="(order)",
                )
            )
        return changes, 0
    return changes, len(work.live) - len(matched)


def _plan_new(
    work: _List, change: Change, entry: Row, live_terms: Row, waits: Check | None
) -> None:
    if reason := _wait_reason(entry, waits):
        change.op, change.reason = "skipped", reason
        return
    new = _new_entry(work.name, live_terms, None)
    reason = energy.merged_error(work.name, new) or energy.version_error(
        work.name, new, work.version
    )
    if reason:
        change.reason = reason
        return
    change.op = "create"
    change.after = _without_nulls(entry)
    work.put(None, new, change)


def _plan_matched(
    work: _List, change: Change, entry: Row, live_terms: Row, index: int, unbound: Check | None
) -> None:
    row = work.live[index]
    if work.mode == "merge":
        wanted = _merged(work.name, row, live_terms)
    else:
        wanted = _new_entry(work.name, live_terms, row)
    names = _differs(wanted, row)
    reason = None
    if names:
        reason = energy.merged_error(work.name, wanted) or energy.version_error(
            work.name, wanted, work.version
        )
    if names and unbound is not None and reason is None:
        refs = [r for n in names for r in energy.entity_refs({n: entry.get(n)})]
        reason = next((why for r in refs if (why := unbound(r))), None)
    if reason:
        change.reason = reason
        work.put(index, row, None)
        return
    if not names:
        change.op = "unchanged"
        work.put(index, row, None)
        return
    change.op = "update"
    change.after = {n: entry.get(n) for n in names}
    change.before = {n: row.get(n) for n in names}
    work.updates[id(change)] = index
    work.put(index, wanted, change)


def _canonical(entries: list[Row]) -> list[Row]:
    return [energy.normalize(e) for e in entries]


def plan_energy(
    doc: Row,
    before: Row,
    *,
    mode: str,
    fmt: str,
    resolve: Callable[[str], str],
    version: tuple[int, int] | None = None,
    waits: Check | None = None,
    unbound: Check | None = None,
    held: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[Change], EnergyStage]:
    """Plan the file's lists *doc* against the live prefs *before*.

    *resolve* gives the live entity_id for a file entity_id, and *version* is
    the target's ``(year, month)``, None when unknown. *waits* says why
    a new entry cannot reference an entity yet, and *unbound* why an updated
    field cannot; the executor passes neither, and the *held* skips and
    refusals of the plan instead.
    """
    held = held or {}
    stage = EnergyStage(mode=mode, fmt=fmt, doc=doc, before=before, version=version)
    changes: list[Change] = []
    for name in energy.LISTS:
        if name not in doc:
            continue
        work = _List(name, energy.live_list(before, name), mode, fmt, version)
        planned, unmanaged = _plan_list(
            work,
            doc[name],
            mode=mode,
            resolve=resolve,
            waits=waits,
            unbound=unbound,
            held=held,
        )
        changes.extend(planned)
        stage.unmanaged[name] = unmanaged
        if _canonical(work.out) != _canonical(work.live):
            stage.lists.append(name)
            stage.wanted[name] = work.out
            payload = work.out
            if name == SOURCES and fmt == LEGACY:
                payload = convert_sources(payload, LEGACY)
            stage.payload[name] = payload
    stage.held = {
        c.key: (c.op, c.reason or "")
        for c in changes
        if c.op in ("skipped", "refused") and c.reason is not None
    }
    return changes, stage
