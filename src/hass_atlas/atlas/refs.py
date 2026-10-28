"""Map ids in an atlas to the ids the live Home Assistant uses for the same items."""

from __future__ import annotations


class IdMap:
    """Exported id to live id, per domain.

    Every file item matched to a live item, or created, is bound here, so a
    reference written later is rewritten through it. Most bindings map an id
    to itself; :meth:`remapped` lists the ones that do not.
    """

    def __init__(self) -> None:
        self._ids: dict[str, dict[str, str]] = {}

    def bind(self, domain: str, exported: str, live: str) -> None:
        self._ids.setdefault(domain, {})[exported] = live

    def get(self, domain: str, exported: str) -> str | None:
        return self._ids.get(domain, {}).get(exported)

    def resolve(self, domain: str, exported: str) -> str:
        """The live id for *exported*, or *exported* itself when it is not bound."""
        return self.get(domain, exported) or exported

    def remapped(self, domain: str) -> dict[str, str]:
        return {k: v for k, v in self._ids.get(domain, {}).items() if k != v}

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {domain: dict(ids) for domain, ids in self._ids.items()}
