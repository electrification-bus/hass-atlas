"""Export Home Assistant configuration to a directory of YAML files."""

from __future__ import annotations

from hass_atlas.atlas.domains.areas import AREAS
from hass_atlas.atlas.domains.base import Domain
from hass_atlas.atlas.domains.devices import DEVICES
from hass_atlas.atlas.domains.energy import ENERGY
from hass_atlas.atlas.domains.entities import ENTITIES
from hass_atlas.atlas.domains.floors import FLOORS
from hass_atlas.atlas.domains.labels import LABELS

FORMAT = 1
MANIFEST = "atlas.yaml"

DOMAINS: tuple[Domain, ...] = (FLOORS, LABELS, AREAS, DEVICES, ENTITIES, ENERGY)


def domain_named(name: str) -> Domain:
    for domain in DOMAINS:
        if domain.name == name:
            return domain
    raise KeyError(name)
