"""Area registry.

``picture`` is not exported: it is an ``/api/image/serve/<random id>/...``
path that does not survive a rebuild.
"""

from __future__ import annotations

from hass_atlas.atlas.domains.base import RegistryDomain

AREAS = RegistryDomain(
    name="areas",
    list_command="config/area_registry/list",
    field_order=(
        "area_id",
        "name",
        "floor_id",
        "icon",
        "labels",
        "aliases",
        "temperature_entity_id",
        "humidity_entity_id",
    ),
    set_fields=("labels", "aliases"),
    registry_order=True,
    depends_on=("floors", "labels"),
    refs=(("floor_id", "floors"), ("labels", "labels")),
)
