"""Data models for HA registry objects and SPAN device tree."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HAEntity:
    """A Home Assistant entity from the entity registry."""

    entity_id: str
    unique_id: str
    platform: str
    device_id: str | None
    device_class: str | None = None
    state_class: str | None = None
    unit_of_measurement: str | None = None
    name: str | None = None  # user override (null = use original_name)
    original_name: str | None = None
    disabled_by: str | None = None
    entity_category: str | None = None
    has_entity_name: bool = False


@dataclass
class HADevice:
    """A Home Assistant device from the device registry."""

    id: str
    name: str | None
    name_by_user: str | None = None
    model: str | None = None
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    via_device_id: str | None = None
    area_id: str | None = None
    entities: list[HAEntity] = field(default_factory=list)
    children: list[HADevice] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name_by_user or self.name or self.id


@dataclass
class HAArea:
    """A Home Assistant area."""

    area_id: str
    name: str


@dataclass
class SpanDeviceTree:
    """A SPAN panel and its child devices."""

    panel: HADevice
    circuits: list[HADevice] = field(default_factory=list)
    battery: HADevice | None = None
    solar: HADevice | None = None
    ev_charger: HADevice | None = None
    site_metering: HADevice | None = None

    @property
    def serial(self) -> str | None:
        """Extract serial number from panel identifiers."""
        for domain, serial in self.panel.identifiers:
            if domain == "span_ebus":
                return serial
        return None

    @property
    def all_child_devices(self) -> list[HADevice]:
        """All non-circuit sub-devices."""
        return [d for d in [self.battery, self.solar, self.ev_charger, self.site_metering] if d]
