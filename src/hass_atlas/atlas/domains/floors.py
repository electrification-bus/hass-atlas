"""Floor registry."""

from __future__ import annotations

from hass_atlas.atlas.domains.base import RegistryDomain

FLOORS = RegistryDomain(
    name="floors",
    list_command="config/floor_registry/list",
    field_order=("floor_id", "name", "level", "icon", "aliases"),
    set_fields=("aliases",),
    registry_order=True,
)
