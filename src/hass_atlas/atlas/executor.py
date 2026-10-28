"""Run a confirmed plan. The only code in hass-atlas that sends atlas write commands.

Per stage: re-read the stage's domains and skip, as a conflict, any change
whose recorded ``before`` no longer matches; write the rest, journaling each
write; then re-read and compare with the file. A failed write stops the run
after its stage, and a verify mismatch stops it at once. A write that timed
out or lost the connection is journaled and shown as ``unknown``, since HA
may have applied it, and ends the run at once.

Entity renames run before the stage's other writes, in two passes when a
target is held by another entity being renamed: every such entity moves to
its temporary entity_id first, then on to its target. A rename whose target
stays held, because the rename of the entity holding it did not run, is
skipped, and an entity already at its temporary id moves back. The stage is
read again after the renames, so the other writes find the entities' new
ids. An entity change is checked against the registry ``id`` it was planned
for, so it is never written to another entity that took its entity_id.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import groupby
from typing import Any

from hass_atlas.atlas.domains.base import Client, RegistryDomain, Row
from hass_atlas.atlas.plan import Change, Plan, Section, change_lines, fmt, registry, resolve_refs
from hass_atlas.atlas.planner import comparable, normalize_name, stage_fields
from hass_atlas.atlas.refs import ENTITY_IDS
from hass_atlas.atlas.snapshot import Journal
from hass_atlas.ha_client import HAClientError, HAConnectionLost

Echo = Callable[[str], None]


def _by_id(domain: RegistryDomain, rows: list[Row]) -> dict[str, Row]:
    return {row[domain.live_key]: row for row in rows}


def _create_payload(values: Row) -> Row:
    """*values* as HA's create schema takes them.

    A null or empty value is left out, since HA rejects a null ``floor_id``
    or ``icon`` there and defaults every field to empty anyway.
    """
    return {k: v for k, v in values.items() if v is not None and v != []}


class Executor:
    def __init__(self, client: Client, plan: Plan, journal: Journal, echo: Echo) -> None:
        self.client = client
        self.plan = plan
        self.journal = journal
        self.echo = echo
        self.restart: list[str] = []
        """Entities enabled that load only once Home Assistant restarts."""
        self.reload: dict[str, Any] = {}
        """Entities enabled that load once their config entry reloads, with HA's delay."""
        self._done: set[tuple[str, str]] = set()
        """The creates and entity renames that ran, which later changes may need."""

    async def _read(self, names: list[str]) -> dict[str, dict[str, Row]]:
        out = {}
        for name in names:
            domain = registry(name)
            out[name] = _by_id(domain, await domain.fetch(self.client))
        return out

    async def _send(
        self, change: Change, op: str, command: str, payload: Row, before: Row | None = None
    ) -> Any:
        if before is None:
            before = {"update": change.before, "rename": {"name": change.temp_name}}.get(op)
        record: dict[str, Any] = {
            "stage": change.stage,
            "domain": change.domain,
            "op": op,
            "key": change.key,
            "payload": payload,
            "before": before,
        }
        try:
            result = await self.client.send_command(command, **payload)
        except HAConnectionLost as exc:
            record["result"] = {"unknown": str(exc)}
            self.journal.append(record)
            raise
        except HAClientError as exc:
            record["result"] = {"error": str(exc), "code": exc.code}
            self.journal.append(record)
            raise
        record["result"] = "ok"
        if op == "create" and isinstance(result, dict):
            record["live_id"] = result.get(registry(change.domain).key)
        self.journal.append(record)
        return result

    def _conflict(self, change: Change, live: dict[str, Row]) -> str | None:
        domain = registry(change.domain)
        if change.op == "create":
            names = {normalize_name(n) for n in (change.after["name"], change.temp_name) if n}
            for live_id, row in live.items():
                taken = isinstance(row.get("name"), str) and normalize_name(row["name"]) in names
                if live_id in (change.key, change.live_id) or taken:
                    return f"{live_id} was created in HA since the plan"
            return None
        live_id = self.plan.ids.resolve(change.domain, change.key)
        current = live.get(live_id)
        if current is None:
            return f"{live_id} was removed from HA since the plan"
        if change.registry_id is not None and current.get("id") != change.registry_id:
            return f"{live_id} is another entity since the plan"
        for name, before in change.before.items():
            now = domain.live_value(current, name, change.desired.get(name))
            if comparable(domain, name, now) != comparable(domain, name, before):
                return f"{name} changed in HA since the plan ({fmt(before)} -> {fmt(now)})"
        return None

    def _rename_conflict(
        self, change: Change, live: dict[str, Row], moving: set[str]
    ) -> str | None:
        old = change.before["entity_id"]
        if old not in live:
            return f"{old} was removed from HA since the plan"
        if change.registry_id is not None and live[old].get("id") != change.registry_id:
            return f"{old} is another entity since the plan"
        for target in (change.after["entity_id"], change.temp_name):
            if target is not None and target in live and target not in moving:
                return f"{target} was registered in HA since the plan"
        return None

    def _unmet(self, change: Change) -> str | None:
        for target, key in change.needs:
            if (target, key) not in self._done:
                if target == "entities":  # entities are never created, only renamed
                    return "its rename did not run"
                return f"{registry(target).kind} {key} was not created"
        return None

    def _collect(self, entity_id: str, result: Any) -> None:
        """Note an enabled entity's ``require_restart`` or ``reload_delay``."""
        if not isinstance(result, dict):
            return
        if result.get("require_restart"):
            self.restart.append(entity_id)
        elif "reload_delay" in result:
            self.reload[entity_id] = result["reload_delay"]

    def _bind_entity(self, change: Change, live_id: str) -> None:
        self.plan.ids.bind(change.domain, change.key, live_id)
        self.plan.ids.bind(ENTITY_IDS, change.desired["entity_id"], live_id)

    async def _rename(self, change: Change, source: str, target: str) -> None:
        payload = {"entity_id": source, "new_entity_id": target}
        command = registry(change.domain).command("update")
        await self._send(change, "rename", command, payload, before={"entity_id": source})
        self._bind_entity(change, target)

    async def _run_renames(self, renames: list[Change], live: dict[str, Row]) -> tuple[bool, bool]:
        """Run a stage's entity renames; return whether one failed and whether any was sent."""
        for change in renames:
            # The plan bound each entity to its target; it is where it was until renamed.
            self._bind_entity(change, change.before["entity_id"])
        held: dict[int, tuple[str, str]] = {}
        more = True
        while more:
            # Repeated until stable: a rename onto the id of one held back is held back too.
            moving = {c.before["entity_id"] for c in renames if id(c) not in held}
            kept = {c.before["entity_id"] for c in renames if id(c) in held}
            more = False
            for change in renames:
                if id(change) in held:
                    continue
                new = change.after["entity_id"]
                if reason := self._unmet(change):
                    held[id(change)] = ("skipped", reason)
                elif new in kept and new in live:
                    held[id(change)] = ("skipped", f"the rename of {new} did not run")
                elif reason := self._rename_conflict(change, live, moving):
                    held[id(change)] = ("conflict", reason)
                else:
                    continue
                more = True
        for change in renames:
            if id(change) in held:
                change.status, change.reason = held[id(change)]
                self._echo(change)
        ready = [c for c in renames if id(c) not in held]
        failed, sent = False, bool(ready)
        left: set[str] = set()
        second: list[Change] = []
        for change in ready:
            old, temp = change.before["entity_id"], change.temp_name
            if await self._try_rename(change, old, temp or change.after["entity_id"]):
                left.add(old)
                if temp is not None:
                    second.append(change)
                else:
                    self._renamed(change)
            else:
                failed = True
        # An entity whose target is still held moves back, which holds its own id in turn.
        stays = {c.before["entity_id"] for c in renames if c.before["entity_id"] in live} - left
        back: list[Change] = []
        while blocked := [
            c
            for c in second
            if c.after["entity_id"] in stays and c.before["entity_id"] not in stays
        ]:
            back += blocked
            stays.update(c.before["entity_id"] for c in blocked)
        for change in second:
            old, new, temp = (
                change.before["entity_id"],
                change.after["entity_id"],
                change.temp_name,
            )
            assert temp is not None
            if all(change is not c for c in back):
                if await self._try_rename(change, temp, new):
                    self._renamed(change)
                else:
                    failed = True
            elif await self._try_rename(change, temp, old):
                change.status, change.reason = "skipped", f"{new} is still held; moved back"
                self._echo(change)
            else:
                failed = True
        return failed, sent

    async def _try_rename(self, change: Change, source: str, target: str) -> bool:
        """Rename an entity; on an HA error mark *change* failed and return False."""
        try:
            await self._rename(change, source, target)
        except HAConnectionLost as exc:
            change.status, change.reason = "unknown", str(exc)
            self._echo(change)
            raise
        except HAClientError as exc:
            change.status, change.reason = "failed", str(exc)
            self._echo(change)
            return False
        return True

    def _renamed(self, change: Change) -> None:
        change.status = "done"
        self._done.add((change.domain, change.key))
        self._echo(change)

    def _echo(self, change: Change) -> None:
        for line in change_lines(change, self.plan.ids):
            self.echo(line)

    async def _apply(self, change: Change, row: Row | None) -> None:
        domain = registry(change.domain)
        values = resolve_refs(domain, change.after, self.plan.ids)
        if change.op == "update":
            live_id = self.plan.ids.resolve(change.domain, change.key)
            for payload in domain.update_payloads(live_id, row, values):
                result = await self._send(change, "update", domain.command("update"), payload)
                self._collect(live_id, result)
            return
        payload = _create_payload(values)
        if change.temp_name is not None:
            payload["name"] = change.temp_name
        result = await self._send(change, "create", domain.command("create"), payload)
        live_id = str(result[domain.key])
        self.plan.ids.bind(change.domain, change.key, live_id)
        if live_id != change.live_id:
            change.note = f"HA assigned {live_id}; later references use it"
        elif live_id != change.key:
            change.note = f"HA assigned {live_id}"
        else:
            change.note = None
        if change.temp_name is not None:
            rename = {domain.key: live_id, "name": values["name"]}
            await self._send(change, "rename", domain.command("update"), rename)

    def _verify(self, change: Change, live: dict[str, Row]) -> list[str]:
        domain = registry(change.domain)
        live_id = self.plan.ids.resolve(change.domain, change.key)
        row = live.get(live_id)
        if change.registry_id is not None:
            row = next((r for r in live.values() if r.get("id") == change.registry_id), None)
        where = f"{domain.kind} {change.label or change.key}"
        if row is None:
            return [f"{where}: {live_id} is not in HA"]
        wanted = resolve_refs(domain, change.desired, self.plan.ids)
        diffs = []
        for name in stage_fields(change):
            if name not in wanted:
                continue
            want = comparable(domain, name, wanted[name])
            have = comparable(domain, name, domain.live_value(row, name, wanted[name]))
            if want != have:
                diffs.append(f"{where}: {name}: expected {fmt(want)}, HA has {fmt(have)}")
        return diffs

    async def run(self) -> int:
        """Run every stage; return 1 on a failed write or verify, 3 if items were skipped.

        A lost connection or timeout during a write raises HAConnectionLost.
        """
        sections = sorted(self.plan.sections, key=lambda s: s.stage)
        for number, group in groupby(sections, key=lambda s: s.stage):
            stage: list[Section] = list(group)
            writes = [c for s in stage for c in s.changes if c.writes]
            if not writes:
                continue
            names = list(dict.fromkeys(s.domain for s in stage))
            self.echo(f"Stage {number}  {', '.join(s.title for s in stage)}")
            live = await self._read(names)
            failed = False
            renames = [c for c in writes if c.op == "rename"]
            if renames:
                failed, sent = await self._run_renames(renames, live["entities"])
                if sent:
                    live = await self._read(names)
            for change in writes:
                if change.op == "rename":
                    continue
                reason = self._unmet(change)
                status = "skipped"
                if reason is None and (reason := self._conflict(change, live[change.domain])):
                    status = "conflict"
                if reason is not None:
                    change.status, change.reason = status, reason
                else:
                    try:
                        row = live[change.domain].get(
                            self.plan.ids.resolve(change.domain, change.key)
                        )
                        await self._apply(change, row)
                        change.status = "done"
                        if change.op == "create":
                            self._done.add((change.domain, change.key))
                    except HAConnectionLost as exc:
                        change.status, change.reason = "unknown", str(exc)
                        self._echo(change)
                        raise
                    except HAClientError as exc:
                        change.status, change.reason = "failed", str(exc)
                        failed = True
                self._echo(change)

            live = await self._read(names)
            diffs = [
                d for c in writes if c.status == "done" for d in self._verify(c, live[c.domain])
            ]
            if diffs:
                self.echo(f"Verify failed after stage {number}; stopping:")
                for diff in diffs:
                    self.echo(f"  {diff}")
                return 1
            if failed:
                self.echo(f"A write failed in stage {number}; later stages were not run.")
                return 1
        return 3 if self.plan.refused else 0
