"""Run a confirmed plan. The only code in hass-atlas that sends atlas write commands.

Per stage: re-read the stage's domains and skip, as a conflict, any change
whose recorded ``before`` no longer matches; write the rest, journaling each
write; then re-read and compare with the file. A failed write stops the run
after its stage, and a verify mismatch stops it at once. A write that timed
out or lost the connection is journaled and shown as ``unknown``, since HA
may have applied it, and ends the run at once.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import groupby
from typing import Any

from hass_atlas.atlas.domains.base import Client, RegistryDomain, Row
from hass_atlas.atlas.plan import Change, Plan, Section, change_lines, fmt, registry, resolve_refs
from hass_atlas.atlas.planner import comparable, normalize_name, stage_fields
from hass_atlas.atlas.snapshot import Journal
from hass_atlas.ha_client import HAClientError, HAConnectionLost

Echo = Callable[[str], None]


def _by_id(domain: RegistryDomain, rows: list[Row]) -> dict[str, Row]:
    return {row[domain.key]: row for row in rows}


def _write_payload(domain: RegistryDomain, values: Row, create: bool) -> Row:
    """*values* as HA's create or update schema takes them.

    On create a null or empty value is left out, since HA rejects a null
    ``floor_id`` or ``icon`` there and defaults every field to empty anyway.
    On update a null list clears it.
    """
    if create:
        return {k: v for k, v in values.items() if v is not None and v != []}
    return {k: ([] if v is None and k in domain.set_fields else v) for k, v in values.items()}


class Executor:
    def __init__(self, client: Client, plan: Plan, journal: Journal, echo: Echo) -> None:
        self.client = client
        self.plan = plan
        self.journal = journal
        self.echo = echo

    async def _read(self, names: list[str]) -> dict[str, dict[str, Row]]:
        out = {}
        for name in names:
            domain = registry(name)
            out[name] = _by_id(domain, await domain.fetch(self.client))
        return out

    async def _send(self, change: Change, op: str, command: str, payload: Row) -> Any:
        record: dict[str, Any] = {
            "stage": change.stage,
            "domain": change.domain,
            "op": op,
            "key": change.key,
            "payload": payload,
            "before": {"update": change.before, "rename": {"name": change.temp_name}}.get(op),
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
        for name, before in change.before.items():
            now = current.get(name)
            if comparable(domain, name, now) != comparable(domain, name, before):
                return f"{name} changed in HA since the plan ({fmt(before)} -> {fmt(now)})"
        return None

    def _unmet(self, change: Change) -> str | None:
        for target, key in change.needs:
            if not any(
                c.domain == target and c.key == key and c.op == "create" and c.status == "done"
                for c in self.plan.changes
            ):
                return f"{registry(target).kind} {key} was not created"
        return None

    async def _apply(self, change: Change) -> None:
        domain = registry(change.domain)
        values = resolve_refs(domain, change.after, self.plan.ids)
        if change.op == "update":
            live_id = self.plan.ids.resolve(change.domain, change.key)
            payload = {domain.key: live_id, **_write_payload(domain, values, create=False)}
            await self._send(change, "update", domain.command("update"), payload)
            return
        payload = _write_payload(domain, values, create=True)
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
        where = f"{domain.kind} {change.key}"
        if row is None:
            return [f"{where}: {live_id} is not in HA"]
        wanted = resolve_refs(domain, change.desired, self.plan.ids)
        diffs = []
        for name in stage_fields(change):
            if name not in wanted:
                continue
            want = comparable(domain, name, wanted[name])
            have = comparable(domain, name, row.get(name))
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
            for change in writes:
                reason = self._unmet(change)
                status = "skipped"
                if reason is None and (reason := self._conflict(change, live[change.domain])):
                    status = "conflict"
                if reason is not None:
                    change.status, change.reason = status, reason
                else:
                    try:
                        await self._apply(change)
                        change.status = "done"
                    except HAConnectionLost as exc:
                        change.status, change.reason = "unknown", str(exc)
                        for line in change_lines(change, self.plan.ids):
                            self.echo(line)
                        raise
                    except HAClientError as exc:
                        change.status, change.reason = "failed", str(exc)
                        failed = True
                for line in change_lines(change, self.plan.ids):
                    self.echo(line)

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
