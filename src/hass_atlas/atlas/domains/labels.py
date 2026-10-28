"""Label registry."""

from __future__ import annotations

from hass_atlas.atlas.domains.base import RegistryDomain

LABELS = RegistryDomain(
    name="labels",
    list_command="config/label_registry/list",
    field_order=("label_id", "name", "color", "icon", "description"),
)
