"""Device registry.

Device ids are random per instance and devices cannot be created, so a file
item names its device by a ``match`` block: the device's ``identifiers``, or
its ``connections`` when it has none, plus ``manufacturer`` and ``model``
when another device shares one of those pairs (``null`` for one the device
lacks). Only devices holding a user value are exported. A device with neither
identifiers nor connections is not, since nothing could match it, and nor is
one whose match block also fits another live device, since apply could not
tell them apart.
``disabled: true`` stands for ``disabled_by: user``; other ``disabled_by``
values are set by Home Assistant.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from hass_atlas.atlas.domains.base import ExportContext, FlowList, RegistryDomain, Row, has_value

PAIR_FIELDS = ("identifiers", "connections")
TIE_FIELDS = ("manufacturer", "model")
MATCH_FIELDS = (*PAIR_FIELDS, *TIE_FIELDS)

Pair = tuple[Any, ...]


def format_mac(mac: str) -> str:
    """HA's ``device_registry.format_mac``: lowercase colon form, or *mac* unchanged."""
    if len(mac) == 17 and mac.count(":") == 5:
        return mac.lower()
    bare = mac
    if len(mac) == 17 and mac.count("-") == 5:
        bare = mac.replace("-", "")
    elif len(mac) == 14 and mac.count(".") == 2:
        bare = mac.replace(".", "")
    if len(bare) == 12:
        return ":".join(bare.lower()[i : i + 2] for i in range(0, 12, 2))
    return mac


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _is_pair(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 2 and all(map(_scalar, value))


def _pair_sort_key(pair: Pair) -> tuple[tuple[str, str], ...]:
    return tuple((type(v).__name__, str(v)) for v in pair)


def pairs(name: str, value: Any) -> list[Pair]:
    """*value*'s 2-element pairs, sorted and without repeats; mac connections normalized."""
    out: set[Pair] = set()
    for raw in value if isinstance(value, list) else []:
        if not _is_pair(raw):
            continue
        first, second = raw
        if name == "connections" and first == "mac" and isinstance(second, str):
            second = format_mac(second)
        out.add((first, second))
    return sorted(out, key=_pair_sort_key)


def _row_key(name: str, found: list[Pair], row: Row) -> str:
    """The ``item_key`` of a file item that matches *row* on *name* with tie fields."""
    canonical: Row = {name: found, **{k: row.get(k) for k in TIE_FIELDS}}
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))


class DeviceDomain(RegistryDomain):
    @property
    def plan_fields(self) -> tuple[str, ...]:
        return ("name_by_user", "area_id", "labels", "disabled_by")

    def key_error(self, item: Row) -> str | None:
        block = item.get("match")
        if not isinstance(block, dict):
            return "match is missing or not a mapping"
        unknown = sorted(set(block) - set(MATCH_FIELDS))
        if unknown:
            return f"unknown match fields {unknown}"
        given = [name for name in PAIR_FIELDS if name in block]
        if len(given) != 1:
            return "match needs identifiers or connections, not both"
        value = block[given[0]]
        if not (isinstance(value, list) and value and all(map(_is_pair, value))):
            return f"match {given[0]} must be a non-empty list of [domain, id] pairs"
        for name in TIE_FIELDS:
            if name in block and not isinstance(block[name], (str, type(None))):
                return f"match {name} must be a string or null"
        return None

    def match_set(self, item: Row) -> tuple[str, frozenset[Pair]]:
        """Which pair field *item* matches on, and the set of pairs it names."""
        block = item["match"]
        name = "identifiers" if "identifiers" in block else "connections"
        return name, frozenset(pairs(name, block[name]))

    def live_set(self, row: Row, name: str) -> frozenset[Pair]:
        return frozenset(pairs(name, row.get(name)))

    def item_key(self, item: Row) -> str:
        name, found = self.match_set(item)
        block = item["match"]
        canonical: Row = {name: sorted(found, key=_pair_sort_key)}
        canonical.update({k: block[k] for k in TIE_FIELDS if k in block})
        return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))

    def group(self, key: str) -> str:
        block = json.loads(key)
        name = "identifiers" if "identifiers" in block else "connections"
        return str(block[name][0][0])

    def label(self, item: Row, row: Row | None) -> str:
        """A device as plan lines show it: its name and its first identifier or connection."""
        name, found = self.match_set(item)
        first = " ".join(str(v) for v in sorted(found, key=_pair_sort_key)[0])
        if isinstance(item["match"].get("model"), str):
            first += f", {item['match']['model']}"
        names = [item.get("name_by_user")]
        if row is not None:
            names += [row.get("name_by_user"), row.get("name")]
        shown = next((n for n in names if isinstance(n, str) and n), None)
        return f"{shown} ({first})" if shown else f"({first})"

    def export(self, rows: list[Row], ctx: ExportContext) -> list[Row]:
        shared: dict[str, Counter[Pair]] = {name: Counter() for name in PAIR_FIELDS}
        for row in rows:
            for name in PAIR_FIELDS:
                shared[name].update(pairs(name, row.get(name)))
        items = []
        for row in rows:
            fields = self._user_fields(row)
            if not fields:
                continue
            name = "identifiers" if pairs("identifiers", row.get("identifiers")) else "connections"
            found = pairs(name, row.get(name))
            if not found:
                continue
            block: Row = {name: [FlowList(pair) for pair in found]}
            if any(shared[name][pair] > 1 for pair in found):
                block.update({k: row.get(k) for k in TIE_FIELDS})
            items.append({"match": block, **fields})
        # Apply's rule: equal set, then equal tie fields. Count every live row,
        # not only the exported ones, so an export never holds a match that
        # apply would refuse.
        count: Counter[str] = Counter()
        for row in rows:
            for name in PAIR_FIELDS:
                found = pairs(name, row.get(name))
                if found:
                    count[_row_key(name, found, row)] += 1
        warned = set()
        kept = []
        for item in sorted(items, key=self.item_key):
            key = self.item_key(item)
            if count[key] <= 1:
                kept.append(item)
            elif key not in warned:
                warned.add(key)
                ctx.warnings.append(f"{count[key]} devices have the match {key}; none is exported")
        return kept

    def _user_fields(self, row: Row) -> Row:
        fields: Row = {}
        for name in ("name_by_user", "area_id"):
            if has_value(row.get(name)):
                fields[name] = row[name]
        if has_value(row.get("labels")):
            fields["labels"] = sorted(row["labels"])
        if row.get("disabled_by") == "user":
            fields["disabled"] = True
        return fields


DEVICES = DeviceDomain(
    name="devices",
    list_command="config/device_registry/list",
    field_order=("match", "name_by_user", "area_id", "labels", "disabled"),
    set_fields=("labels",),
    depends_on=("areas", "labels"),
    refs=(("area_id", "areas"), ("labels", "labels")),
    live_key_field="id",
    update_key_field="device_id",
)
