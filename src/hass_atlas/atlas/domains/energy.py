"""Energy Dashboard preferences (``energy/get_prefs``).

``energy.yaml`` is a mapping of Home Assistant's three lists. Grid sources are
written in the unified form, one source per connection, whatever form the
live Home Assistant stores; apply converts them back. Entries keep the order
Home Assistant lists them in, which is the dashboard's order.

Entries are sparse, as other domains' items are: a null is not written. Home
Assistant fills a grid source's null keys on save. Apply sends a gas or water
source with its three price keys, null when unset, since HA's cost sensor
reads ``entity_energy_price`` by key (``sensor.py`` ``_get_energy_price``). A battery or grid ``stat_rate`` is not
written when the entry has a ``power_config``, since Home Assistant derives it
from that on every save. ``config_entry_solar_forecast`` is not written: it
holds config entry ids, which a rebuild regenerates. Export leaves out, with
a warning, an empty string in a field other than ``name``, an entry that
would then not load, and a second entry with the key of an earlier one.

The internal form of the file is one item: the mapping itself, holding only
the lists the file has, so a list left out of a hand-written file is told
apart from an empty one.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from typing import Any

from hass_atlas.atlas.domains.base import Client, DocError, ExportContext, FlowDict, Row
from hass_atlas.energy_prefs import UNIFIED, convert_sources, is_legacy_grid
from hass_atlas.ha_client import HAClientError

SOURCES = "energy_sources"
LISTS = (SOURCES, "device_consumption", "device_consumption_water")
SOURCE_TYPES = ("grid", "solar", "battery", "gas", "water")
POWER_FIELDS = ("stat_rate", "stat_rate_inverted", "stat_rate_from", "stat_rate_to")
FORECAST = "config_entry_solar_forecast"

_IMPORT_PRICES = ("stat_cost", "entity_energy_price", "number_energy_price")
_EXPORT_PRICES = ("stat_compensation", "entity_energy_price_export", "number_energy_price_export")
FIELDS: dict[str, tuple[str, ...]] = {
    "grid": (
        "type",
        "stat_energy_from",
        "stat_energy_to",
        "name",
        "stat_rate",
        "power_config",
        "cost_adjustment_day",
        *_IMPORT_PRICES,
        *_EXPORT_PRICES,
    ),
    "solar": ("type", "stat_energy_from", "name", "stat_rate"),
    "battery": (
        "type",
        "stat_energy_from",
        "stat_energy_to",
        "name",
        "stat_rate",
        "power_config",
        "stat_soc",
        "capacity",
    ),
    "gas": ("type", "stat_energy_from", "name", "stat_rate", *_IMPORT_PRICES),
    "water": ("type", "stat_energy_from", "name", "stat_rate", *_IMPORT_PRICES),
}
DEVICE_FIELDS = ("stat_consumption", "name", "stat_rate", "included_in_stat")
# The first Home Assistant release whose schema takes a source field, by source
# type (``components/energy/data.py`` at each release tag); earlier releases
# reject the field as an extra key.
SOURCE_FIELDS_SINCE: dict[str, dict[str, tuple[int, int]]] = {
    "grid": {"name": (2026, 6), "power_config": (2026, 2)},
    "solar": {"name": (2026, 6)},
    "battery": {
        "name": (2026, 6),
        "stat_soc": (2026, 6),
        "capacity": (2026, 8),
        "power_config": (2026, 2),
    },
    "gas": {"name": (2026, 6), "stat_rate": (2026, 3)},
    "water": {"name": (2026, 6), "stat_rate": (2026, 3)},
}
NUMBER_FIELDS = (
    "cost_adjustment_day",
    "number_energy_price",
    "number_energy_price_export",
    "capacity",
)
# Fields that hold a statistic or entity id: an entity id is rewritten to the
# entity's live id, and a ``domain:id`` external statistic is kept as it is.
REF_FIELDS = (
    "stat_energy_from",
    "stat_energy_to",
    "stat_consumption",
    "stat_rate",
    "stat_soc",
    "stat_cost",
    "stat_compensation",
    "entity_energy_price",
    "entity_energy_price_export",
    "included_in_stat",
)
_GRID_SIDES = ("stat_energy_from", "stat_energy_to")
# Home Assistant drops this from a gas source on save (removed in 2022.10).
_DROPPED = {"gas": ("entity_energy_from",)}


def fields(list_name: str, entry: Row) -> tuple[str, ...]:
    """The fields an entry of *list_name* may hold, in file order."""
    if list_name != SOURCES:
        return DEVICE_FIELDS
    return FIELDS.get(str(entry.get("type")), ("type",))


def required(list_name: str, entry: Row) -> tuple[str, ...]:
    """The fields a file cannot clear in an entry."""
    if list_name != SOURCES:
        return ("stat_consumption",)
    source_type = entry.get("type")
    if source_type == "grid":
        return ("type", "cost_adjustment_day")
    if source_type == "battery":
        return ("type", "stat_energy_from", "stat_energy_to")
    return ("type", "stat_energy_from")


def is_entity_ref(value: Any) -> bool:
    """Whether *value* names an entity: it has a dot and no colon."""
    return isinstance(value, str) and "." in value and ":" not in value


def complete(list_name: str, entry: Row) -> Row:
    """*entry* as apply sends it: a gas or water source with all three price keys."""
    if list_name == SOURCES and entry.get("type") in ("gas", "water"):
        return {**entry, **{k: entry.get(k) for k in _IMPORT_PRICES}}
    return entry


def map_refs(entry: Row, rewrite: Callable[[str], str]) -> Row:
    """*entry* with each entity id it references passed through *rewrite*."""
    out = dict(entry)
    for name in REF_FIELDS:
        if is_entity_ref(out.get(name)):
            out[name] = rewrite(out[name])
    config = out.get("power_config")
    if isinstance(config, dict):
        out["power_config"] = {k: rewrite(v) if is_entity_ref(v) else v for k, v in config.items()}
    return out


def entity_refs(entry: Row) -> list[str]:
    """The entity ids *entry* references, in field order."""
    refs = [entry[name] for name in REF_FIELDS if is_entity_ref(entry.get(name))]
    config = entry.get("power_config")
    if isinstance(config, dict):
        refs += [config[k] for k in POWER_FIELDS if is_entity_ref(config.get(k))]
    return refs


def _power_ids(entry: Row) -> list[str]:
    config = entry.get("power_config")
    ids = [config[k] for k in POWER_FIELDS if isinstance(config, dict) and config.get(k)]
    return ids or ([entry["stat_rate"]] if entry.get("stat_rate") else [])


def key(list_name: str, entry: Row) -> str:
    """The identity of *entry* within its list (PLAN section 5)."""
    if list_name != SOURCES:
        return str(entry.get("stat_consumption"))
    kind = str(entry.get("type"))
    if kind == "grid":
        sides = [str(entry.get(side) or "") for side in _GRID_SIDES]
        if not any(sides):
            sides += _power_ids(entry)
        return " ".join([kind, *sides])
    if kind == "battery":
        return f"{kind} {entry.get('stat_energy_from')} {entry.get('stat_energy_to')}"
    return f"{kind} {entry.get('stat_energy_from')}"


def label(list_name: str, entry: Row) -> str:
    """An entry as plan lines show it: its key statistics."""
    if list_name != SOURCES:
        return str(entry.get("stat_consumption"))
    if entry.get("type") in ("grid", "battery"):
        ids = [str(entry.get(side) or "-") for side in _GRID_SIDES]
        if entry.get("type") == "grid" and not any(entry.get(s) for s in _GRID_SIDES):
            ids = _power_ids(entry)
        return " / ".join(ids)
    return str(entry.get("stat_energy_from"))


def kind(list_name: str, entry: Row) -> str:
    """What plan lines call an entry: its list, or ``<type> source``."""
    if list_name != SOURCES:
        return list_name
    return f"{entry.get('type')} source"


def _number(value: Any) -> Any:
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def normalize(entry: Row) -> Row:
    """*entry* as Home Assistant stores it, for comparison.

    A null field equals an absent one; numbers are floats (``Coerce(float)``);
    a battery or grid ``stat_rate`` is dropped when ``power_config`` is set,
    since HA derives it; a gas ``entity_energy_from`` is dropped, as HA drops it.
    """
    out = {k: _number(v) if k in NUMBER_FIELDS else v for k, v in entry.items() if v is not None}
    if isinstance(out.get("power_config"), dict):
        out.pop("stat_rate", None)
    for name in _DROPPED.get(str(out.get("type")), ()):
        out.pop(name, None)
    return out


def live_list(prefs: Row, list_name: str) -> list[Row]:
    """*list_name* of *prefs* as stored, with grid sources in the unified form."""
    entries = prefs.get(list_name) or []
    if list_name == SOURCES:
        return convert_sources(entries, UNIFIED)
    return copy.deepcopy(entries)


def canonical(prefs: Row, list_name: str) -> list[Row]:
    """*list_name* of *prefs* in the form two prefs are compared in."""
    return [normalize(entry) for entry in live_list(prefs, list_name)]


def _check_type(name: str, value: Any) -> str | None:
    if name in NUMBER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} must be a number"
        if not math.isfinite(value):
            return f"{name} must be a finite number"
        if name == "capacity" and value <= 0:
            return "capacity must be greater than 0"
        return None
    if name == "power_config":
        return _power_config_error(value)
    if name == "name":  # Home Assistant takes any string as a name
        return None if isinstance(value, str) else "name must be a string"
    if not isinstance(value, str) or not value:
        return f"{name} must be a non-empty string"
    return None


def _power_config_error(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return "power_config must be a mapping"
    unknown = sorted(set(value) - set(POWER_FIELDS))
    if unknown:
        return f"power_config has unknown fields {unknown}"
    if not all(isinstance(v, str) and v for v in value.values()):
        return "power_config values must be non-empty strings"
    methods = [
        "stat_rate" in value,
        "stat_rate_inverted" in value,
        "stat_rate_from" in value or "stat_rate_to" in value,
    ]
    if sum(methods) != 1:
        return "power_config needs one of stat_rate, stat_rate_inverted, or stat_rate_from and stat_rate_to"
    if ("stat_rate_from" in value) != ("stat_rate_to" in value):
        return "power_config needs both stat_rate_from and stat_rate_to"
    return None


def entry_error(list_name: str, entry: Any) -> str | None:
    """Why *entry* is not a valid file entry of *list_name*, or None."""
    if not isinstance(entry, dict):
        return "expected a mapping"
    if list_name == SOURCES:
        if entry.get("type") not in SOURCE_TYPES:
            return f"type must be one of {', '.join(SOURCE_TYPES)}"
        if is_legacy_grid(entry) or "power" in entry:
            return "write a grid source in the unified form, one source per connection"
        if FORECAST in entry:
            return f"{FORECAST} holds config entry ids; leave it out"
    allowed = fields(list_name, entry)
    unknown = sorted(set(entry) - set(allowed))
    if unknown:
        return f"unknown fields {unknown}"
    # A grid source without cost_adjustment_day is created with 0.
    for name in required(list_name, entry):
        if name not in entry and name != "cost_adjustment_day":
            return f"{name} is required"
    for name, value in entry.items():
        if value is None:
            if name in required(list_name, entry) or name in _GRID_SIDES:
                return f"{name} cannot be null"
            continue
        if name == "type":
            continue
        if error := _check_type(name, value):
            return error
    if "power_config" in entry and "stat_rate" in entry:
        return "stat_rate is derived from power_config; leave it out"
    if entry.get("type") == "grid" and not any(
        entry.get(k) for k in (*_GRID_SIDES, "stat_rate", "power_config")
    ):
        return "a grid source needs stat_energy_from, stat_energy_to or a power statistic"
    return None


def version_error(list_name: str, entry: Row, version: tuple[int, int] | None) -> str | None:
    """Why Home Assistant *version* would reject a field *entry* sets, or None."""
    if version is None or list_name != SOURCES:
        return None
    since = SOURCE_FIELDS_SINCE.get(str(entry.get("type")), {})
    for name in fields(list_name, entry):
        first = since.get(name)
        if first is not None and version < first and entry.get(name) is not None:
            return f"{name} needs Home Assistant {first[0]}.{first[1]} or later"
    return None


def merged_error(list_name: str, entry: Row) -> str | None:
    """Why Home Assistant would reject *entry*, as apply would send it, or None."""
    if list_name != SOURCES:
        return None
    source_type = entry.get("type")
    if source_type == "grid":
        for entity, number in (
            ("entity_energy_price", "number_energy_price"),
            ("entity_energy_price_export", "number_energy_price_export"),
        ):
            if entry.get(entity) is not None and entry.get(number) is not None:
                return f"both {entity} and {number} would be set; set one of them to null"
    sides = [("stat_energy_from", "stat_cost", "entity_energy_price", "number_energy_price")]
    if source_type == "grid":
        sides.append(
            (
                "stat_energy_to",
                "stat_compensation",
                "entity_energy_price_export",
                "number_energy_price_export",
            )
        )
    if source_type in ("grid", "gas", "water"):
        for stat, cost, entity, number in sides:
            value = entry.get(stat)
            external = isinstance(value, str) and not is_entity_ref(value)
            if external and entry.get(cost) is None:
                if entry.get(entity) is not None or entry.get(number) is not None:
                    return f"{stat} {value} is an external statistic; set {cost} for its price"
    return None


class EnergyDomain:
    name = "energy"
    filename = "energy.yaml"
    depends_on: tuple[str, ...] = ("entities",)
    field_order = LISTS

    async def fetch(self, client: Client) -> list[Row]:
        """``[prefs]``, or ``[]`` when the dashboard was never saved (HA's ``not_found``)."""
        try:
            prefs = await client.send_command("energy/get_prefs")
        except HAClientError as err:
            if err.code == "not_found":
                return []
            raise
        return [prefs] if isinstance(prefs, dict) else []

    def export(self, rows: list[Row], ctx: ExportContext) -> list[Row]:
        prefs = rows[0] if rows else {}
        doc: Row = {}
        for list_name in LISTS:
            entries = []
            seen: set[str] = set()
            for entry in live_list(prefs, list_name):
                where = f"{kind(list_name, entry)} {label(list_name, entry)}"
                out = self._export_entry(list_name, entry, where, ctx)
                if error := entry_error(list_name, out):
                    ctx.warnings.append(f"{where}: not exported: {error}")
                    continue
                ident = key(list_name, out)
                if ident in seen:
                    ctx.warnings.append(f"{where}: a second entry with this key is not exported")
                    continue
                seen.add(ident)
                entries.append(out)
            doc[list_name] = entries
        return [doc]

    def _export_entry(self, list_name: str, entry: Row, where: str, ctx: ExportContext) -> Row:
        if entry.get(FORECAST):
            ctx.warnings.append(
                f"{where}: {FORECAST} is not exported; its config entry ids differ per installation"
            )
        allowed = fields(list_name, entry)
        dropped = (FORECAST, *_DROPPED.get(str(entry.get("type")), ()))
        for name in sorted(set(entry) - set(allowed) - set(dropped)):
            if entry[name] is not None:
                ctx.warnings.append(f"{where}: {name} is not exported (unknown to hass-atlas)")
        out = {}
        for name in allowed:
            value = entry.get(name)
            if value == "" and name != "name":
                ctx.warnings.append(f"{where}: {name} is empty and is not exported")
            elif value is not None:
                out[name] = value
        if isinstance(out.get("power_config"), dict):
            out.pop("stat_rate", None)
        return out

    def to_doc(self, items: list[Row]) -> Row:
        doc = items[0] if items else {name: [] for name in LISTS}
        out: Row = {}
        for list_name in LISTS:
            if list_name not in doc:
                continue
            entries = []
            for entry in doc[list_name]:
                ordered = {name: entry[name] for name in fields(list_name, entry) if name in entry}
                unknown = set(entry) - set(ordered)
                if unknown:
                    raise ValueError(f"fields not in field order: {sorted(unknown)}")
                config = ordered.get("power_config")
                if isinstance(config, dict):
                    ordered["power_config"] = FlowDict(
                        (k, config[k]) for k in POWER_FIELDS if k in config
                    )
                entries.append(ordered)
            out[list_name] = entries
        return out

    def from_doc(self, doc: Any) -> list[Row]:
        if not isinstance(doc, dict):
            raise DocError(f"expected a mapping of {', '.join(LISTS)}")
        unknown = sorted(set(doc) - set(LISTS), key=str)
        if unknown:
            raise DocError(f"unknown lists {unknown}")
        for list_name, entries in doc.items():
            if not isinstance(entries, list):
                raise DocError(f"{list_name} must be a list")
            seen: set[str] = set()
            for index, entry in enumerate(entries):
                if error := entry_error(list_name, entry):
                    raise DocError(f"{list_name} item {index}: {error}")
                ident = key(list_name, entry)
                if ident in seen:
                    raise DocError(f"{list_name} item {index}: duplicate entry {ident}")
                seen.add(ident)
        return [doc]

    def count(self, items: list[Row]) -> int:
        return sum(len(entries) for doc in items for entries in doc.values())


ENERGY = EnergyDomain()
