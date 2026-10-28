"""The Domain protocol and the registry-backed domains built on it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Row = dict[str, Any]


class Client(Protocol):
    async def send_command(self, msg_type: str, **kwargs: Any) -> Any: ...


@dataclass
class ExportContext:
    """What an export can see beyond its own rows, and where it leaves warnings."""

    ha_version: str | None = None
    rows: dict[str, list[Row]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Domain(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def filename(self) -> str: ...

    @property
    def depends_on(self) -> tuple[str, ...]: ...

    @property
    def field_order(self) -> tuple[str, ...]: ...

    async def fetch(self, client: Client) -> list[Row]: ...

    def export(self, rows: list[Row], ctx: ExportContext) -> list[Row]: ...

    def key_error(self, item: Row) -> str | None: ...

    def item_key(self, item: Row) -> str: ...


class FlowList(list[Any]):
    """A list that YAML output writes on one line, such as an identifier pair."""


def has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


@dataclass
class RegistryDomain:
    """A registry whose items are keyed by ``field_order[0]``.

    Export keeps only the fields in ``field_order`` that hold a value, so
    timestamps, pictures and fields a newer HA adds are never written.
    ``set_fields`` are stored as sets in HA, so their order is arbitrary and
    they are sorted. With ``registry_order`` the items keep the order HA lists
    them in (the user's order); otherwise they are sorted by key. ``refs``
    maps a field to the domain whose ids it holds. ``live_key`` names the id
    field of live rows and ``update_key`` the id argument of the update
    command, when they differ from the file's key.
    """

    name: str
    list_command: str
    field_order: tuple[str, ...]
    set_fields: tuple[str, ...] = ()
    registry_order: bool = False
    depends_on: tuple[str, ...] = ()
    refs: tuple[tuple[str, str], ...] = ()
    live_key_field: str | None = None
    update_key_field: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.name}.yaml"

    @property
    def key(self) -> str:
        return self.field_order[0]

    @property
    def live_key(self) -> str:
        return self.live_key_field or self.key

    @property
    def update_key(self) -> str:
        return self.update_key_field or self.key

    @property
    def plan_fields(self) -> tuple[str, ...]:
        """The fields plan lines show, in order, as the plan holds them."""
        return self.field_order

    def key_error(self, item: Row) -> str | None:
        """Why *item* has no usable key, or None."""
        if not isinstance(item.get(self.key), str):
            return f"{self.key} is missing or not a string"
        return None

    def item_key(self, item: Row) -> str:
        """The key that identifies *item* within its file."""
        return str(item[self.key])

    def group(self, key: str) -> str:
        """What unmatched items are counted by in plan lines."""
        return self.kind

    @property
    def kind(self) -> str:
        """One item's name in plan lines: ``floor`` for ``floors``."""
        return self.name.removesuffix("s")

    def command(self, op: str) -> str:
        """The WebSocket command for *op* (``create``, ``update``) on this registry."""
        return f"{self.list_command.rsplit('/', 1)[0]}/{op}"

    async def fetch(self, client: Client) -> list[Row]:
        rows: list[Row] = await client.send_command(self.list_command)
        return rows

    def export(self, rows: list[Row], ctx: ExportContext) -> list[Row]:
        items = [self._export_row(row) for row in rows]
        if not self.registry_order:
            items.sort(key=lambda item: str(item[self.key]))
        return items

    def _export_row(self, row: Row) -> Row:
        item: Row = {}
        for name in self.field_order:
            value: Any = row.get(name)
            if has_value(value):
                item[name] = sorted(value) if name in self.set_fields else value
        return item
