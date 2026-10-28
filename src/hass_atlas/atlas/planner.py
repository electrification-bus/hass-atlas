"""Match atlas items to live ones and plan what apply writes.

The plan covers every stage before anything is written: each stage plans
against the live rows as earlier stages would leave them, so a dry run shows
the ids HA will assign and the references rewritten to them.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hass_atlas.atlas import domain_named
from hass_atlas.atlas.domains.areas import AREAS
from hass_atlas.atlas.domains.base import Domain, RegistryDomain, Row
from hass_atlas.atlas.domains.devices import DEVICES, PAIR_FIELDS, TIE_FIELDS, DeviceDomain
from hass_atlas.atlas.domains.energy import ENERGY, LISTS, EnergyDomain
from hass_atlas.atlas.domains.entities import ENTITIES, EntityDomain
from hass_atlas.atlas.domains.floors import FLOORS
from hass_atlas.atlas.domains.labels import LABELS
from hass_atlas.atlas.energy_stage import plan_energy
from hass_atlas.atlas.plan import Change, Plan, Section, registry, resolve_refs
from hass_atlas.atlas.refs import ENTITY_IDS, IdMap
from hass_atlas.energy_prefs import grid_format, parse_ha_version

AREA_SENSORS = ("temperature_entity_id", "humidity_entity_id")
_SENSOR_CLASS = {"temperature_entity_id": "temperature", "humidity_entity_id": "humidity"}
TEMP_SUFFIX = "_atlas_tmp"


@dataclass(frozen=True)
class Stage:
    number: int
    domain: Domain
    title: str
    fields: tuple[str, ...]
    creates: bool = True


def _fields(domain: RegistryDomain, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(name for name in domain.field_order[1:] if name not in exclude)


# Entities (4) come before the area sensors (5): HA checks a sensor's
# device class against its state when it is set, and a rename moves the state.
# Energy (6) comes last, once every entity_id is final.
STAGES: tuple[Stage, ...] = (
    Stage(1, LABELS, "labels", _fields(LABELS)),
    Stage(1, FLOORS, "floors", _fields(FLOORS)),
    Stage(2, AREAS, "areas", _fields(AREAS, AREA_SENSORS)),
    Stage(3, DEVICES, "devices", DEVICES.plan_fields, creates=False),
    Stage(4, ENTITIES, "entities", ENTITIES.plan_fields, creates=False),
    Stage(5, AREAS, "area sensors", AREA_SENSORS, creates=False),
    Stage(6, ENERGY, "energy", ()),
)


def stage_fields(change: Change) -> tuple[str, ...]:
    for stage in STAGES:
        if stage.number == change.stage and stage.domain.name == change.domain:
            return stage.fields
    raise KeyError((change.stage, change.domain))


def normalize_name(name: str) -> str:
    """The form HA compares names in for uniqueness: casefolded, spaces removed."""
    return name.casefold().replace(" ", "")


# The theme colors HA accepts for a label besides #RRGGBB (SUPPORTED_LABEL_THEME_COLORS).
LABEL_COLORS = frozenset(
    (
        "primary accent disabled red pink purple deep-purple indigo blue light-blue cyan "
        "teal green light-green lime yellow amber orange deep-orange brown light-grey grey "
        "dark-grey blue-grey black white"
    ).split()
)
_COLOR_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_SIMPLE_NAME = re.compile(r"[A-Za-z0-9 _-]*[A-Za-z0-9][A-Za-z0-9 _-]*")
_SLUG = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


def simple_slug(name: str) -> str | None:
    """HA's ``slugify(name)`` for a name of ASCII letters, digits, spaces, ``_`` and ``-``.

    For those characters python-slugify only lowercases, turns each run of
    the others into one ``_`` and strips ``_`` from the ends. Any other name
    gives None, since transliteration and quote handling are not reproduced.
    """
    if not _SIMPLE_NAME.fullmatch(name):
        return None
    return re.sub(r"[ _-]+", "_", name.lower()).strip("_")


def is_slug(value: str) -> bool:
    """Whether *value* is its own slug, so a name equal to it creates that id."""
    return _SLUG.fullmatch(value) is not None


def next_id(base: str, taken: set[str]) -> str:
    """The id HA generates from slug *base* when *taken* are in use."""
    suggestion, tries = base, 1
    while suggestion in taken:
        tries += 1
        suggestion = f"{base}_{tries}"
    return suggestion


def comparable(domain: RegistryDomain, name: str, value: Any) -> Any:
    """*value* as HA stores it in *domain*; see :meth:`RegistryDomain.comparable`."""
    return domain.comparable(name, value)


def _type_error(domain: RegistryDomain, name: str, value: Any) -> str | None:
    if name in domain.set_fields:
        if value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return None
        return f"{name} must be a list of strings"
    if name == "disabled":
        if value is None or isinstance(value, bool):
            return None
        return "disabled must be true or false"
    if name == "level":
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return None
        return "level must be a whole number"
    if value is not None and not isinstance(value, str):
        return f"{name} must be a string"
    if domain.name == "labels" and value is not None:
        if name == "icon" and ":" not in value:
            return 'icon must have the form "prefix:name"'
        if name == "color" and not (_COLOR_HEX.fullmatch(value) or value in LABEL_COLORS):
            return "color must be #RRGGBB or a theme color such as red or light-blue"
    return None


@dataclass
class Binding:
    live: Row | None = None
    by_name: bool = False
    refused: str | None = None


def match(domain: RegistryDomain, items: list[Row], rows: list[Row]) -> dict[str, Binding]:
    """Bind each file item to a live row: equal id first, then equal normalized name."""
    key = domain.key
    by_id = {row[key]: row for row in rows}
    owner: dict[str, str] = {}
    out: dict[str, Binding] = {}
    for item in items:
        row = by_id.get(item[key])
        if row is not None:
            out[item[key]] = Binding(row)
            owner[row[key]] = item[key]
    for item in items:
        if item[key] in out:
            continue
        name = item.get("name")
        if not isinstance(name, str):
            out[item[key]] = Binding()
            continue
        wanted = normalize_name(name)
        found = [r for r in rows if isinstance(r.get("name"), str)]
        found = [r for r in found if normalize_name(r["name"]) == wanted]
        if len(found) > 1:
            ids = ", ".join(r[key] for r in found)
            out[item[key]] = Binding(refused=f"name matches live {ids}")
        elif found and found[0][key] in owner:
            live = found[0][key]
            out[item[key]] = Binding(
                refused=f"name matches live {live}, which is matched to {owner[live]}"
            )
        elif found:
            out[item[key]] = Binding(found[0], by_name=True)
            owner[found[0][key]] = item[key]
        else:
            out[item[key]] = Binding()
    return out


def match_devices(domain: DeviceDomain, items: list[Row], rows: list[Row]) -> dict[str, Binding]:
    """Bind each file device to the live device with the same identifier (or connection) set.

    Manufacturer and model only break a tie. Two file items bound to one live
    device are both refused, since neither can be preferred.
    """
    index: dict[tuple[str, frozenset[Any]], list[Row]] = {}
    for row in rows:
        for pair_field in PAIR_FIELDS:
            index.setdefault((pair_field, domain.live_set(row, pair_field)), []).append(row)
    out: dict[str, Binding] = {}
    claims: dict[str, list[str]] = {}
    for item in items:
        key = domain.item_key(item)
        name, wanted = domain.match_set(item)
        found = index.get((name, wanted), [])
        if len(found) > 1:
            block = item["match"]
            ties = [f for f in TIE_FIELDS if f in block]
            narrowed = [row for row in found if all(row.get(f) == block[f] for f in ties)]
            if len(narrowed) != 1:
                if not ties:
                    why = "add manufacturer and model to the match to tell them apart"
                elif narrowed:
                    why = "manufacturer and model do not tell them apart"
                else:
                    why = "none has this manufacturer and model"
                out[key] = Binding(refused=f"{name} match {len(found)} live devices; {why}")
                continue
            found = narrowed
        if found:
            out[key] = Binding(found[0])
            claims.setdefault(found[0][domain.live_key], []).append(key)
        else:
            out[key] = Binding()
    for keys in claims.values():
        if len(keys) > 1:
            for key in keys:
                out[key] = Binding(
                    out[key].live, refused="another file item matches the same live device"
                )
    return out


def match_entities(domain: EntityDomain, items: list[Row], rows: list[Row]) -> dict[str, Binding]:
    """Bind each file entity to the live entity with its platform and unique_id, or entity_id.

    A unique_id match also needs the domain of the item's entity_id, when it
    has one, since HA keys entities by domain, platform and unique_id. Two
    file items bound to one live entity are both refused.
    """
    by_unique_id: dict[tuple[str, str], list[Row]] = {}
    by_entity_id: dict[str, Row] = {}
    for row in rows:
        by_entity_id[row["entity_id"]] = row
        if row.get("unique_id") is not None:
            pair = (str(row.get("platform")), str(row["unique_id"]))
            by_unique_id.setdefault(pair, []).append(row)
    out: dict[str, Binding] = {}
    claims: dict[str, list[str]] = {}
    for item in items:
        key = domain.item_key(item)
        block = item["match"]
        if "entity_id" in block:
            held = by_entity_id.get(block["entity_id"])
            if held is not None and held.get("platform") != block["platform"]:
                out[key] = Binding(
                    refused=f"{block['entity_id']} is held by platform {held.get('platform')}"
                )
                continue
            found = [held] if held is not None else []
        else:
            found = by_unique_id.get((block["platform"], block["unique_id"]), [])
            entity_id = item.get("entity_id")
            if isinstance(entity_id, str):
                prefix = entity_id.partition(".")[0] + "."
                found = [r for r in found if r["entity_id"].startswith(prefix)]
            if len(found) > 1:
                out[key] = Binding(
                    refused=f"unique_id matches {len(found)} live entities; set entity_id "
                    "to tell them apart by domain"
                )
                continue
        if found:
            out[key] = Binding(found[0])
            claims.setdefault(found[0]["entity_id"], []).append(key)
        else:
            out[key] = Binding()
    for keys in claims.values():
        if len(keys) > 1:
            for key in keys:
                out[key] = Binding(
                    out[key].live, refused="another file item matches the same live entity"
                )
    return out


@dataclass
class _Planner:
    docs: dict[str, list[Row]]
    applied: set[str]
    states: dict[str, Row] | None
    sim: dict[str, list[Row]]
    categories: dict[str, set[str]] = field(default_factory=dict)
    ha_version: str | None = None
    ids: IdMap = field(default_factory=IdMap)
    bindings: dict[str, dict[str, Binding]] = field(default_factory=dict)
    created: dict[str, set[str]] = field(default_factory=dict)
    refused: dict[str, set[str]] = field(default_factory=dict)
    _entities: dict[str, Row] | None = None

    def row(self, domain: RegistryDomain, live_id: str | None) -> Row | None:
        if isinstance(domain, EntityDomain):
            return self.entity_index().get(live_id or "")
        for row in self.sim.get(domain.name, []):
            if row[domain.live_key] == live_id:
                return row
        return None

    def entity_index(self) -> dict[str, Row]:
        """The simulated entity rows by entity_id, kept current through renames."""
        if self._entities is None:
            self._entities = {row["entity_id"]: row for row in self.sim.get("entities", [])}
        return self._entities

    def resolve_ref(self, target: str, value: str) -> tuple[str | None, bool]:
        """Return an error, or None, and whether *value* is created by this plan."""
        kind = registry(target).kind
        if value in {item[registry(target).key] for item in self.docs.get(target, [])}:
            if self.ids.get(target, value) is not None:
                return None, value in self.created.get(target, set())
            if value in self.refused.get(target, set()):
                return f"{kind} {value} is refused", False
            if target not in self.applied:
                return f"{kind} {value} is not in HA, and --only leaves out {target}", False
        domain = registry(target)
        if any(row[domain.key] == value for row in self.sim.get(target, [])):
            return None, False
        return f"no {kind} {value} in the file or in HA", False

    def check_refs(self, domain: RegistryDomain, values: Row, change: Change) -> str | None:
        for name, target in domain.refs:
            if target == ENTITY_IDS:
                continue  # sensors are checked against the states, by check_sensors
            value = values.get(name)
            refs = value if isinstance(value, list) else [value] if value is not None else []
            for ref in refs:
                error, creates = self.resolve_ref(target, ref)
                if error:
                    return f"{name} {ref}: {error}"
                if creates:
                    change.needs.append((target, ref))
        return None

    def check_sensors(self, values: Row) -> str | None:
        for name, device_class in _SENSOR_CLASS.items():
            entity_id = values.get(name)
            if entity_id is None:
                continue
            state = (self.states or {}).get(entity_id)
            if state is None:
                return f"{name} {entity_id}: entity does not exist"
            if (
                not entity_id.startswith("sensor.")
                or state.get("attributes", {}).get("device_class") != device_class
            ):
                return f"{name} {entity_id}: not a {device_class} sensor"
        return None

    def check_sensor_refs(self, values: Row) -> str | None:
        """Why a sensor in *values* is refused: it names a file entity that matches no live one.

        That entity_id in HA may belong to another entity.
        """
        files = {ENTITIES.file_entity_id(item) for item in self.docs.get("entities", [])}
        for name in AREA_SENSORS:
            value = values.get(name)
            if (
                isinstance(value, str)
                and value in files
                and self.ids.get(ENTITY_IDS, value) is None
            ):
                return f"{name} {value}: the file's entity {value} matches no live entity"
        return None

    def name_holder(self, domain: RegistryDomain, name: str, skip: str | None) -> str | None:
        wanted = normalize_name(name)
        for row in self.sim.get(domain.name, []):
            if row[domain.key] != skip and isinstance(row.get("name"), str):
                if normalize_name(row["name"]) == wanted:
                    return str(row[domain.key])
        return None

    def plan_device(self, stage: Stage, item: Row) -> Change:
        domain = DEVICES
        key = domain.item_key(item)
        binding = self.bindings[domain.name][key]
        change = Change(stage=stage.number, domain=domain.name, key=key, op="refused")
        change.label = domain.label(item, binding.live)
        fields = {name: item[name] for name in domain.field_order[1:] if name in item}
        desired = {name: value for name, value in fields.items() if name != "disabled"}
        if "disabled" in fields:
            desired["disabled_by"] = "user" if fields["disabled"] else None
        change.desired = desired
        if binding.refused:
            change.reason = binding.refused
            return change
        for name, value in fields.items():
            if error := _type_error(domain, name, value):
                change.reason = error
                return change
        if binding.live is None:
            change.op = "unmatched"
            return change
        if error := self.check_refs(domain, desired, change):
            change.reason = error
            return change
        change.live_id = binding.live[domain.live_key]
        row = self.row(domain, change.live_id)
        assert row is not None
        held = row.get("disabled_by")
        if "disabled_by" in desired and held not in (None, "user"):
            # HA ignores an enable while the config entry is disabled, and a
            # user disable would outlast the config entry being enabled again.
            change.reason = f"disabled_by is {held} in HA; apply sets or clears only user"
            return change
        self.plan_update(domain, row, change)
        if "disabled_by" in change.after:
            self.cascade_device(row)
        return change

    def cascade_device(self, device: Row) -> None:
        """Disable or enable *device*'s entities as HA does when the device's disable changes."""
        for row in self.sim.get("entities", []):
            if row.get("device_id") != device["id"]:
                continue
            if device["disabled_by"] is None and row.get("disabled_by") == "device":
                row["disabled_by"] = None
            elif device["disabled_by"] is not None and row.get("disabled_by") is None:
                row["disabled_by"] = "device"

    def plan_item(self, stage: Stage, item: Row) -> Change:
        domain = stage.domain
        assert isinstance(domain, RegistryDomain)
        if isinstance(domain, DeviceDomain):
            return self.plan_device(stage, item)
        key = item[domain.key]
        binding = self.bindings[domain.name][key]
        desired = {name: item[name] for name in stage.fields if name in item}
        change = Change(stage=stage.number, domain=domain.name, key=key, op="refused")
        change.desired = desired
        change.by_name = binding.by_name
        if binding.refused:
            change.reason = binding.refused
            return change
        for name, value in desired.items():
            if error := _type_error(domain, name, value):
                change.reason = error
                return change
        if "name" in desired and not (isinstance(desired["name"], str) and desired["name"]):
            change.reason = "name cannot be cleared"
            return change
        if error := self.check_refs(domain, desired, change):
            change.reason = error
            return change

        live_id = self.ids.get(domain.name, key)
        change.live_id = live_id
        row = self.row(domain, live_id)
        if row is None:
            if not stage.creates:
                why = "refused" if key in self.refused.get(domain.name, set()) else "not in HA"
                change.reason = f"{domain.kind} {key} is {why}"
                return change
            return self.plan_create(domain, item, change)
        if key in self.created.get(domain.name, set()):
            change.needs.append((domain.name, key))
        if stage.fields == AREA_SENSORS:
            if error := self.check_sensor_refs(desired):
                change.reason = error
                return change
            # HA checks a sensor only when it changes, so only changes are checked here.
            wanted = resolve_refs(domain, desired, self.ids)
            changed = {name: value for name, value in wanted.items() if value != row.get(name)}
            if error := self.check_sensors(changed):
                change.reason = error
                return change
        return self.plan_update(domain, row, change)

    def plan_create(self, domain: RegistryDomain, item: Row, change: Change) -> Change:
        key = item[domain.key]
        name = change.desired.get("name")
        if not isinstance(name, str):
            change.reason = "not in HA, and the file gives no name to create it with"
            return change
        if holder := self.name_holder(domain, name, None):
            change.reason = f"name {name!r} is in use by {holder}"
            return change

        taken = {row[domain.key] for row in self.sim.get(domain.name, [])}
        slug = simple_slug(name)
        predicted: str | None = next_id(slug, taken) if slug else None
        if slug != key and is_slug(key):
            for temp in (key, key.replace("_", " "), key.replace("_", "-")):
                if self.name_holder(domain, temp, None) is None:
                    change.temp_name = temp
                    predicted = next_id(key, taken)
                    break
        if predicted is None:
            change.note = "HA chooses the id"
        elif predicted != key:
            change.note = f"HA will assign {predicted}"

        change.op = "create"
        change.after = {k: v for k, v in change.desired.items() if v is not None and v != []}
        change.live_id = predicted
        live_id = predicted or key
        self.ids.bind(domain.name, key, live_id)
        self.created.setdefault(domain.name, set()).add(key)
        row: Row = {name: None for name in domain.field_order}
        row.update({k: [] for k in domain.set_fields})
        row.update(resolve_refs(domain, change.after, self.ids))
        row[domain.key] = live_id
        self.sim.setdefault(domain.name, []).append(row)
        return change

    def diff(self, domain: RegistryDomain, row: Row, change: Change) -> Row:
        """Fill *change*'s ``after`` and ``before`` from *row*; return the wanted live values."""
        wanted = resolve_refs(domain, change.desired, self.ids)
        for name, value in change.desired.items():
            have = domain.live_value(row, name, wanted[name])
            if comparable(domain, name, wanted[name]) != comparable(domain, name, have):
                change.after[name] = value
                change.before[name] = have
        return wanted

    def plan_update(self, domain: RegistryDomain, row: Row, change: Change) -> Change:
        wanted = self.diff(domain, row, change)
        new_name = change.after.get("name")
        if isinstance(new_name, str) and normalize_name(new_name) != normalize_name(
            str(row.get("name"))
        ):
            if holder := self.name_holder(domain, new_name, row[domain.key]):
                change.after, change.before = {}, {}
                change.reason = f"name {new_name!r} is in use by {holder}"
                return change
        return self.commit(domain, row, change, wanted)

    def commit(self, domain: RegistryDomain, row: Row, change: Change, wanted: Row) -> Change:
        """Mark *change* an update or unchanged and apply it to the simulated *row*."""
        change.op = "update" if change.after else "unchanged"
        for name in change.after:
            row[name] = domain.stored(row, name, wanted[name])
        return change

    def plan_entities(self, stage: Stage, items: list[Row]) -> list[Change]:
        """Plan every file entity: renames first, then the other fields."""
        domain = ENTITIES
        bindings = self.bindings[domain.name]
        out: list[Change] = []
        matched: list[tuple[Row, Change, Row]] = []
        renames: dict[str, Change] = {}
        for item in items:
            key = domain.item_key(item)
            binding = bindings[key]
            change = Change(stage=stage.number, domain=domain.name, key=key, op="refused")
            change.label = domain.label(item, binding.live)
            change.desired = {k: v for k, v in domain.desired(item).items() if k != "entity_id"}
            if binding.refused:
                change.reason = binding.refused
                out.append(change)
                continue
            error = next(
                (
                    e
                    for name in item
                    if name != "match"
                    if (e := domain.field_error(name, item[name]))
                ),
                None,
            )
            if error:
                change.reason = error
                out.append(change)
                continue
            if binding.live is None:
                change.op = "unmatched"
                out.append(change)
                continue
            row = self.row(domain, binding.live["entity_id"])
            assert row is not None
            new = item.get("entity_id")
            if isinstance(new, str) and new != row["entity_id"]:
                rename = Change(stage=stage.number, domain=domain.name, key=key, op="rename")
                rename.label = row["entity_id"]
                rename.live_id = row["entity_id"]
                rename.registry_id = row.get("id")
                rename.desired = {"entity_id": new}
                renames[key] = rename
            matched.append((item, change, row))
        self.plan_renames(renames)
        out.extend(renames[key] for key in sorted(renames))
        for item, change, row in matched:
            if change.key in renames and renames[change.key].op == "rename":
                change.needs.append((domain.name, change.key))
            held = self.plan_entity(item, change, row)
            if not (change.op == "unchanged" and (change.key in renames or held)):
                out.append(change)
            out.extend(held)
        return out

    def plan_renames(self, renames: dict[str, Change]) -> None:
        """Decide which renames can run and simulate them.

        A target held by an entity that is itself renamed away goes through
        ``<target>_atlas_tmp``: every such entity moves to its temporary id
        first, then on. A target held by anything else, an entity or a
        state with no registry entry, refuses the rename, and so in turn
        any rename onto the id that entity keeps.
        """
        index = self.entity_index()
        states = self.states or {}
        targets = Counter(c.desired["entity_id"] for c in renames.values())
        refused: dict[str, str] = {}
        for key, change in renames.items():
            new = change.desired["entity_id"]
            if targets[new] > 1:
                refused[key] = f"{targets[new]} file items set entity_id {new}"
        while True:
            moving = {c.live_id for k, c in renames.items() if k not in refused}
            more = False
            for key, change in renames.items():
                if key in refused:
                    continue
                new = change.desired["entity_id"]
                temp = new + TEMP_SUFFIX
                if new in index and new not in moving:
                    refused[key] = f"entity_id {new} is held by another entity"
                elif new not in index and new in states:
                    refused[key] = f"entity_id {new} is held by a state with no registry entry"
                elif new in moving and (temp in index or temp in states or temp in targets):
                    refused[key] = f"temporary entity_id {temp} is taken"
                else:
                    change.temp_name = temp if new in moving else None
                    continue
                more = True
            if not more:
                break
        for key, change in renames.items():
            if key in refused:
                change.op, change.reason, change.temp_name = "refused", refused[key], None
        done = [c for k, c in renames.items() if k not in refused]
        rows = [index.pop(str(c.live_id)) for c in done]
        moved = {str(c.live_id): states.pop(str(c.live_id)) for c in done if c.live_id in states}
        for change, row in zip(done, rows, strict=True):
            old, new = str(change.live_id), change.desired["entity_id"]
            change.before = {"entity_id": old}
            change.after = {"entity_id": new}
            row["entity_id"] = new
            index[new] = row
            if old in moved:
                states[new] = {**moved[old], "entity_id": new}
            self.ids.bind(ENTITIES.name, change.key, new)
            self.ids.bind(ENTITY_IDS, new, new)

    def plan_entity(self, item: Row, change: Change, row: Row) -> list[Change]:
        """Plan *item*'s fields other than entity_id on its simulated live *row*.

        Return one refused change for each field HA would reject; the item's
        other fields are planned without it.
        """
        domain = ENTITIES
        change.live_id = change.label = row["entity_id"]
        change.registry_id = row.get("id")
        change.desired = domain.explicit(row, change.desired)
        if error := self.check_refs(domain, change.desired, change):
            change.reason = error
            return []
        wanted = self.diff(domain, row, change)
        held = []
        for name, error in self.field_refusals(row, change).items():
            refusal = Change(
                stage=change.stage,
                domain=change.domain,
                key=change.key,
                op="refused",
                live_id=change.live_id,
                label=change.label,
                desired={name: change.desired.pop(name)},
                reason=error,
            )
            del change.after[name], change.before[name]
            held.append(refusal)
        if error := self.entity_refusal(row, change, wanted):
            change.after, change.before = {}, {}
            change.reason = error
            change.op = "refused"
            return held
        self.commit(domain, row, change, wanted)
        return held

    def field_refusals(self, row: Row, change: Change) -> dict[str, str]:
        """The fields of *change* that HA would reject for entity *row*, with why."""
        after = change.after
        out = {}
        for scope, category in (after.get("categories") or {}).items():
            if category is not None and category not in self.categories.get(scope, set()):
                out["categories"] = f"categories {scope} {category}: no such category in HA"
                break
        # HA checks only an enable; a user disable replaces one that HA set.
        if "disabled_by" in after and after["disabled_by"] is None:
            device = self.row(DEVICES, row["device_id"]) if row.get("device_id") else None
            if device is not None and device["disabled_by"]:
                held = device["disabled_by"]
                out["disabled_by"] = f"its device is disabled ({held}); enable the device first"
            elif row.get("disabled_by") != "user":
                held = row["disabled_by"]
                out["disabled_by"] = f"disabled_by is {held} in HA; apply clears only user"
        if "hidden_by" in after and row.get("hidden_by") not in (None, "user"):
            held = row["hidden_by"]
            out["hidden_by"] = f"hidden_by is {held} in HA; apply sets or clears only user"
        return out

    def plan_energy(self, stage: Stage, items: list[Row], rows: list[Row], mode: str) -> Section:
        """Plan the energy lists against the live prefs, after the entity renames."""
        doc = items[0] if items else {}
        before = rows[0] if rows else {}
        files = {ENTITIES.file_entity_id(item) for item in self.docs.get("entities", [])}

        def resolve(entity_id: str) -> str:
            return self.ids.resolve(ENTITY_IDS, entity_id)

        def unbound(entity_id: str) -> str | None:
            if entity_id in files and self.ids.get(ENTITY_IDS, entity_id) is None:
                return f"the file's entity {entity_id} matches no live entity"
            return None

        def waits(entity_id: str) -> str | None:
            if reason := unbound(entity_id):
                return reason
            live_id = resolve(entity_id)
            if live_id not in self.entity_index() and live_id not in (self.states or {}):
                return f"{live_id} is not in HA"
            return None

        changes, energy = plan_energy(
            doc,
            before,
            mode=mode,
            fmt=grid_format(self.ha_version, before),
            resolve=resolve,
            version=parse_ha_version(self.ha_version),
            waits=waits,
            unbound=unbound,
        )
        return Section(
            stage=stage.number,
            title=stage.title,
            domain=stage.domain.name,
            in_file=sum(len(entries) for entries in doc.values()),
            live=sum(len(before.get(name) or []) for name in LISTS),
            changes=changes,
            energy=energy,
        )

    def entity_refusal(self, row: Row, change: Change, wanted: Row) -> str | None:
        """Why HA would reject *change*'s writes to entity *row* as a whole, or None."""
        after = change.after
        if "area_id" in after or "name" in after:
            area = wanted["area_id"] if "area_id" in after else row.get("area_id")
            name = wanted["name"] if "name" in after else row.get("name")
            own = name if name is not None else row.get("original_name")
            if area is not None and row.get("device_id") and not own:
                return (
                    "an entity without a name of its own cannot have an area of its own; "
                    "set the area on its device or give the entity a name"
                )
        return None


def _sets_area_sensors(docs: dict[str, list[Row]], applied: set[str]) -> bool:
    if "areas" not in applied:
        return False
    return any(
        item.get(name) is not None for item in docs.get("areas", []) for name in AREA_SENSORS
    )


def domains_to_read(docs: dict[str, list[Row]], applied: set[str]) -> list[str]:
    """Domains apply reads: the applied ones and every domain they depend on.

    Entities are read too when areas set sensors, so a sensor follows the
    file's entity to its live entity_id; energy depends on them for the same.
    """
    names = set(applied)
    for name in applied:
        names.update(domain_named(name).depends_on)
    if docs.get("entities") and _sets_area_sensors(docs, applied):
        names.add("entities")
    return list(dict.fromkeys(s.domain.name for s in STAGES if s.domain.name in names))


def needs_states(docs: dict[str, list[Row]], applied: set[str]) -> bool:
    """Whether apply reads the states.

    They check area sensors, rename targets and the entities that new energy
    entries reference.
    """
    if "entities" in applied and docs.get("entities"):
        return True
    if ENERGY.name in applied and docs.get(ENERGY.name):
        return True
    return _sets_area_sensors(docs, applied)


def category_scopes(docs: dict[str, list[Row]], applied: set[str]) -> list[str]:
    """The category scopes the applied entities name, which apply reads categories for."""
    if "entities" not in applied:
        return []
    scopes: set[str] = set()
    for item in docs.get("entities", []):
        categories = item.get("categories")
        if isinstance(categories, dict):
            scopes.update(k for k in categories if isinstance(k, str))
    return sorted(scopes)


def build_plan(
    docs: dict[str, list[Row]],
    applied: set[str],
    live: dict[str, list[Row]],
    states: dict[str, Row] | None,
    *,
    url: str,
    ha_version: str | None,
    categories: dict[str, set[str]] | None = None,
    energy_mode: str = "merge",
) -> Plan:
    """Plan every stage for the *applied* domains of an atlas against *live* rows.

    *categories* holds the live category ids per scope that entities name;
    *energy_mode* is ``merge`` or ``replace``.
    """
    planner = _Planner(
        docs=docs,
        applied=applied,
        states=dict(states) if states is not None else None,
        sim=copy.deepcopy(live),
        categories=categories or {},
        ha_version=ha_version,
    )
    for name in domains_to_read(docs, applied):
        if name not in docs or name == ENERGY.name:
            continue
        domain = registry(name)
        rows = live.get(name, [])
        if isinstance(domain, DeviceDomain):
            bindings = match_devices(domain, docs[name], rows)
        elif isinstance(domain, EntityDomain):
            bindings = match_entities(domain, docs[name], rows)
            for item in docs[name]:
                binding = bindings[domain.item_key(item)]
                file_id = domain.file_entity_id(item)
                if file_id is not None and binding.live is not None and binding.refused is None:
                    planner.ids.bind(ENTITY_IDS, file_id, binding.live["entity_id"])
        else:
            bindings = match(domain, docs[name], rows)
        planner.bindings[name] = bindings
        for key, binding in bindings.items():
            if binding.live is not None and binding.refused is None:
                planner.ids.bind(name, key, binding.live[domain.live_key])

    sections: list[Section] = []
    for stage in STAGES:
        name = stage.domain.name
        if name not in applied or name not in docs:
            continue
        items = docs[name]
        if isinstance(stage.domain, EnergyDomain):
            sections.append(planner.plan_energy(stage, items, live.get(name, []), energy_mode))
            continue
        sensors = stage.fields == AREA_SENSORS
        if sensors:
            items = [item for item in items if any(f in item for f in stage.fields)]
        section = Section(
            stage=stage.number,
            title=stage.title,
            domain=name,
            in_file=len(items),
            live=None if sensors else len(live.get(name, [])),
        )
        if isinstance(stage.domain, EntityDomain):
            section.changes.extend(planner.plan_entities(stage, items))
            sections.append(section)
            continue
        for item in items:
            change = planner.plan_item(stage, item)
            if change.op == "refused" and planner.ids.get(name, change.key) is None:
                planner.refused.setdefault(name, set()).add(change.key)
            section.changes.append(change)
        sections.append(section)
    return Plan(url=url, ha_version=ha_version, sections=sections, ids=planner.ids)
