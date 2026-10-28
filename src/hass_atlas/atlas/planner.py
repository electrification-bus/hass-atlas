"""Match atlas items to live ones and plan what apply writes.

The plan covers every stage before anything is written: each stage plans
against the live rows as earlier stages would leave them, so a dry run shows
the ids HA will assign and the references rewritten to them.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from hass_atlas.atlas.domains.areas import AREAS
from hass_atlas.atlas.domains.base import RegistryDomain, Row
from hass_atlas.atlas.domains.devices import DEVICES, PAIR_FIELDS, TIE_FIELDS, DeviceDomain
from hass_atlas.atlas.domains.floors import FLOORS
from hass_atlas.atlas.domains.labels import LABELS
from hass_atlas.atlas.plan import Change, Plan, Section, registry, resolve_refs
from hass_atlas.atlas.refs import IdMap

AREA_SENSORS = ("temperature_entity_id", "humidity_entity_id")
_SENSOR_CLASS = {"temperature_entity_id": "temperature", "humidity_entity_id": "humidity"}


@dataclass(frozen=True)
class Stage:
    number: int
    domain: RegistryDomain
    title: str
    fields: tuple[str, ...]
    creates: bool = True


def _fields(domain: RegistryDomain, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    return tuple(name for name in domain.field_order[1:] if name not in exclude)


# Numbered as in the full apply order, where entities (4) come before the
# area sensors: HA checks a sensor's device class when it is set.
STAGES: tuple[Stage, ...] = (
    Stage(1, LABELS, "labels", _fields(LABELS)),
    Stage(1, FLOORS, "floors", _fields(FLOORS)),
    Stage(2, AREAS, "areas", _fields(AREAS, AREA_SENSORS)),
    Stage(3, DEVICES, "devices", DEVICES.plan_fields, creates=False),
    Stage(5, AREAS, "area sensors", AREA_SENSORS, creates=False),
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
    """*value* as HA stores it: set fields sorted, aliases stripped, null lists empty."""
    if name in domain.set_fields:
        items = value if isinstance(value, list) else []
        if name == "aliases":
            return sorted({v.strip() for v in items if isinstance(v, str) and v.strip()})
        return sorted(set(items))
    return value


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


@dataclass
class _Planner:
    docs: dict[str, list[Row]]
    applied: set[str]
    states: dict[str, Row] | None
    sim: dict[str, list[Row]]
    ids: IdMap = field(default_factory=IdMap)
    bindings: dict[str, dict[str, Binding]] = field(default_factory=dict)
    created: dict[str, set[str]] = field(default_factory=dict)
    refused: dict[str, set[str]] = field(default_factory=dict)

    def row(self, domain: RegistryDomain, live_id: str | None) -> Row | None:
        for row in self.sim.get(domain.name, []):
            if row[domain.live_key] == live_id:
                return row
        return None

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
        return self.plan_update(domain, row, change)

    def plan_item(self, stage: Stage, item: Row) -> Change:
        domain = stage.domain
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
            # HA checks a sensor only when it changes, so only changes are checked here.
            changed = {name: value for name, value in desired.items() if value != row.get(name)}
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

    def plan_update(self, domain: RegistryDomain, row: Row, change: Change) -> Change:
        wanted = resolve_refs(domain, change.desired, self.ids)
        for name, value in change.desired.items():
            if comparable(domain, name, wanted[name]) != comparable(domain, name, row.get(name)):
                change.after[name] = value
                change.before[name] = row.get(name)
        new_name = change.after.get("name")
        if isinstance(new_name, str) and normalize_name(new_name) != normalize_name(
            str(row.get("name"))
        ):
            if holder := self.name_holder(domain, new_name, row[domain.key]):
                change.after, change.before = {}, {}
                change.reason = f"name {new_name!r} is in use by {holder}"
                return change
        change.op = "update" if change.after else "unchanged"
        for name in change.after:
            empty = wanted[name] is None and name in domain.set_fields
            row[name] = [] if empty else wanted[name]
        return change


def domains_to_read(docs: dict[str, list[Row]], applied: set[str]) -> list[str]:
    """Domains apply reads: the applied ones and every domain they reference."""
    names = set(applied)
    for name in applied:
        names.update(target for _, target in registry(name).refs)
    return list(dict.fromkeys(s.domain.name for s in STAGES if s.domain.name in names))


def needs_states(docs: dict[str, list[Row]], applied: set[str]) -> bool:
    if "areas" not in applied:
        return False
    return any(
        item.get(name) is not None for item in docs.get("areas", []) for name in AREA_SENSORS
    )


def build_plan(
    docs: dict[str, list[Row]],
    applied: set[str],
    live: dict[str, list[Row]],
    states: dict[str, Row] | None,
    *,
    url: str,
    ha_version: str | None,
) -> Plan:
    """Plan every stage for the *applied* domains of an atlas against *live* rows."""
    planner = _Planner(docs=docs, applied=applied, states=states, sim=copy.deepcopy(live))
    for name in domains_to_read(docs, applied):
        if name not in docs:
            continue
        domain = registry(name)
        rows = live.get(name, [])
        if isinstance(domain, DeviceDomain):
            bindings = match_devices(domain, docs[name], rows)
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
        for item in items:
            change = planner.plan_item(stage, item)
            if change.op == "refused" and planner.ids.get(name, change.key) is None:
                planner.refused.setdefault(name, set()).add(change.key)
            section.changes.append(change)
        sections.append(section)
    return Plan(url=url, ha_version=ha_version, sections=sections, ids=planner.ids)
