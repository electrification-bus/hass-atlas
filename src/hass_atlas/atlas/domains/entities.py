"""Entity registry.

Every entity is exported (``entities: all`` in the manifest), so a rebuilt
Home Assistant gets every entity_id back. Entities cannot be created, so a
file item names its entity by a ``match`` block: ``platform`` and
``unique_id``, or ``platform`` and ``entity_id`` when the unique_id must not
be written: on ``mobile_app``, whose unique_ids embed the webhook id, and
when the unique_id holds the entity's config entry or config subentry id, as
config-entry helpers' and subentry-based conversation agents' do. Such an
entity_id is not renamed. HA keys entities by domain, platform and
unique_id, so the domain of the item's ``entity_id`` is part of the match.

Only user-set fields are written: ``name``, ``icon`` and ``device_class``
(Home Assistant keeps its own in ``original_*``), ``area_id``, ``labels``,
``aliases`` when they differ from the default ``[null]`` (``null`` stands
for the entity's computed name), ``hidden`` and ``disabled`` for
``hidden_by`` and ``disabled_by: user``, and the user keys of ``options`` in
:data:`USER_OPTIONS`. ``categories`` are not written, since category ids are
random per installation and apply does not create categories; a file can
still set them by id.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from hass_atlas.atlas.domains.base import (
    Client,
    ExportContext,
    FlowDict,
    RegistryDomain,
    Row,
    has_value,
)

# The option keys a user sets in the entity settings dialog. Home Assistant
# writes other keys into the same domains (sensor.suggested_display_precision)
# and its own domains (sensor.private), which are not exported.
USER_OPTIONS: dict[str, tuple[str, ...]] = {
    "sensor": ("display_precision", "unit_of_measurement"),
    "weather": (
        "precipitation_unit",
        "pressure_unit",
        "temperature_unit",
        "visibility_unit",
        "wind_speed_unit",
    ),
}

# homeassistant.core.VALID_ENTITY_ID
_OBJECT_ID = r"(?!_)[\da-z_]+(?<!_)"
VALID_ENTITY_ID = re.compile(r"(?!.+__)" + _OBJECT_ID + r"\." + _OBJECT_ID)
_SCOPE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")

MATCH_FIELDS = ("platform", "unique_id", "entity_id")
GET_ENTRIES = "config/entity_registry/get_entries"
# Ids per get_entries command; its replies stay far below the client's frame limit.
BATCH = 500

_PLAIN_FIELDS = ("name", "icon", "area_id", "device_class")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _unique_id(row: Row) -> str | None:
    value = row.get("unique_id")
    return None if value is None or value == "" else str(value)


# The random ids a rebuild regenerates, which a unique_id may hold.
_ENTRY_IDS = ("config_entry_id", "config_subentry_id")


def unstable_key(row: Row) -> bool:
    """Whether *row* is matched by entity_id: its unique_id must not be written."""
    unique_id = _unique_id(row)
    if unique_id is None or row.get("platform") == "mobile_app":
        return True
    entries = [row.get(name) for name in _ENTRY_IDS]
    return any(isinstance(e, str) and e != "" and e in unique_id for e in entries)


def _leaks(row: Row) -> bool:
    """Whether *row*'s entity_id holds its unique_id or config entry or subentry id.

    HA builds ``<platform>_<unique_id>`` when an entity has no name, so an
    unstable unique_id can end up in the entity_id itself.
    """
    object_id = str(row.get("entity_id", "")).partition(".")[2]
    secrets = [_unique_id(row), *(row.get(name) for name in _ENTRY_IDS)]
    return any(isinstance(s, str) and _slug(s) and _slug(s) in object_id for s in secrets)


def option_error(value: Any) -> str | None:
    """Why *value* is not a valid ``options`` field, or None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return "options must be a mapping of domain to options"
    for domain, keys in value.items():
        if domain not in USER_OPTIONS:
            return f"options {domain} is not one of {', '.join(USER_OPTIONS)}"
        if not isinstance(keys, dict):
            return f"options {domain} must be a mapping"
        for key, option in keys.items():
            if key not in USER_OPTIONS[domain]:
                return f"options {domain} {key} is not one of {', '.join(USER_OPTIONS[domain])}"
            if option is None:
                continue
            if key == "display_precision":
                if not isinstance(option, int) or isinstance(option, bool):
                    return f"options {domain} {key} must be a whole number"
            elif not isinstance(option, str):
                return f"options {domain} {key} must be a string"
    return None


def _merge(current: Any, values: dict[str, Any]) -> dict[str, Any]:
    """*current* with *values* set, a None value removing its key."""
    out = dict(current) if isinstance(current, dict) else {}
    for key, value in values.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


class EntityDomain(RegistryDomain):
    @property
    def plan_fields(self) -> tuple[str, ...]:
        return (
            "entity_id",
            "name",
            "icon",
            "area_id",
            "device_class",
            "labels",
            "categories",
            "aliases",
            "hidden_by",
            "disabled_by",
            "options",
        )

    @property
    def kind(self) -> str:
        return "entity"

    def shown(self, values: Row) -> list[tuple[str, Any]]:
        """Plan line fields, with one ``categories.<scope>`` and ``options.<domain>.<key>`` each."""
        out: list[tuple[str, Any]] = []
        for name, value in super().shown(values):
            if name == "categories" and isinstance(value, dict):
                out += [(f"categories.{scope}", v) for scope, v in value.items()]
            elif name == "options" and isinstance(value, dict):
                out += [
                    (f"options.{domain}.{key}", v)
                    for domain, keys in value.items()
                    if isinstance(keys, dict)
                    for key, v in keys.items()
                ]
            else:
                out.append((name, value))
        return out

    def key_error(self, item: Row) -> str | None:
        block = item.get("match")
        if not isinstance(block, dict):
            return "match is missing or not a mapping"
        unknown = sorted(set(block) - set(MATCH_FIELDS))
        if unknown:
            return f"unknown match fields {unknown}"
        if not isinstance(block.get("platform"), str) or not block["platform"]:
            return "match platform is missing or not a string"
        given = [name for name in ("unique_id", "entity_id") if name in block]
        if len(given) != 1:
            return "match needs unique_id or entity_id, not both"
        if not isinstance(block[given[0]], str) or not block[given[0]]:
            return f"match {given[0]} must be a string"
        if given == ["entity_id"]:
            if not VALID_ENTITY_ID.fullmatch(block["entity_id"]):
                return f"match entity_id {block['entity_id']} is not a valid entity id"
            if "entity_id" in item:
                return "an item matched by entity_id cannot set entity_id"
        return None

    def item_key(self, item: Row) -> str:
        block = item["match"]
        canonical: Row = {"platform": block["platform"]}
        if "unique_id" in block:
            canonical["unique_id"] = block["unique_id"]
            entity_id = item.get("entity_id")
            if isinstance(entity_id, str) and "." in entity_id:
                canonical["domain"] = entity_id.partition(".")[0]
        else:
            canonical["entity_id"] = block["entity_id"]
        return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))

    def row_key(self, row: Row) -> str:
        """The ``item_key`` of the file item export writes for *row*."""
        canonical: Row = {"platform": row.get("platform")}
        if unstable_key(row):
            canonical["entity_id"] = row["entity_id"]
        else:
            canonical["unique_id"] = _unique_id(row)
            canonical["domain"] = str(row["entity_id"]).partition(".")[0]
        return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))

    def group(self, key: str) -> str:
        return str(json.loads(key)["platform"])

    def label(self, item: Row, row: Row | None) -> str:
        """An entity as plan lines show it: its entity_id in the file, else in HA."""
        block = item["match"]
        for value in (block.get("entity_id"), item.get("entity_id")):
            if isinstance(value, str):
                return value
        if row is not None:
            return str(row["entity_id"])
        return f"({block['platform']} {block['unique_id']})"

    def file_entity_id(self, item: Row) -> str | None:
        """The entity_id the file gives *item*, which other domains refer to it by."""
        value = item["match"].get("entity_id", item.get("entity_id"))
        return value if isinstance(value, str) else None

    def field_error(self, name: str, value: Any) -> str | None:
        """Why a file value is not valid for field *name*, or None."""
        if name == "entity_id":
            if not isinstance(value, str) or not VALID_ENTITY_ID.fullmatch(value):
                return f"entity_id {value!r} is not a valid entity id"
        elif name in _PLAIN_FIELDS:
            if value is not None and not isinstance(value, str):
                return f"{name} must be a string"
        elif name == "labels":
            if value is not None and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                return "labels must be a list of strings"
        elif name == "aliases":
            if value is not None and not (
                isinstance(value, list) and all(v is None or isinstance(v, str) for v in value)
            ):
                return "aliases must be a list of strings and nulls"
        elif name == "categories":
            if value is None:
                return None
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and _SCOPE.fullmatch(k) for k in value
            ):
                return "categories must be a mapping from scope to category id"
            if not all(v is None or isinstance(v, str) for v in value.values()):
                return "categories values must be category ids or null"
        elif name in ("hidden", "disabled"):
            if value is not None and not isinstance(value, bool):
                return f"{name} must be true or false"
        elif name == "options":
            return option_error(value)
        return None

    def desired(self, item: Row) -> Row:
        """*item*'s fields as the registry names them: ``hidden`` is ``hidden_by``."""
        out: Row = {}
        for name in self.field_order[1:]:
            if name not in item:
                continue
            value = item[name]
            if name in ("hidden", "disabled"):
                out[f"{name}_by"] = "user" if value else None
            else:
                out[name] = value
        return out

    def comparable(self, name: str, value: Any) -> Any:
        if name == "aliases":
            if value is None:
                return [None]
            items = value if isinstance(value, list) else []
            return [
                v.strip() if isinstance(v, str) else None
                for v in items
                if v is None or (isinstance(v, str) and v.strip())
            ]
        return super().comparable(name, value)

    def live_value(self, row: Row, name: str, wanted: Any) -> Any:
        if name == "categories" and isinstance(wanted, dict):
            live = row.get("categories") or {}
            return {scope: live.get(scope) for scope in wanted}
        if name == "options" and isinstance(wanted, dict):
            live = row.get("options") or {}
            return {
                domain: {key: (live.get(domain) or {}).get(key) for key in keys}
                for domain, keys in wanted.items()
                if isinstance(keys, dict)
            }
        return row.get(name)

    def stored(self, row: Row, name: str, value: Any) -> Any:
        if name == "categories":
            return _merge(row.get("categories"), value or {})
        if name == "options":
            options = dict(row.get("options") or {})
            for domain, keys in (value or {}).items():
                options[domain] = _merge(options.get(domain), keys)
            return options
        if name == "aliases":
            return self.comparable(name, value)
        return super().stored(row, name, value)

    def explicit(self, row: Row, desired: Row) -> Row:
        """*desired* with a null ``categories`` or ``options`` spelled out per key.

        Null categories remove every scope the entity has; null options remove
        every user key it has.
        """
        out = dict(desired)
        if "categories" in out and out["categories"] is None:
            out["categories"] = {scope: None for scope in sorted(row.get("categories") or {})}
        if "options" in out and out["options"] is None:
            live = row.get("options") or {}
            out["options"] = {
                domain: {key: None for key in keys if key in (live.get(domain) or {})}
                for domain, keys in USER_OPTIONS.items()
                if any(key in (live.get(domain) or {}) for key in keys)
            }
        return out

    def update_payloads(self, live_id: str, row: Row | None, values: Row) -> list[Row]:
        """One command for the plain fields, and one per options domain.

        Each options command sends the domain's live options with the file's
        keys merged in, since HA replaces a domain's options whole.
        """
        fields: Row = {}
        for name, value in values.items():
            if name == "options":
                continue
            if name == "labels" and value is None:
                value = []
            elif name == "aliases" and value is None:
                value = [None]
            fields[name] = value
        payloads = [{"entity_id": live_id, **fields}] if fields else []
        live = (row or {}).get("options") or {}
        for domain, keys in sorted((values.get("options") or {}).items()):
            merged = _merge(live.get(domain), keys)
            payloads.append({"entity_id": live_id, "options_domain": domain, "options": merged})
        return payloads

    async def fetch(self, client: Client) -> list[Row]:
        """``list`` rows with ``aliases`` and ``device_class`` from ``get_entries``.

        ``list`` leaves those two out. An entity removed between the two
        commands is left out.
        """
        rows: list[Row] = await client.send_command(self.list_command)
        ids = [row["entity_id"] for row in rows]
        extended: dict[str, Any] = {}
        for start in range(0, len(ids), BATCH):
            extended.update(
                await client.send_command(GET_ENTRIES, entity_ids=ids[start : start + BATCH])
            )
        out = []
        for row in rows:
            entry = extended.get(row["entity_id"])
            if isinstance(entry, dict):
                out.append(
                    {
                        **row,
                        "aliases": entry.get("aliases"),
                        "device_class": entry.get("device_class"),
                    }
                )
        return out

    def export(self, rows: list[Row], ctx: ExportContext) -> list[Row]:
        count = Counter(self.row_key(row) for row in rows)
        items = []
        for row in rows:
            key = self.row_key(row)
            if count[key] > 1:
                ctx.warnings.append(
                    f"{count[key]} entities have the match {key}; none is exported"
                )
                continue
            if unstable_key(row):
                if _leaks(row):
                    name = row.get("name") or row.get("original_name") or "with no name"
                    ctx.warnings.append(
                        f"a {row.get('platform')} entity ({name}) has an entity_id built from its "
                        "unique_id or config entry or subentry id; it is not exported"
                    )
                    continue
                item: Row = {
                    "match": FlowDict(platform=row["platform"], entity_id=row["entity_id"])
                }
            else:
                block = FlowDict(platform=row["platform"], unique_id=_unique_id(row))
                item = {"match": block, "entity_id": row["entity_id"]}
            item.update(self._user_fields(row, ctx))
            items.append(item)
        items.sort(key=self.item_key)
        return items

    def _user_fields(self, row: Row, ctx: ExportContext) -> Row:
        fields: Row = {}
        for name in _PLAIN_FIELDS:
            if has_value(row.get(name)) and isinstance(row[name], str):
                fields[name] = row[name]
        labels = row.get("labels")
        if isinstance(labels, list) and labels:
            fields["labels"] = sorted(labels)
        aliases = row.get("aliases")
        if isinstance(aliases, list) and aliases != [None]:
            fields["aliases"] = list(aliases)
        if row.get("hidden_by") == "user":
            fields["hidden"] = True
        if row.get("disabled_by") == "user":
            fields["disabled"] = True
        options = self._user_options(row, ctx)
        if options:
            fields["options"] = options
        return fields

    def _user_options(self, row: Row, ctx: ExportContext) -> Row:
        live = row.get("options")
        live = live if isinstance(live, dict) else {}
        out: Row = {}
        for domain, keys in USER_OPTIONS.items():
            values = live.get(domain)
            values = values if isinstance(values, dict) else {}
            kept = FlowDict()
            for key in keys:
                value = values.get(key)
                if value is None:
                    continue
                if option_error({domain: {key: value}}):
                    ctx.warnings.append(
                        f"{row['entity_id']}: options {domain} {key} holds {value!r}; not exported"
                    )
                    continue
                kept[key] = value
            if kept:
                out[domain] = kept
        return out


ENTITIES = EntityDomain(
    name="entities",
    list_command="config/entity_registry/list",
    field_order=(
        "match",
        "entity_id",
        "name",
        "icon",
        "area_id",
        "device_class",
        "labels",
        "categories",
        "aliases",
        "hidden",
        "disabled",
        "options",
    ),
    set_fields=("labels",),
    depends_on=("areas", "labels", "devices"),
    refs=(("area_id", "areas"), ("labels", "labels")),
    live_key_field="entity_id",
    update_key_field="entity_id",
)
