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

_KEYS = {"floors": "floor_id", "labels": "label_id", "areas": "area_id", "devices": "id"}

DEVICE_UPDATE = "config/device_registry/update"

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


def empty_fixture(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """A rebuilt Home Assistant: no floors, labels or areas, *data*'s devices with
    new ids and no user values (see :func:`rebuilt_devices`), and *data*'s states.
    """
    data = data if data is not None else load_fixture()
    return {
        "ha_version": data.get("ha_version"),
        "devices": rebuilt_devices(data.get("devices", [])),
        "disabled_entries": [_new_id(e) for e in data.get("disabled_entries", [])],
        "states": copy.deepcopy(data.get("states", [])),
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
        return [(cmd, msg) for cmd, msg in self.sent if cmd in _SCHEMAS]

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
            for child in self.registries["devices"]:
                if child.get("parent_device_id") != row["id"]:
                    continue
                if row["disabled_by"] is not None and child["disabled_by"] is None:
                    child.update(disabled_by="device", modified_at=self._tick())
                elif row["disabled_by"] is None and child["disabled_by"] == "device":
                    child.update(disabled_by=None, modified_at=self._tick())
        return row

    def row(self, registry: str, live_id: str) -> dict[str, Any] | None:
        key = _KEYS[registry]
        return next((row for row in self.registries[registry] if row[key] == live_id), None)
