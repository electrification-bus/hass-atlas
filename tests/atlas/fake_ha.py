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

Energy commands follow ``components/energy/{websocket_api,data}.py``:
``energy/get_prefs`` fails with ``not_found`` until prefs are first saved,
then returns them as stored. ``energy/save_prefs`` validates each list it is
sent as HA's schema does (see :func:`energy_schema_problem`), replaces those
lists and keeps the others (a first save starts from three empty lists),
fills a grid source's null keys, coerces numbers to float, drops a gas
``entity_energy_from``, sets a battery or grid ``stat_rate`` from its
``power_config`` and returns the whole prefs. Ids are not checked. Before
2026.3 (:data:`UNIFIED_GRID_SINCE`) it takes the legacy grid form only, and
the source fields in :data:`SOURCE_FIELD_SINCE` only from the release that
added each. With ``HASS_ATLAS_HA_PYTHON`` set, every save the 2026.10 schema
applies to is also run through HA's own schema, and a different verdict or
result is recorded in
:data:`SCHEMA_MISMATCHES`, which ``tests/atlas/conftest.py`` fails a test on.
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

from hass_atlas.energy_prefs import UNIFIED_GRID_SINCE, parse_ha_version
from hass_atlas.ha_client import HAClientError
from tests._ha_energy_schema import check_with_ha, required_key_errors

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
ENERGY_GET = "energy/get_prefs"
ENERGY_SAVE = "energy/save_prefs"
ENERGY_LISTS = ("energy_sources", "device_consumption", "device_consumption_water")

SCHEMA_MISMATCHES: list[str] = []
"""Saves on which the fake and HA's own schema disagreed."""

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
        self.energy: dict[str, Any] | None = data.get("energy")
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
        return [
            (cmd, msg)
            for cmd, msg in self.sent
            if cmd in _SCHEMAS or cmd in (ENTITY_UPDATE, ENERGY_SAVE)
        ]

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
        if msg_type == ENERGY_GET:
            if self.energy is None:
                raise HAClientError(f"{msg_type} failed: No prefs", code="not_found")
            return copy.deepcopy(self.energy)
        if msg_type == ENERGY_SAVE:
            return self._save_energy(msg)
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

    @property
    def legacy_energy(self) -> bool:
        parsed = parse_ha_version(self.ha_version)
        return parsed is not None and parsed < UNIFIED_GRID_SINCE

    def _save_energy(self, msg: dict[str, Any]) -> dict[str, Any]:
        version = parse_ha_version(self.ha_version)
        problem, lists = energy_schema_problem(msg, legacy=self.legacy_energy, version=version)
        if version is None or version >= _CURRENT_SCHEMA_SINCE:
            _check_against_ha(msg, problem, lists)
        if problem:
            raise HAClientError(f"{ENERGY_SAVE} failed: {problem}", code="invalid_format")
        if "energy_sources" in lists:
            lists["energy_sources"] = [_derive_stat_rate(s) for s in lists["energy_sources"]]
        current = self.energy or {name: [] for name in ENERGY_LISTS}
        self.energy = {**current, **lists}
        return copy.deepcopy(self.energy)

    def row(self, registry: str, live_id: str) -> dict[str, Any] | None:
        key = _KEYS[registry]
        return next((row for row in self.registries[registry] if row[key] == live_id), None)


# --- energy/save_prefs ---------------------------------------------------


class _Invalid(Exception):
    pass


_ENTITY_ID = re.compile(r"(?!.+__)" + _OBJECT_ID + r"\." + _OBJECT_ID)
_POWER_KEYS = ("stat_rate", "stat_rate_inverted", "stat_rate_from", "stat_rate_to")


_NO_DEFAULT = object()

# The first release whose ``energy/data.py`` takes each source field, by
# source type, from the core release tags.
SOURCE_FIELD_SINCE: dict[str, dict[str, tuple[int, int]]] = {
    "grid": {"name": (2026, 6)},
    "solar": {"name": (2026, 6)},
    "battery": {
        "power_config": (2026, 2),
        "stat_soc": (2026, 6),
        "name": (2026, 6),
        "capacity": (2026, 8),
    },
    "gas": {"stat_rate": (2026, 3), "name": (2026, 6)},
    "water": {"stat_rate": (2026, 3), "name": (2026, 6)},
}
# Saves to this release or later get the schema of HA 2026.10.
_CURRENT_SCHEMA_SINCE = max(v for fields in SOURCE_FIELD_SINCE.values() for v in fields.values())
_LEGACY_GRID_KEYS = ("type", "flow_from", "flow_to", "power", "cost_adjustment_day")


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise _Invalid(f"expected str @ {where}")
    return value


def _str_or_none(value: Any, where: str) -> str | None:
    return None if value is None else _str(value, where)


def _float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise _Invalid(f"expected float @ {where}") from None


def _float_or_none(value: Any, where: str) -> float | None:
    return None if value is None else _float(value, where)


def _power_config(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise _Invalid(f"expected a dictionary @ {where}")
    for k in value:
        if k not in _POWER_KEYS:
            raise _Invalid(f"extra keys not allowed @ {where}['{k}']")
    out = {k: _str(v, f"{where}['{k}']") for k, v in value.items()}
    if "stat_rate" in out and "stat_rate_inverted" in out:
        raise _Invalid(
            f"two or more values in the same group of exclusion 'power_source' @ {where}"
        )
    if ("stat_rate_from" in out) != ("stat_rate_to" in out):
        raise _Invalid(
            f"some but not all values in the same group of inclusion 'two_sensors' @ {where}"
        )
    if not out:
        raise _Invalid("power_config must have at least one option")
    if sum(("stat_rate" in out, "stat_rate_inverted" in out, "stat_rate_from" in out)) > 1:
        raise _Invalid("power_config must use only one configuration method")
    return out


def _fields(entry: dict[str, Any], where: str, spec: dict[str, Any]) -> dict[str, Any]:
    """*entry* through *spec*, which maps a field to (checker, required, default).

    A None checker drops the field, as HA's ``Remove`` does; ``_NO_DEFAULT``
    leaves an absent optional field out.
    """
    for k in entry:
        if k not in spec:
            raise _Invalid(f"extra keys not allowed @ {where}['{k}']")
    out = {}
    for name, (check, needed, default) in spec.items():
        if name in entry:
            if check is not None:
                out[name] = check(entry[name], f"{where}['{name}']")
        elif needed:
            raise _Invalid(f"required key not provided @ {where}['{name}']")
        elif default is not _NO_DEFAULT:
            out[name] = default
    return out


def _reject_external_price(
    entry: dict[str, Any], stat: str, entity: str, number: str, cost: str
) -> None:
    value = entry.get(stat)
    if value is not None and not _ENTITY_ID.fullmatch(value) and entry.get(cost) is None:
        if entry.get(entity) is not None or entry.get(number) is not None:
            raise _Invalid(
                "Entity or number price is not supported for external statistics. "
                f"Use {cost} instead"
            )


def _source(entry: Any, where: str, version: tuple[int, int] | None = None) -> dict[str, Any]:
    if not isinstance(entry, dict) or entry.get("type") not in (
        "grid",
        "solar",
        "battery",
        "gas",
        "water",
    ):
        raise _Invalid(
            f"Unexpected value for type: Expected grid, solar, battery, gas, water @ {where}"
        )
    req, opt = (_str, True, _NO_DEFAULT), (_str, False, _NO_DEFAULT)
    nullable = (_str_or_none, False, None)
    number = (_float_or_none, False, None)
    kind = entry["type"]
    if kind == "grid":
        spec = {
            "type": req,
            "stat_energy_from": nullable,
            "stat_energy_to": nullable,
            "stat_cost": nullable,
            "entity_energy_price": nullable,
            "number_energy_price": number,
            "stat_compensation": nullable,
            "entity_energy_price_export": nullable,
            "number_energy_price_export": number,
            "stat_rate": opt,
            "power_config": (_power_config, False, _NO_DEFAULT),
            "cost_adjustment_day": (_float, True, _NO_DEFAULT),
            "name": opt,
        }
        out = _fields(entry, where, _since(spec, kind, version))
        _reject_external_price(
            out, "stat_energy_from", "entity_energy_price", "number_energy_price", "stat_cost"
        )
        _reject_external_price(
            out,
            "stat_energy_to",
            "entity_energy_price_export",
            "number_energy_price_export",
            "stat_compensation",
        )
        for entity, num in (
            ("entity_energy_price", "number_energy_price"),
            ("entity_energy_price_export", "number_energy_price_export"),
        ):
            if out[entity] is not None and out[num] is not None:
                raise _Invalid("Define either an entity or a fixed number for the price")
        if not any(
            out.get(k) is not None
            for k in ("stat_energy_from", "stat_energy_to", "stat_rate", "power_config")
        ):
            raise _Invalid(
                "Grid must have at least one of: import meter, export meter, or power sensor"
            )
        return out
    if kind == "solar":

        def forecast(value: Any, at: str) -> list[str] | None:
            if value is None:
                return None
            if not isinstance(value, list):
                raise _Invalid(f"expected a list @ {at}")
            return [_str(v, at) for v in value]

        spec = {
            "type": req,
            "stat_energy_from": req,
            "stat_rate": opt,
            "config_entry_solar_forecast": (forecast, False, _NO_DEFAULT),
            "name": opt,
        }
        return _fields(entry, where, _since(spec, kind, version))
    if kind == "battery":

        def capacity(value: Any, at: str) -> float:
            number = _float(value, at)
            if number <= 0:
                raise _Invalid(f"value must be above 0 @ {at}")
            return number

        spec = {
            "type": req,
            "stat_energy_from": req,
            "stat_energy_to": req,
            "stat_rate": opt,
            "power_config": (_power_config, False, _NO_DEFAULT),
            "stat_soc": opt,
            "capacity": (capacity, False, _NO_DEFAULT),
            "name": opt,
        }
        return _fields(entry, where, _since(spec, kind, version))
    spec = {
        "type": req,
        "stat_energy_from": req,
        "stat_rate": opt,
        "stat_cost": (_str_or_none, False, _NO_DEFAULT),
        "entity_energy_price": (_str_or_none, False, _NO_DEFAULT),
        "number_energy_price": (_float_or_none, False, _NO_DEFAULT),
        "name": opt,
    }
    if kind == "gas":
        spec["entity_energy_from"] = (None, False, _NO_DEFAULT)  # removed
    out = _fields(entry, where, _since(spec, kind, version))
    _reject_external_price(
        out, "stat_energy_from", "entity_energy_price", "number_energy_price", "stat_cost"
    )
    return out


def _since(spec: dict[str, Any], kind: str, version: tuple[int, int] | None) -> dict[str, Any]:
    """*spec* without the fields HA *version* does not take yet; None is the newest."""
    if version is None:
        return spec
    since = SOURCE_FIELD_SINCE.get(kind, {})
    return {k: v for k, v in spec.items() if k not in since or version >= since[k]}


def _legacy_grid(entry: dict[str, Any], where: str) -> dict[str, Any]:
    for k in entry:
        if k not in _LEGACY_GRID_KEYS:
            raise _Invalid(f"extra keys not allowed @ {where}['{k}']")
    return copy.deepcopy(entry)


def _device(entry: Any, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise _Invalid(f"expected a dictionary @ {where}")
    opt = (_str, False, _NO_DEFAULT)
    spec = {
        "stat_consumption": (_str, True, _NO_DEFAULT),
        "stat_rate": opt,
        "name": opt,
        "included_in_stat": opt,
    }
    return _fields(entry, where, spec)


def _unique_grid_stats(sources: list[dict[str, Any]]) -> None:
    for name, what in (
        ("stat_energy_from", "Import meter"),
        ("stat_energy_to", "Export meter"),
        ("stat_rate", "Power stat"),
    ):
        seen: set[str] = set()
        for source in sources:
            value = source.get(name) if source["type"] == "grid" else None
            if value is None:
                continue
            if value in seen:
                raise _Invalid(f"{what} {value} is used in multiple grid connections")
            seen.add(value)


def energy_schema_problem(
    msg: dict[str, Any], legacy: bool = False, version: tuple[int, int] | None = None
) -> tuple[str | None, dict[str, Any]]:
    """HA's error for an ``energy/save_prefs`` message, or None, and the validated lists.

    *version* is HA's ``(year, month)``, None for the newest. A *legacy* grid
    source is checked for its keys only (:func:`required_key_errors`).
    """
    for key in msg:
        if key not in ENERGY_LISTS:
            return f"extra keys not allowed @ data['{key}']", {}
    for key, value in msg.items():
        if not isinstance(value, list):
            return f"expected list for dictionary value @ data['{key}']", {}
    if legacy and (errors := required_key_errors(msg, legacy=True)):
        return errors[0], {}
    out: dict[str, Any] = {}
    try:
        for key, value in msg.items():
            if key == "energy_sources":
                sources = [
                    _legacy_grid(e, f"data['{key}'][{i}]")
                    if legacy and isinstance(e, dict) and e.get("type") == "grid"
                    else _source(e, f"data['{key}'][{i}]", version)
                    for i, e in enumerate(value)
                ]
                if not legacy:
                    _unique_grid_stats(sources)
                out[key] = sources
            else:
                out[key] = [_device(e, f"data['{key}'][{i}]") for i, e in enumerate(value)]
    except _Invalid as err:
        return str(err), {}
    return None, out


def _derive_stat_rate(source: dict[str, Any]) -> dict[str, Any]:
    """``EnergyManager._process_energy_sources``: stat_rate from power_config."""
    config = source.get("power_config")
    if source["type"] not in ("battery", "grid") or config is None:
        return source
    if "stat_rate" in config:
        rate = config["stat_rate"]
    elif "stat_rate_inverted" in config:
        inverted = config["stat_rate_inverted"]
        rate = (
            f"{inverted}_inverted"
            if inverted.startswith("sensor.")
            else f"sensor.{inverted.replace('.', '_')}_inverted"
        )
    else:
        low = config["stat_rate_from"].removeprefix("sensor.")
        high = config["stat_rate_to"].removeprefix("sensor.")
        rate = f"sensor.energy_{source['type']}_{low}_{high}_net_power"
    return {**source, "stat_rate": rate}


def _check_against_ha(msg: dict[str, Any], problem: str | None, lists: dict[str, Any]) -> None:
    """Record a save on which HA's list schemas disagree with the fake's.

    The message-level checks (unknown keys, non-list values) are the fake's own.
    """
    if any(k not in ENERGY_LISTS or not isinstance(v, list) for k, v in msg.items()):
        return
    verdict = check_with_ha(msg)
    if verdict is None:
        return
    if (problem is None) != ("error" not in verdict):
        SCHEMA_MISMATCHES.append(
            f"{json.dumps(msg)}: fake says {problem!r}, HA says {verdict.get('error')!r}"
        )
    elif problem is None and verdict["result"] != lists:
        SCHEMA_MISMATCHES.append(
            f"{json.dumps(msg)}: fake gives {json.dumps(lists)}, HA {json.dumps(verdict['result'])}"
        )
