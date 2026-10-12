"""Read and write Energy Dashboard grid sources in either of HA's two formats.

Home Assistant 2026.3 replaced the single legacy grid source::

    {"type": "grid", "flow_from": [{"stat_energy_from": ...}, ...],
     "flow_to": [{"stat_energy_to": ...}, ...], "power": [...],
     "cost_adjustment_day": 0.0}

with one unified grid source per connection::

    {"type": "grid", "stat_energy_from": ..., "stat_energy_to": ...,
     "stat_rate": ..., "power_config": {...}, "cost_adjustment_day": 0.0}

From 2026.3, ``energy/save_prefs`` rejects the legacy form and stored prefs are
migrated to the unified one; before it, only the legacy form is accepted and
at most one grid source is allowed.  Both require ``cost_adjustment_day``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

UNIFIED = "unified"
LEGACY = "legacy"

# First Home Assistant release whose energy/save_prefs takes the unified grid form.
UNIFIED_GRID_SINCE = (2026, 3)

# Unified grid fields that carry import cost and export compensation settings.
_IMPORT_COST_KEYS = ("stat_cost", "entity_energy_price", "number_energy_price")
_EXPORT_COST_KEYS = (
    "stat_compensation",
    "entity_energy_price_export",
    "number_energy_price_export",
)


@dataclass
class GridConnection:
    """One grid import/export pair, with an optional power sensor."""

    stat_energy_from: str | None = None
    stat_energy_to: str | None = None
    stat_rate: str | None = None


def parse_ha_version(version: str | None) -> tuple[int, int] | None:
    """Return ``(year, month)`` from a Home Assistant version such as ``2026.9.4``."""
    m = re.match(r"^\s*(\d{4})\.(\d{1,2})(?:\D|$)", version or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_grid(source: dict[str, Any]) -> bool:
    return source.get("type") == "grid"


def is_legacy_grid(source: dict[str, Any]) -> bool:
    return is_grid(source) and ("flow_from" in source or "flow_to" in source)


def grid_format(ha_version: str | None, prefs: dict[str, Any] | None = None) -> str:
    """Choose the grid format the target Home Assistant accepts.

    The version decides when it parses.  Otherwise the stored grid sources
    decide (``energy/get_prefs`` returns them in the format HA stores), and
    with neither, the unified format of current releases is used.
    """
    parsed = parse_ha_version(ha_version)
    if parsed is not None:
        return UNIFIED if parsed >= UNIFIED_GRID_SINCE else LEGACY
    grids = [s for s in (prefs or {}).get("energy_sources", []) or [] if is_grid(s)]
    if grids:
        return LEGACY if any(is_legacy_grid(s) for s in grids) else UNIFIED
    return UNIFIED


# ---------------------------------------------------------------------------
# Readers (either format)
# ---------------------------------------------------------------------------


def grid_import_ids(source: dict[str, Any]) -> list[str]:
    """Import energy statistics of a grid source."""
    if not is_grid(source):
        return []
    ids = [f.get("stat_energy_from") for f in source.get("flow_from", []) or []]
    ids.append(source.get("stat_energy_from"))
    return [i for i in ids if i]


def grid_export_ids(source: dict[str, Any]) -> list[str]:
    """Export energy statistics of a grid source."""
    if not is_grid(source):
        return []
    ids = [f.get("stat_energy_to") for f in source.get("flow_to", []) or []]
    ids.append(source.get("stat_energy_to"))
    return [i for i in ids if i]


def power_config_ids(power_config: dict[str, Any] | None) -> list[str]:
    return [v for v in (power_config or {}).values() if isinstance(v, str) and v]


def source_rate_ids(source: dict[str, Any]) -> list[str]:
    """Power statistics of a source: ``stat_rate``, ``power_config``, legacy ``power``."""
    ids: list[str] = []
    if rate := source.get("stat_rate"):
        ids.append(rate)
    ids += power_config_ids(source.get("power_config"))
    for p in source.get("power", []) or []:
        if rate := p.get("stat_rate"):
            ids.append(rate)
        ids += power_config_ids(p.get("power_config"))
    return ids


def source_energy_ids(source: dict[str, Any]) -> set[str]:
    """Energy statistics a source references, in either grid format."""
    ids = set(grid_import_ids(source)) | set(grid_export_ids(source))
    if not is_grid(source):
        for k in ("stat_energy_from", "stat_energy_to"):
            if v := source.get(k):
                ids.add(v)
    return ids


def grid_connections(source: dict[str, Any]) -> list[GridConnection]:
    """The connections of a grid source; a legacy source pairs flows by index."""
    if not is_grid(source):
        return []
    if not is_legacy_grid(source):
        return [GridConnection(
            source.get("stat_energy_from"), source.get("stat_energy_to"), source.get("stat_rate")
        )]
    return [
        GridConnection(u.get("stat_energy_from"), u.get("stat_energy_to"), u.get("stat_rate"))
        for u in legacy_to_unified(source)
    ]


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def legacy_to_unified(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one legacy grid source to unified ones, as HA's storage migration does.

    Pairs ``flow_from[i]``, ``flow_to[i]`` and ``power[i]`` into connection ``i``.
    """
    flow_from = source.get("flow_from", []) or []
    flow_to = source.get("flow_to", []) or []
    power = source.get("power", []) or []
    cost_adj = source.get("cost_adjustment_day", 0.0)
    result: list[dict[str, Any]] = []
    for i in range(max(len(flow_from), len(flow_to), len(power))):
        new: dict[str, Any] = {"type": "grid", "cost_adjustment_day": cost_adj}
        if i < len(flow_from):
            ff = flow_from[i]
            new["stat_energy_from"] = ff.get("stat_energy_from") or None
            for k in _IMPORT_COST_KEYS:
                if k in ff:
                    new[k] = ff[k]
        if i < len(flow_to):
            ft = flow_to[i]
            new["stat_energy_to"] = ft.get("stat_energy_to")
            if "stat_compensation" in ft:
                new["stat_compensation"] = ft["stat_compensation"]
            if "entity_energy_price" in ft:
                new["entity_energy_price_export"] = ft["entity_energy_price"]
            if "number_energy_price" in ft:
                new["number_energy_price_export"] = ft["number_energy_price"]
        if i < len(power):
            for k in ("stat_rate", "power_config"):
                if k in power[i]:
                    new[k] = power[i][k]
        result.append(new)
    return result


def unified_to_legacy(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Combine unified grid sources into the one legacy grid source older HA accepts."""
    if not sources:
        return None
    legacy: dict[str, Any] = {
        "type": "grid",
        "flow_from": [],
        "flow_to": [],
        "cost_adjustment_day": sources[0].get("cost_adjustment_day", 0.0),
    }
    power: list[dict[str, Any]] = []
    for s in sources:
        if s.get("stat_energy_from"):
            legacy["flow_from"].append(_new_legacy_flow_from(
                s["stat_energy_from"], {k: s[k] for k in _IMPORT_COST_KEYS if k in s}
            ))
        if s.get("stat_energy_to"):
            flow: dict[str, Any] = {"stat_energy_to": s["stat_energy_to"]}
            if "stat_compensation" in s:
                flow["stat_compensation"] = s["stat_compensation"]
            if "entity_energy_price_export" in s:
                flow["entity_energy_price"] = s["entity_energy_price_export"]
            if "number_energy_price_export" in s:
                flow["number_energy_price"] = s["number_energy_price_export"]
            legacy["flow_to"].append(flow)
        entry = {k: s[k] for k in ("stat_rate", "power_config") if k in s}
        if entry:
            power.append(entry)
    if power:
        legacy["power"] = power
    return legacy


def convert_sources(sources: list[dict[str, Any]], fmt: str) -> list[dict[str, Any]]:
    """Return a copy of ``energy_sources`` with every grid source in ``fmt``.

    Unified grid sources merged for the legacy format take the position of the
    first one; other sources keep their order.
    """
    result: list[dict[str, Any]] = []
    if fmt == UNIFIED:
        for s in sources:
            if is_legacy_grid(s):
                result.extend(legacy_to_unified(s))
            else:
                result.append(copy.deepcopy(s))
        return result
    grids = [s for s in sources if is_grid(s)]
    if all(is_legacy_grid(s) for s in grids) and len(grids) <= 1:
        return copy.deepcopy(sources)
    unified = [u for s in grids for u in (legacy_to_unified(s) if is_legacy_grid(s) else [s])]
    merged = unified_to_legacy(copy.deepcopy(unified))
    placed = False
    for s in sources:
        if not is_grid(s):
            result.append(copy.deepcopy(s))
        elif not placed and merged is not None:
            result.append(merged)
            placed = True
    return result


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

_SIDES = (
    ("stat_energy_from", _IMPORT_COST_KEYS),
    ("stat_energy_to", _EXPORT_COST_KEYS),
)
_IMPORT_PRICE_KEYS = ("entity_energy_price", "number_energy_price")
_EXPORT_PRICE_KEYS = ("entity_energy_price_export", "number_energy_price_export")
_ENTITY_ID_RE = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")


def _is_entity_id(stat: str) -> bool:
    """True for an entity id; False for an external statistic such as ``domain:id``."""
    return bool(_ENTITY_ID_RE.match(stat))


def _new_legacy_flow_from(stat: str, costs: dict[str, Any] | None = None) -> dict[str, Any]:
    """A legacy ``flow_from`` entry with the price keys HA's validator reads directly."""
    flow: dict[str, Any] = {"stat_energy_from": stat, **{k: None for k in _IMPORT_COST_KEYS}}
    flow.update(costs or {})
    return flow


def _new_unified(conn: GridConnection) -> dict[str, Any]:
    source: dict[str, Any] = {"type": "grid"}
    if conn.stat_energy_from:
        source["stat_energy_from"] = conn.stat_energy_from
    if conn.stat_energy_to:
        source["stat_energy_to"] = conn.stat_energy_to
    if conn.stat_rate:
        source["stat_rate"] = conn.stat_rate
    source["cost_adjustment_day"] = 0.0
    return source


def _unified_has_refs(source: dict[str, Any]) -> bool:
    return bool(
        source.get("stat_energy_from")
        or source.get("stat_energy_to")
        or source.get("stat_rate")
        or source.get("power_config")
    )


def _shared_sides(source: dict[str, Any], conn: GridConnection) -> int:
    """Sides ``source`` shares with ``conn``; 0 when any side holds a different statistic."""
    shared = 0
    for side, want in (("stat_energy_from", conn.stat_energy_from),
                       ("stat_energy_to", conn.stat_energy_to)):
        have = source.get(side)
        if want and have:
            if have != want:
                return 0
            shared += 1
    return shared


def _apply_carried(
    source: dict[str, Any],
    import_costs: list[dict[str, Any]],
    export_costs: list[dict[str, Any]],
    settings: list[dict[str, Any]],
) -> None:
    """Give a new source the costs and settings of a grid source it replaces."""
    if source.get("stat_energy_from") and import_costs:
        costs = import_costs.pop(0)
        if not _is_entity_id(source["stat_energy_from"]):
            costs = {k: v for k, v in costs.items() if k not in _IMPORT_PRICE_KEYS}
        source.update(costs)
    if source.get("stat_energy_to") and export_costs:
        costs = export_costs.pop(0)
        if not _is_entity_id(source["stat_energy_to"]):
            costs = {k: v for k, v in costs.items() if k not in _EXPORT_PRICE_KEYS}
        source.update(costs)
    if settings:
        source.update(settings.pop(0))


def merge_grid_sources(
    grids: list[dict[str, Any]],
    wanted: list[GridConnection],
    skipped: set[str] | None = None,
    fmt: str = UNIFIED,
) -> list[dict[str, Any]]:
    """Merge wanted grid connections into existing grid sources already in ``fmt``.

    Unified: a wanted connection fills the empty sides of the existing source
    that shares its import or export statistic (preferring one that shares
    both) and holds no different statistic on either side; otherwise it adds a
    new source.  A statistic or ``stat_rate`` another kept grid source already
    uses is not added again.  Existing sources that reference a ``skipped``
    statistic lose that side, or are dropped when no wanted connection matches
    them, and their cost fields, ``cost_adjustment_day`` and ``name`` move to
    the new sources.  Other existing sources are kept unchanged.

    Legacy: the single grid source keeps its flows except skipped ones, and
    gains a flow for each wanted statistic it lacks; a new import flow takes
    the cost fields of a skipped one.
    """
    skipped = skipped or set()
    if fmt == LEGACY:
        return _merge_legacy(grids, wanted, skipped)

    work = [copy.deepcopy(s) for s in grids]
    touched = [bool(source_energy_ids(s) & skipped) for s in grids]
    import_costs: list[dict[str, Any]] = []
    export_costs: list[dict[str, Any]] = []
    for s in work:
        for (side, keys), pool in zip(_SIDES, (import_costs, export_costs)):
            if s.get(side) in skipped:
                pool.append({k: s[k] for k in keys if k in s})
                s[side] = None
                for k in keys:
                    s.pop(k, None)

    claimed: dict[int, GridConnection] = {}
    new_conns: list[GridConnection] = []
    for conn in wanted:
        best, best_shared = None, 0
        for i, s in enumerate(work):
            if i in claimed:
                continue
            shared = _shared_sides(s, conn)
            if shared > best_shared:
                best, best_shared = i, shared
        if best is None:
            new_conns.append(conn)
        else:
            claimed[best] = conn

    settings: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    # Per kept source, the keys this merge set, with the value each replaced.
    added: list[dict[str, Any]] = []
    for i, s in enumerate(work):
        if i not in claimed:
            if not touched[i]:
                kept.append(s)
                added.append({})
                continue
            for (side, keys), pool in zip(_SIDES, (import_costs, export_costs)):
                if s.get(side):
                    pool.append({k: s[k] for k in keys if k in s})
            settings.append({k: s[k] for k in ("cost_adjustment_day", "name") if k in s})
            continue
        conn = claimed[i]
        s.setdefault("cost_adjustment_day", 0.0)
        changes: dict[str, Any] = {}
        for side, want in (("stat_energy_from", conn.stat_energy_from),
                           ("stat_energy_to", conn.stat_energy_to)):
            if want and not s.get(side):
                changes[side] = s.get(side)
                s[side] = want
        if conn.stat_rate and "power_config" not in s and s.get("stat_rate") != conn.stat_rate:
            changes["stat_rate"] = s.get("stat_rate")
            s["stat_rate"] = conn.stat_rate
        kept.append(s)
        added.append(changes)

    for conn in new_conns:
        s = _new_unified(conn)
        _apply_carried(s, import_costs, export_costs, settings)
        kept.append(s)
        added.append({k: None for k in ("stat_energy_from", "stat_energy_to", "stat_rate")
                      if s.get(k)})

    # HA requires each import, export and stat_rate to be unique across grid sources.
    used: dict[str, set[str]] = {k: set() for k in ("stat_energy_from", "stat_energy_to",
                                                    "stat_rate")}
    for s, changes in zip(kept, added):
        for key, values in used.items():
            if key not in changes and s.get(key):
                values.add(s[key])
    for s, changes in zip(kept, added):
        for key, previous in changes.items():
            if s[key] in used[key]:
                if previous is not None:
                    s[key] = previous
                elif key == "stat_rate":
                    del s[key]
                else:
                    s[key] = None
            else:
                used[key].add(s[key])
    for s in kept[len(kept) - len(new_conns):]:
        for side, keys in _SIDES:
            if not s.get(side):
                s.pop(side, None)
                for k in keys:
                    s.pop(k, None)
    return [s for s in kept if _unified_has_refs(s)]


def _merge_legacy(
    grids: list[dict[str, Any]],
    wanted: list[GridConnection],
    skipped: set[str],
) -> list[dict[str, Any]]:
    if grids:
        base = copy.deepcopy(grids[0])
        for extra in grids[1:]:
            base.setdefault("flow_from", []).extend(copy.deepcopy(extra.get("flow_from", [])))
            base.setdefault("flow_to", []).extend(copy.deepcopy(extra.get("flow_to", [])))
    else:
        base = {"type": "grid", "flow_from": [], "flow_to": [], "cost_adjustment_day": 0.0}
    import_costs = [
        {k: f[k] for k in _IMPORT_COST_KEYS if k in f}
        for f in base.get("flow_from", []) or [] if f.get("stat_energy_from") in skipped
    ]
    base["flow_from"] = [
        f for f in base.get("flow_from", []) or [] if f.get("stat_energy_from") not in skipped
    ]
    base["flow_to"] = [
        f for f in base.get("flow_to", []) or [] if f.get("stat_energy_to") not in skipped
    ]
    base.setdefault("cost_adjustment_day", 0.0)
    have_from = {f.get("stat_energy_from") for f in base["flow_from"]}
    have_to = {f.get("stat_energy_to") for f in base["flow_to"]}
    for conn in wanted:
        if conn.stat_energy_from and conn.stat_energy_from not in have_from:
            costs = import_costs.pop(0) if import_costs else None
            if costs and not _is_entity_id(conn.stat_energy_from):
                costs = {k: v for k, v in costs.items() if k not in _IMPORT_PRICE_KEYS}
            base["flow_from"].append(_new_legacy_flow_from(conn.stat_energy_from, costs))
            have_from.add(conn.stat_energy_from)
        if conn.stat_energy_to and conn.stat_energy_to not in have_to:
            base["flow_to"].append({"stat_energy_to": conn.stat_energy_to})
            have_to.add(conn.stat_energy_to)
    if not (base["flow_from"] or base["flow_to"] or base.get("power")):
        return []
    return [base]


def build_grid_sources(wanted: list[GridConnection], fmt: str = UNIFIED) -> list[dict[str, Any]]:
    """New grid sources for the wanted connections, in ``fmt``."""
    return merge_grid_sources([], wanted, set(), fmt)
