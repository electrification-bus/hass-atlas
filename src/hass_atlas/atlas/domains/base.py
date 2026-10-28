"""The Domain protocol and the registry-backed domains built on it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Row = dict[str, Any]


class Client(Protocol):
    async def send_command(self, msg_type: str, **kwargs: Any) -> Any: ...


@dataclass
class ExportContext:
    """What an export can see beyond its own rows."""

    ha_version: str | None = None
    rows: dict[str, list[Row]] = field(default_factory=dict)


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


def has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


@dataclass
class RegistryDomain:
    """A registry whose items are keyed by ``field_order[0]``.

    Export keeps only the fields in ``field_order`` that hold a value, so
    timestamps, pictures and fields a newer HA adds are never written.
    ``set_fields`` are stored as sets in HA, so their order is arbitrary and
    they are sorted. With ``registry_order`` the items keep the order HA lists
    them in (the user's order); otherwise they are sorted by key.
    """

    name: str
    list_command: str
    field_order: tuple[str, ...]
    set_fields: tuple[str, ...] = ()
    registry_order: bool = False
    depends_on: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return f"{self.name}.yaml"

    @property
    def key(self) -> str:
        return self.field_order[0]

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
