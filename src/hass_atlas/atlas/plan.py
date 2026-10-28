"""The changes an apply makes, and how they are printed."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from hass_atlas.atlas import DOMAINS, FORMAT
from hass_atlas.atlas.domains.base import RegistryDomain, Row
from hass_atlas.atlas.refs import IdMap

WRITES = ("create", "update")
# How a write ends when it is not done.
ENDED = ("failed", "conflict", "skipped", "unknown")

SYMBOLS = {
    "create": "+",
    "update": "~",
    "unchanged": "=",
    "refused": "!",
    "unmatched": "?",
    "conflict": "!",
    "skipped": "!",
    "failed": "x",
    "unknown": "?",
}


def registry(name: str) -> RegistryDomain:
    for domain in DOMAINS:
        if domain.name == name and isinstance(domain, RegistryDomain):
            return domain
    raise KeyError(name)


def resolve_refs(domain: RegistryDomain, values: Row, ids: IdMap) -> Row:
    """Return *values* with the ids of other domains rewritten to their live ids."""
    out = dict(values)
    for name, target in domain.refs:
        value = out.get(name)
        if isinstance(value, str):
            out[name] = ids.resolve(target, value)
        elif isinstance(value, list):
            out[name] = [ids.resolve(target, v) if isinstance(v, str) else v for v in value]
    return out


@dataclass
class Change:
    """One file item in one stage.

    ``desired`` holds the item's fields for this stage as the file has them;
    ``after`` the subset to write and ``before`` their live values when the
    plan was made. References in both stay in the file's terms until they are
    written, so a live id learned in an earlier stage is used. ``label`` is
    shown in place of ``key`` when the key is not readable.
    """

    stage: int
    domain: str
    key: str
    op: str
    live_id: str | None = None
    desired: Row = field(default_factory=dict)
    after: Row = field(default_factory=dict)
    before: Row = field(default_factory=dict)
    label: str | None = None
    temp_name: str | None = None
    by_name: bool = False
    reason: str | None = None
    note: str | None = None
    needs: list[tuple[str, str]] = field(default_factory=list)
    status: str = "planned"

    @property
    def writes(self) -> bool:
        return self.op in WRITES

    @property
    def state(self) -> str:
        """What the line shows: the op, or how a write ended."""
        return self.status if self.status in ENDED else self.op


@dataclass
class Section:
    """One domain's changes within a stage."""

    stage: int
    title: str
    domain: str
    in_file: int
    live: int | None
    changes: list[Change] = field(default_factory=list)


@dataclass
class Plan:
    url: str
    ha_version: str | None
    sections: list[Section]
    ids: IdMap

    @property
    def changes(self) -> list[Change]:
        return [change for section in self.sections for change in section.changes]

    def count(self, state: str) -> int:
        return sum(1 for change in self.changes if change.state == state)

    @property
    def writes(self) -> bool:
        return any(change.writes for change in self.changes)

    @property
    def refused(self) -> bool:
        """Whether items were refused, skipped, in conflict or unmatched."""
        return any(
            c.state in ("refused", "conflict", "skipped", "unmatched") for c in self.changes
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ha_version": self.ha_version,
            "format": FORMAT,
            "ids": self.ids.as_dict(),
            "changes": [asdict(change) for change in self.changes],
        }


_BARE = re.compile(r"[a-z0-9_.:-]+")


def fmt(value: Any) -> str:
    """A value as plan lines show it: ids bare, other strings quoted."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value if _BARE.fullmatch(value) else json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(fmt(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _field(name: str, value: Any) -> str:
    """A field's value; names are always quoted, so they never pass for an id."""
    if name in ("name", "name_by_user") and isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return fmt(value)


def _line(symbol: str, kind: str, key: str, text: str) -> str:
    return f"  {symbol} {kind:<7} {key:<17} {text}".rstrip()


def _ordered(domain: RegistryDomain, values: Row) -> list[tuple[str, Any]]:
    return [(name, values[name]) for name in domain.plan_fields if name in values]


def _text(change: Change, domain: RegistryDomain, ids: IdMap) -> list[str]:
    after = resolve_refs(domain, change.after, ids)
    before = change.before
    state = change.state
    if state in ("refused", *ENDED):
        lines = [f"{state}: {change.reason}"]
    elif state == "unmatched":
        lines = ["unmatched: not in HA"]
    elif change.op == "create":
        fields = [f"{k}={_field(k, v)}" for k, v in _ordered(domain, after) if k != "name"]
        if change.temp_name is not None:
            head = f"create as {json.dumps(change.temp_name)}, rename -> {_field('name', after['name'])}"
        else:
            head = f"create name={_field('name', after['name'])}"
        lines = [" ".join([head, *fields])]
    elif change.op == "update":
        lines = [
            f"{k}: {_field(k, before.get(k))} -> {_field(k, v)}"
            for k, v in _ordered(domain, after)
        ]
    else:
        lines = ["unchanged"]
    notes = []
    if change.by_name:
        notes.append(f"matched live {change.live_id} by name")
    if change.note:
        notes.append(change.note)
    if notes:
        lines[0] += f"  ({'; '.join(notes)})"
    return lines


def change_lines(change: Change, ids: IdMap) -> list[str]:
    domain = registry(change.domain)
    text = _text(change, domain, ids)
    first = _line(SYMBOLS[change.state], domain.kind, change.label or change.key, text[0])
    indent = " " * (len(first) - len(text[0]))
    return [first, *(indent + line for line in text[1:])]


def section_header(section: Section) -> str:
    counts = f"{section.in_file} in file"
    if section.live is not None:
        counts += f", {section.live} live"
    return f"Stage {section.stage}  {section.title}  ({counts})"


def render(plan: Plan, verbose: bool = False) -> list[str]:
    """The plan, one line per change; unchanged items are counted unless *verbose*."""
    version = f"HA {plan.ha_version}, " if plan.ha_version else ""
    lines = [f"Plan for {plan.url}  ({version}atlas format {FORMAT})"]
    for section in plan.sections:
        lines.append(section_header(section))
        domain = registry(section.domain)
        quiet = 0
        unmatched: Counter[str] = Counter()
        for change in section.changes:
            if change.op == "unchanged" and not verbose and not change.by_name:
                quiet += 1
                continue
            if change.op == "unmatched" and not verbose:
                unmatched[domain.group(change.key)] += 1
                continue
            lines.extend(change_lines(change, plan.ids))
        if unmatched:
            groups = ", ".join(f"{group} {n}" for group, n in sorted(unmatched.items()))
            total = f"{unmatched.total()} unmatched:"
            lines.append(_line("?", domain.kind, total, f"{groups}  (-v lists them)"))
        if quiet:
            lines.append(_line("=", domain.kind, f"{quiet} unchanged", ""))
    return lines


def summary(plan: Plan, done: bool = False) -> str:
    """The count line; *done* reports a run instead of a plan."""
    if done:
        created = sum(1 for c in plan.changes if c.op == "create" and c.status == "done")
        updated = sum(1 for c in plan.changes if c.op == "update" and c.status == "done")
        parts = [f"{created} created", f"{updated} updated"]
        extra: tuple[str, ...] = (
            "refused",
            "conflict",
            "skipped",
            "failed",
            "unknown",
            "unmatched",
        )
    else:
        parts = [f"{plan.count('create')} create", f"{plan.count('update')} update"]
        extra = ("refused", "conflict", "unmatched")
    parts += [f"{plan.count(state)} {state}" for state in extra if plan.count(state)]
    parts.append(f"{plan.count('unchanged')} unchanged")
    return "Summary: " + ", ".join(parts) + "."
