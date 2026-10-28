"""Stateful in-memory Home Assistant for atlas tests.

Floor, label and area commands behave as in HA 2026.10.4
(``homeassistant/components/config/{floor,label,area}_registry.py`` and
``homeassistant/helpers/{normalized_name_base_registry,area_registry}.py``):
ids come from the slug of the name with ``_2``, ``_3`` on a clash; names are
unique after casefolding and removing spaces; aliases are stripped and empty
ones dropped; area labels that do not exist are dropped; area floors are not
checked; a changed area temperature or humidity sensor is checked against the
states; a label icon needs a ``:`` and a label color is ``#RRGGBB`` or a
theme color; an update of an unknown id fails with ``unknown_error``.

Device updates follow ``components/config/device_registry.py`` and
``helpers/device_registry.py``: ``area_id`` and ``name_by_user`` take null,
``labels`` does not, ``disabled_by`` is ``user`` or null; unknown labels are
dropped and the area is not checked; an unknown ``device_id`` fails with
``not_found``; an equal value changes nothing; disabling a device gives its
enabled child devices ``disabled_by: device`` and enabling it clears that; an
enable is ignored while the device's config entry (in ``disabled_entries``)
is disabled, and an enabled child under a disabled parent stays ``device``.
A device whose disable changes, other than to ``config_entry``, gives its
enabled entities ``disabled_by: device`` or clears that.

Entity commands follow ``components/config/entity_registry.py`` and
``helpers/entity_registry.py``: ``list`` gives the partial rows, without
``aliases`` and ``device_class`` (a fixture's ``original_name`` stands for
the unprefixed one ``list`` gives), and ``get_entries`` the extended rows.
``update`` keeps a null alias (the computed name) and strips the others,
drops unknown labels without checking the area, merges ``categories`` per
scope, refuses an enable under a disabled device ("Device is disabled"), a
taken, invalid or other-domain ``new_entity_id`` and an own area without an
own name (taking ``name``, else ``original_name``, as the unprefixed name),
all as ``invalid_info``; ``options_domain`` replaces that domain's options
whole; the reply is ``{entity_entry: ...}``, with ``require_restart`` (no
config entry) or ``reload_delay`` when the message enables the entity. A
rename moves the entity's state. ``config/category_registry/list`` lists one
scope's categories.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from slugify import slugify as _slugify

from hass_atlas.ha_client import HAClientError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "atlas"

_LISTS = {
    "config/floor_registry/list": "floors",
    "config/label_registry/list": "labels",
    "config/area_registry/list": "areas",
    "config/device_registry/list": "devices",
}

_KEYS = {
    "floors": "floor_id",
    "labels": "label_id",
    "areas": "area_id",
    "devices": "id",
    "entities": "entity_id",
}

DEVICE_UPDATE = "config/device_registry/update"
ENTITY_LIST = "config/entity_registry/list"
ENTITY_GET = "config/entity_registry/get_entries"
ENTITY_UPDATE = "config/entity_registry/update"
CATEGORY_LIST = "config/category_registry/list"

# RegistryEntry.as_partial_dict, and what extended_dict adds to it.
PARTIAL_FIELDS = (
    "area_id",
    "categories",
    "config_entry_id",
    "config_subentry_id",
    "created_at",
    "device_id",
    "disabled_by",
    "entity_category",
    "entity_id",
    "has_entity_name",
    "hidden_by",
    "icon",
    "id",
    "labels",
    "modified_at",
    "name",
    "next_name_part",
    "options",
    "original_name",
    "platform",
    "translation_key",
    "unique_id",
)
EXTENDED_FIELDS = (
    *PARTIAL_FIELDS,
    "aliases",
    "capabilities",
    "device_class",
    "original_device_class",
    "original_icon",
)
# The user values a rebuilt Home Assistant does not have, as a new entity holds them.
_ENTITY_DEFAULTS: dict[str, Any] = {
    "name": None,
    "icon": None,
    "area_id": None,
    "device_class": None,
    "labels": [],
    "categories": {},
    "aliases": [None],
}
_USER_OPTIONS = {
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
_VALID_ENTITY_ID = re.compile(r"(?!.+__)" + _OBJECT_ID + r"\." + _OBJECT_ID)
_SLUG_KEY = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
RELOAD_DELAY = 30

# Optional fields per write command, and whether each accepts null.
_SCHEMAS: dict[str, dict[str, bool]] = {
    "config/floor_registry/create": {"aliases": False, "icon": True, "level": True},
    "config/floor_registry/update": {
        "aliases": False,
        "icon": True,
        "level": True,
        "name": False,
    },
    "config/label_registry/create": {"color": True, "description": True, "icon": True},
    "config/label_registry/update": {
        "color": True,
        "description": True,
        "icon": True,
        "name": False,
    },
    "config/area_registry/create": {
        "aliases": False,
        "floor_id": False,
        "humidity_entity_id": True,
        "icon": False,
        "labels": False,
        "picture": True,
        "temperature_entity_id": True,
    },
    "config/area_registry/update": {
        "aliases": False,
        "floor_id": True,
        "humidity_entity_id": True,
        "icon": True,
        "labels": False,
        "name": False,
        "picture": True,
        "temperature_entity_id": True,
    },
    DEVICE_UPDATE: {"area_id": True, "disabled_by": True, "labels": False, "name_by_user": True},
}

# A label color is #RRGGBB or one of these (``SUPPORTED_LABEL_THEME_COLORS``).
_LABEL_THEME_COLORS = frozenset(
    "primary accent disabled red pink purple deep-purple indigo blue light-blue cyan teal "
    "green light-green lime yellow amber orange deep-orange brown light-grey grey dark-grey "
    "blue-grey black white".split()
)
_COLOR_HEX = re.compile(r"#[0-9A-Fa-f]{6}")

_FIELDS = {
    "floors": ("aliases", "floor_id", "icon", "level", "name"),
    "labels": ("color", "description", "icon", "label_id", "name"),
    "areas": (
        "aliases",
        "area_id",
        "floor_id",
        "humidity_entity_id",
        "icon",
        "labels",
        "name",
        "picture",
        "temperature_entity_id",
    ),
}


def load_fixture(name: str = "small") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _new_id(old: str) -> str:
    return hashlib.sha256(f"rebuilt {old}".encode()).hexdigest()[:32]


def rebuilt_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*devices* as a rebuilt Home Assistant has them.

    Device and config entry ids are new, and the user's names, areas, labels
    and disables are gone; a disable set by HA itself is kept.
    """
    out = []
    for device in copy.deepcopy(devices):
        for name in ("id", "config_entry_id", "parent_device_id", "via_device_id"):
            if device.get(name) is not None:
                device[name] = _new_id(device[name])
        if "config_entries" in device:
            device["config_entries"] = [_new_id(e) for e in device["config_entries"]]
        if "primary_config_entry" in device:
            device["primary_config_entry"] = device["config_entry_id"]
        device.update(name_by_user=None, area_id=None, labels=[])
        if device["disabled_by"] in ("user", "device"):
            device["disabled_by"] = None
        out.append(device)
    return out


def rebuilt_entities(data: dict[str, Any]) -> list[dict[str, Any]]:
    """*data*'s entities as a rebuilt Home Assistant has them.

    Registry, device, config entry and subentry ids are new, and so is a
    unique_id that holds the webhook id (``mobile_app``) or one of those
    config ids. The
    entity_id is the one in *data*'s ``rebuilt_entity_ids``, or kept. User
    values are gone, as are user option keys, a user hide and a user or
    device disable.
    """
    renamed = data.get("rebuilt_entity_ids", {})
    out = []
    for row in copy.deepcopy(data.get("entities", [])):
        if row["platform"] == "mobile_app":
            row["unique_id"] = _new_id(row["unique_id"])
        for name in ("config_entry_id", "config_subentry_id"):
            entry = row.get(name)
            if entry is not None and entry in row["unique_id"]:
                row["unique_id"] = row["unique_id"].replace(entry, _new_id(entry))
        for name in ("id", "device_id", "config_entry_id", "config_subentry_id"):
            if row.get(name) is not None:
                row[name] = _new_id(row[name])
        row["entity_id"] = renamed.get(row["entity_id"], row["entity_id"])
        row.update(copy.deepcopy(_ENTITY_DEFAULTS))
        if row["hidden_by"] == "user":
            row["hidden_by"] = None
        if row["disabled_by"] in ("user", "device"):
            row["disabled_by"] = None
        for domain, keys in _USER_OPTIONS.items():
            if domain in row["options"]:
                row["options"][domain] = {
                    k: v for k, v in row["options"][domain].items() if k not in keys
                }
        out.append(row)
    return out


def empty_fixture(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """A rebuilt Home Assistant: no floors, labels or areas, *data*'s devices and
    entities with new ids and no user values (see :func:`rebuilt_devices` and
    :func:`rebuilt_entities`), and *data*'s states under the rebuilt entity_ids.

    *data*'s categories are kept with new ids, as on a real rebuild.
    """
    data = data if data is not None else load_fixture()
    renamed = data.get("rebuilt_entity_ids", {})
    states = copy.deepcopy(data.get("states", []))
    for state in states:
        state["entity_id"] = renamed.get(state["entity_id"], state["entity_id"])
    return {
        "ha_version": data.get("ha_version"),
        "devices": rebuilt_devices(data.get("devices", [])),
        "entities": rebuilt_entities(data),
        "categories": {
            scope: [{**row, "category_id": _new_id(row["category_id"])} for row in rows]
            for scope, rows in data.get("categories", {}).items()
        },
        "disabled_entries": [_new_id(e) for e in data.get("disabled_entries", [])],
        "states": states,
    }


def slugify(text: str) -> str:
    """HA's ``homeassistant.util.slugify``, with the python-slugify version HA pins."""
    return _slugify(text, separator="_") or "unknown"


def normalize_name(name: str) -> str:
    return name.casefold().replace(" ", "")


Hook = Callable[["FakeHA", dict[str, Any]], None]


class FakeHA:
    """Answers registry commands from its state and records every command.

    ``fail`` maps a command type to the error it raises before touching the
    state. ``before`` and ``after`` map a command type to hooks run around it,
    to simulate someone else editing HA during a run. Any command it does not
    know fails the test.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = copy.deepcopy(data if data is not None else load_fixture())
        self.ha_version: str | None = data.get("ha_version")
        self.registries: dict[str, list[dict[str, Any]]] = {
            name: data.get(name, []) for name in _LISTS.values()
        }
        self.registries["entities"] = data.get("entities", [])
        self.categories: dict[str, list[dict[str, Any]]] = data.get("categories", {})
        self.states: list[dict[str, Any]] = data.get("states", [])
        self.disabled_entries: set[str] = set(data.get("disabled_entries", []))
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, HAClientError] = {}
        self.before: dict[str, list[Hook]] = {}
        self.after: dict[str, list[Hook]] = {}
        self._clock = 1770000000.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(cmd, msg) for cmd, msg in self.sent if cmd in _SCHEMAS or cmd == ENTITY_UPDATE]

    async def send_command(self, msg_type: str, **kwargs: Any) -> Any:
        self.sent.append((msg_type, kwargs))
        for hook in self.before.get(msg_type, []):
            hook(self, kwargs)
        if msg_type in self.fail:
            raise self.fail[msg_type]
        result = self._handle(msg_type, kwargs)
        for hook in self.after.get(msg_type, []):
            hook(self, kwargs)
        return result

    def _handle(self, msg_type: str, msg: dict[str, Any]) -> Any:
        if msg_type in _LISTS:
            return copy.deepcopy(self.registries[_LISTS[msg_type]])
        if msg_type == "get_states":
            return copy.deepcopy(self.states)
        if msg_type == ENTITY_LIST:
            return [self._entity_dict(row, PARTIAL_FIELDS) for row in self.registries["entities"]]
        if msg_type == ENTITY_GET:
            return self._get_entries(msg)
        if msg_type == ENTITY_UPDATE:
            return self._update_entity(msg)
        if msg_type == CATEGORY_LIST:
            if set(msg) != {"scope"}:
                raise HAClientError(f"{msg_type} failed: scope is required", "invalid_format")
            return copy.deepcopy(self.categories.get(msg["scope"], []))
        if msg_type not in _SCHEMAS:
            raise AssertionError(f"unexpected command {msg_type}")
        if msg_type == DEVICE_UPDATE:
            return copy.deepcopy(self._update_device(msg))
        registry = msg_type.split("/")[1].removesuffix("_registry") + "s"
        op = msg_type.rsplit("/", 1)[1]
        self._validate(msg_type, registry, op, msg)
        data = dict(msg)
        if "aliases" in data:
            data["aliases"] = sorted({s.strip() for s in data["aliases"] if s.strip()})
        if registry == "areas" and "labels" in data:
            known = {label["label_id"] for label in self.registries["labels"]}
            data["labels"] = sorted(set(data["labels"]) & known)
        try:
            if op == "create":
                return copy.deepcopy(self._create(registry, data))
            return copy.deepcopy(self._update(registry, data))
        except ValueError as err:
            raise HAClientError(f"{msg_type} failed: {err}", code="invalid_info") from None
        except KeyError:
            raise HAClientError(
                f"{msg_type} failed: Unknown error", code="unknown_error"
            ) from None

    def _validate(self, msg_type: str, registry: str, op: str, msg: dict[str, Any]) -> None:
        schema = _SCHEMAS[msg_type]
        required = ("name",) if op == "create" else (_KEYS[registry],)
        allowed = set(schema) | set(required)
        problems = [f"extra keys not allowed @ data['{k}']" for k in msg if k not in allowed]
        problems += [f"required key not provided @ data['{k}']" for k in required if k not in msg]
        problems += [
            f"expected str for dictionary value @ data['{k}']"
            for k, nullable in schema.items()
            if k in msg and msg[k] is None and not nullable
        ]
        if registry == "labels":
            icon, color = msg.get("icon"), msg.get("color")
            if icon is not None and ":" not in str(icon):
                problems.append('Icons should be specified in the form "prefix:name"')
            if color is not None and not (
                _COLOR_HEX.fullmatch(str(color)) or color in _LABEL_THEME_COLORS
            ):
                problems.append("Color should be in the format #RRGGBB")
        if problems:
            raise HAClientError(f"{msg_type} failed: {problems[0]}", code="invalid_format")

    def _check_name(self, registry: str, name: str, skip: str | None) -> None:
        key = _KEYS[registry]
        wanted = normalize_name(name)
        for row in self.registries[registry]:
            if row[key] != skip and normalize_name(row["name"]) == wanted:
                raise ValueError(f"The name {name} ({wanted}) is already in use")

    def _check_sensors(self, data: dict[str, Any], old: dict[str, Any] | None) -> None:
        for field, device_class in (
            ("temperature_entity_id", "temperature"),
            ("humidity_entity_id", "humidity"),
        ):
            entity_id = data.get(field)
            if entity_id is None or (old is not None and old.get(field) == entity_id):
                continue
            state = next((s for s in self.states if s["entity_id"] == entity_id), None)
            if state is None:
                raise ValueError(f"Entity {entity_id} does not exist")
            if (
                not entity_id.startswith("sensor.")
                or state.get("attributes", {}).get("device_class") != device_class
            ):
                raise ValueError(f"Entity {entity_id} is not a {device_class} sensor")

    def _tick(self) -> float:
        self._clock += 1
        return self._clock

    def _create(self, registry: str, data: dict[str, Any]) -> dict[str, Any]:
        key = _KEYS[registry]
        self._check_name(registry, data["name"], None)
        if registry == "areas":
            self._check_sensors(data, None)
        taken = {row[key] for row in self.registries[registry]}
        base = suggestion = slugify(data["name"])
        tries = 1
        while suggestion in taken:
            tries += 1
            suggestion = f"{base}_{tries}"
        row: dict[str, Any] = {name: None for name in _FIELDS[registry]}
        if registry != "labels":
            row["aliases"] = []
        if registry == "areas":
            row["labels"] = []
        row.update(data)
        row[key] = suggestion
        row["created_at"] = row["modified_at"] = self._tick()
        self.registries[registry].append(row)
        return row

    def _update(self, registry: str, data: dict[str, Any]) -> dict[str, Any]:
        key = _KEYS[registry]
        rows = self.registries[registry]
        index = next((i for i, row in enumerate(rows) if row[key] == data[key]), None)
        if index is None:
            raise KeyError(data[key])
        old = rows[index]
        if "name" in data and normalize_name(data["name"]) != normalize_name(old["name"]):
            self._check_name(registry, data["name"], old[key])
        if registry == "areas":
            self._check_sensors(data, old)
        changes = {k: v for k, v in data.items() if k != key and v != old.get(k)}
        if changes:
            rows[index] = {**old, **changes, "modified_at": self._tick()}
        return rows[index]

    def _update_device(self, msg: dict[str, Any]) -> dict[str, Any]:
        schema = _SCHEMAS[DEVICE_UPDATE]
        allowed = {"device_id", *schema}
        problems = [f"extra keys not allowed @ data['{k}']" for k in msg if k not in allowed]
        if "device_id" not in msg:
            problems.append("required key not provided @ data['device_id']")
        for k, value in msg.items():
            if k == "labels":
                if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                    problems.append(f"expected list for dictionary value @ data['{k}']")
            elif k == "disabled_by":
                if value not in (None, "user"):
                    problems.append(
                        f"value must be one of ['user'] for dictionary value @ data['{k}']"
                    )
            elif k in schema and not (value is None or isinstance(value, str)):
                problems.append(f"expected str for dictionary value @ data['{k}']")
        if problems:
            raise HAClientError(f"{DEVICE_UPDATE} failed: {problems[0]}", code="invalid_format")
        row = self.row("devices", msg["device_id"])
        if row is None:
            raise HAClientError(f"{DEVICE_UPDATE} failed: Device not found", code="not_found")
        data = {k: v for k, v in msg.items() if k != "device_id"}
        if "labels" in data:
            known = {label["label_id"] for label in self.registries["labels"]}
            data["labels"] = sorted(set(data["labels"]) & known)
        if "disabled_by" in data and data["disabled_by"] is None:
            parent = self.row("devices", row.get("parent_device_id") or "")
            if row.get("config_entry_id") in self.disabled_entries:
                del data["disabled_by"]
            elif parent is not None and parent["disabled_by"] is not None:
                data["disabled_by"] = "device"
        changes = {
            k: v
            for k, v in data.items()
            if (set(v) != set(row[k]) if k == "labels" else v != row.get(k))
        }
        if not changes:
            return row
        row.update(changes, modified_at=self._tick())
        if "disabled_by" in changes:
            self._cascade_entities(row)
            for child in self.registries["devices"]:
                if child.get("parent_device_id") != row["id"]:
                    continue
                if row["disabled_by"] is not None and child["disabled_by"] is None:
                    child.update(disabled_by="device", modified_at=self._tick())
                    self._cascade_entities(child)
                elif row["disabled_by"] is None and child["disabled_by"] == "device":
                    child.update(disabled_by=None, modified_at=self._tick())
                    self._cascade_entities(child)
        return row

    def _cascade_entities(self, device: dict[str, Any]) -> None:
        """HA's entity registry when *device*'s disable changes: disable or enable its entities."""
        if device["disabled_by"] == "config_entry":
            return
        for entity in self.registries["entities"]:
            if entity.get("device_id") != device["id"]:
                continue
            if device["disabled_by"] is None and entity["disabled_by"] == "device":
                entity.update(disabled_by=None, modified_at=self._tick())
            elif device["disabled_by"] is not None and entity["disabled_by"] is None:
                entity.update(disabled_by="device", modified_at=self._tick())

    @staticmethod
    def _entity_dict(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        return copy.deepcopy({name: row.get(name) for name in fields})

    def _get_entries(self, msg: dict[str, Any]) -> dict[str, Any]:
        ids = msg.get("entity_ids")
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",")]
        if set(msg) != {"entity_ids"} or not isinstance(ids, list):
            raise HAClientError(f"{ENTITY_GET} failed: entity_ids is required", "invalid_format")
        out = {}
        for entity_id in ids:
            row = self.row("entities", entity_id)
            out[entity_id] = None if row is None else self._entity_dict(row, EXTENDED_FIELDS)
        return out

    def _entity_schema_problem(self, msg: dict[str, Any]) -> str | None:
        nullable_str = ("area_id", "device_class", "icon", "name")
        allowed = {
            "entity_id",
            "aliases",
            "categories",
            "labels",
            "new_entity_id",
            "disabled_by",
            "hidden_by",
            "options_domain",
            "options",
            *nullable_str,
        }
        for key in msg:
            if key not in allowed:
                return f"extra keys not allowed @ data['{key}']"
        if not isinstance(msg.get("entity_id"), str) or not _VALID_ENTITY_ID.fullmatch(
            msg["entity_id"].lower()
        ):
            return "Entity ID is an invalid entity ID for dictionary value @ data['entity_id']"
        for key in nullable_str:
            if key in msg and not (msg[key] is None or isinstance(msg[key], str)):
                return f"expected str for dictionary value @ data['{key}']"
        if "aliases" in msg and not (
            isinstance(msg["aliases"], list)
            and all(a is None or isinstance(a, str) for a in msg["aliases"])
        ):
            return "expected list for dictionary value @ data['aliases']"
        if "labels" in msg and not (
            isinstance(msg["labels"], list) and all(isinstance(v, str) for v in msg["labels"])
        ):
            return "expected list for dictionary value @ data['labels']"
        if "categories" in msg:
            cats = msg["categories"]
            if not isinstance(cats, dict) or not all(
                isinstance(k, str) and _SLUG_KEY.fullmatch(k) for k in cats
            ):
                return "expected a dictionary with slug keys @ data['categories']"
            if not all(v is None or isinstance(v, str) for v in cats.values()):
                return "expected str for dictionary value @ data['categories']"
        if "new_entity_id" in msg and not isinstance(msg["new_entity_id"], str):
            return "expected str for dictionary value @ data['new_entity_id']"
        for key in ("disabled_by", "hidden_by"):
            if key in msg and msg[key] not in (None, "user"):
                return f"value must be one of ['user'] for dictionary value @ data['{key}']"
        if ("options_domain" in msg) != ("options" in msg):
            return "some but not all values in the same group of inclusion 'entity_option'"
        if "options_domain" in msg and not isinstance(msg["options_domain"], str):
            return "expected str for dictionary value @ data['options_domain']"
        if "options" in msg and not (msg["options"] is None or isinstance(msg["options"], dict)):
            return "expected a dictionary for dictionary value @ data['options']"
        return None

    def _update_entity(self, msg: dict[str, Any]) -> dict[str, Any]:
        if problem := self._entity_schema_problem(msg):
            raise HAClientError(f"{ENTITY_UPDATE} failed: {problem}", code="invalid_format")
        entity_id = msg["entity_id"].lower()
        row = self.row("entities", entity_id)
        if row is None:
            raise HAClientError(f"{ENTITY_UPDATE} failed: Entity not found", code="not_found")
        keys = ("area_id", "device_class", "disabled_by", "hidden_by", "icon", "name")
        changes = {k: msg[k] for k in (*keys, "new_entity_id") if k in msg}
        if "aliases" in msg:
            changes["aliases"] = [
                a.strip() if a is not None else None
                for a in msg["aliases"]
                if a is None or a.strip()
            ]
        if "labels" in msg:
            known = {label["label_id"] for label in self.registries["labels"]}
            changes["labels"] = sorted(set(msg["labels"]) & known)
        if "disabled_by" in msg and msg["disabled_by"] is None and row.get("device_id"):
            device = self.row("devices", row["device_id"])
            if device is not None and device["disabled_by"] is not None:
                raise HAClientError(f"{ENTITY_UPDATE} failed: Device is disabled", "invalid_info")
        if "categories" in msg:
            categories = dict(row["categories"])
            for scope, category in msg["categories"].items():
                if category is None:
                    categories.pop(scope, None)
                else:
                    categories[scope] = category
            changes["categories"] = categories
        try:
            self._change_entity(row, changes)
            if "options_domain" in msg:
                options = {k: v for k, v in row["options"].items() if k != msg["options_domain"]}
                if msg["options"] is not None:
                    options[msg["options_domain"]] = copy.deepcopy(msg["options"])
                if options != row["options"]:
                    row.update(options=options, modified_at=self._tick())
        except ValueError as err:
            raise HAClientError(f"{ENTITY_UPDATE} failed: {err}", code="invalid_info") from None
        result: dict[str, Any] = {"entity_entry": self._entity_dict(row, EXTENDED_FIELDS)}
        if "disabled_by" in changes and changes["disabled_by"] is None:
            if row.get("config_entry_id") is None:
                result["require_restart"] = True
            else:
                result["reload_delay"] = RELOAD_DELAY
        return result

    def _change_entity(self, row: dict[str, Any], changes: dict[str, Any]) -> None:
        new_values = {}
        for key, value in changes.items():
            old = row.get("entity_id" if key == "new_entity_id" else key)
            same = set(value) == set(old) if key == "labels" else value == old
            if not same:
                new_values[key] = value
        new_id = new_values.pop("new_entity_id", None)
        if new_id is not None:
            taken = self.row("entities", new_id) is not None or any(
                s["entity_id"] == new_id for s in self.states
            )
            if taken:
                raise ValueError("Entity with this ID is already registered")
            if not _VALID_ENTITY_ID.fullmatch(new_id):
                raise ValueError("Invalid entity ID")
            if new_id.partition(".")[0] != row["entity_id"].partition(".")[0]:
                raise ValueError("New entity ID should be same domain")
        if "area_id" in new_values or "name" in new_values:
            area = new_values.get("area_id", row.get("area_id"))
            name = new_values.get("name", row.get("name"))
            own = name if name is not None else row.get("original_name")
            if area is not None and row.get("device_id") is not None and not own:
                raise ValueError(
                    "An entity without a name of its own cannot have an area of its own, "
                    "set the area on its device instead"
                )
        if new_id is not None:
            for state in self.states:
                if state["entity_id"] == row["entity_id"]:
                    state["entity_id"] = new_id
            new_values["entity_id"] = new_id
        if new_values:
            row.update(new_values, modified_at=self._tick())

    def row(self, registry: str, live_id: str) -> dict[str, Any] | None:
        key = _KEYS[registry]
        return next((row for row in self.registries[registry] if row[key] == live_id), None)
