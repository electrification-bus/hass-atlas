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


class DocError(ValueError):
    """A file's content is malformed; *index* is the list item at fault, if one is."""

    def __init__(self, message: str, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


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

    def to_doc(self, items: list[Row]) -> Any:
        """*items* as the domain's file holds them, for YAML output."""
        ...

    def from_doc(self, doc: Any) -> list[Row]:
        """The items of a loaded file; raises DocError when it is malformed."""
        ...

    def count(self, items: list[Row]) -> int:
        """How many entries *items* hold, as export reports them."""
        ...


class FlowList(list[Any]):
    """A list that YAML output writes on one line, such as an identifier pair."""


class FlowDict(dict[str, Any]):
    """A mapping that YAML output writes on one line, such as an entity's match block."""


def in_field_order(items: list[Row], field_order: tuple[str, ...]) -> list[Row]:
    """*items* with each item's fields in *field_order*."""
    ordered = []
    for item in items:
        unknown = set(item) - set(field_order)
        if unknown:
            raise ValueError(f"fields not in field order: {sorted(unknown)}")
        ordered.append({name: item[name] for name in field_order if name in item})
    return ordered


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
    maps a field to the domain whose ids it holds, or to ``ENTITY_IDS`` for
    an entity_id. ``live_key`` names the id field of live rows and
    ``update_key`` the id argument of the update command, when they differ
    from the file's key.
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

    def comparable(self, name: str, value: Any) -> Any:
        """*value* as HA stores it: set fields sorted, aliases stripped, null lists empty."""
        if name in self.set_fields:
            items = value if isinstance(value, list) else []
            if name == "aliases":
                return sorted({v.strip() for v in items if isinstance(v, str) and v.strip()})
            return sorted(set(items))
        return value

    def live_value(self, row: Row, name: str, wanted: Any) -> Any:
        """The part of *row*'s *name* that a file value *wanted* is compared with."""
        return row.get(name)

    def stored(self, row: Row, name: str, value: Any) -> Any:
        """What *row*'s *name* holds once *value* is written."""
        return [] if value is None and name in self.set_fields else value

    def update_payloads(self, live_id: str, row: Row | None, values: Row) -> list[Row]:
        """The update commands that write *values* to item *live_id*, whose live row is *row*.

        A null list clears it.
        """
        fields = {k: ([] if v is None and k in self.set_fields else v) for k, v in values.items()}
        return [{self.update_key: live_id, **fields}]

    def shown(self, values: Row) -> list[tuple[str, Any]]:
        """*values* as plan lines list them: in ``plan_fields`` order."""
        return [(name, values[name]) for name in self.plan_fields if name in values]

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

    def to_doc(self, items: list[Row]) -> list[Row]:
        return in_field_order(items, self.field_order)

    def from_doc(self, doc: Any) -> list[Row]:
        if not isinstance(doc, list):
            raise DocError("expected a list")
        seen: set[str] = set()
        for index, item in enumerate(doc):
            if not isinstance(item, dict):
                raise DocError("expected a mapping", index)
            if error := self.key_error(item):
                raise DocError(error, index)
            ident = self.item_key(item)
            if ident in seen:
                raise DocError(f"duplicate {self.key} {item[self.key]!r}", index)
            seen.add(ident)
            unknown_fields = sorted(set(item) - set(self.field_order))
            if unknown_fields:
                raise DocError(f"unknown fields {unknown_fields}", index)
        return doc

    def count(self, items: list[Row]) -> int:
        return len(items)

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
