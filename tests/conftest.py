"""Shared test fixtures with mock HA registry data."""

from __future__ import annotations

import pytest

from ha_atlas.models import HAArea, HADevice, HAEntity, SpanDeviceTree


SERIAL = "nt-0000-abc12"
PANEL_DEVICE_ID = "dev-panel-001"
SITE_METER_DEVICE_ID = "dev-site-meter-001"
SOLAR_DEVICE_ID = "dev-solar-001"
BATTERY_DEVICE_ID = "dev-battery-001"
CIRCUIT_1_DEVICE_ID = "dev-circuit-001"
CIRCUIT_2_DEVICE_ID = "dev-circuit-002"
CIRCUIT_1_NODE_ID = "c1-node"
CIRCUIT_2_NODE_ID = "c2-node"


def make_entity(
    entity_id: str,
    unique_id: str,
    device_id: str,
    device_class: str | None = None,
    state_class: str | None = None,
    disabled_by: str | None = None,
    entity_category: str | None = None,
) -> HAEntity:
    return HAEntity(
        entity_id=entity_id,
        unique_id=unique_id,
        platform="span_ebus",
        device_id=device_id,
        device_class=device_class,
        state_class=state_class,
        unit_of_measurement="kWh" if device_class == "energy" else None,
        original_name=entity_id.split(".")[-1],
        disabled_by=disabled_by,
        entity_category=entity_category,
    )


@pytest.fixture
def panel_device() -> HADevice:
    return HADevice(
        id=PANEL_DEVICE_ID,
        name="SPAN Panel",
        model="SPAN Panel",
        identifiers=[("span_ebus", SERIAL)],
    )


@pytest.fixture
def site_meter_device() -> HADevice:
    return HADevice(
        id=SITE_METER_DEVICE_ID,
        name="Site Metering",
        model="Site Metering",
        identifiers=[("span_ebus", f"{SERIAL}_site-meter")],
        via_device_id=PANEL_DEVICE_ID,
        entities=[
            make_entity(
                "sensor.span_site_imported_energy",
                f"{SERIAL}_site-meter_imported-energy",
                SITE_METER_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
            make_entity(
                "sensor.span_site_exported_energy",
                f"{SERIAL}_site-meter_exported-energy",
                SITE_METER_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
        ],
    )


@pytest.fixture
def solar_device() -> HADevice:
    return HADevice(
        id=SOLAR_DEVICE_ID,
        name="Solar PV",
        model="Solar PV",
        identifiers=[("span_ebus", f"{SERIAL}_solar")],
        via_device_id=PANEL_DEVICE_ID,
        entities=[
            make_entity(
                "sensor.span_solar_imported_energy",
                f"{SERIAL}_solar_imported-energy",
                SOLAR_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
        ],
    )


@pytest.fixture
def battery_device() -> HADevice:
    return HADevice(
        id=BATTERY_DEVICE_ID,
        name="Battery Storage",
        model="Battery Storage",
        identifiers=[("span_ebus", f"{SERIAL}_battery")],
        via_device_id=PANEL_DEVICE_ID,
        entities=[
            make_entity(
                "sensor.span_battery_imported_energy",
                f"{SERIAL}_battery_imported-energy",
                BATTERY_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
            make_entity(
                "sensor.span_battery_exported_energy",
                f"{SERIAL}_battery_exported-energy",
                BATTERY_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
        ],
    )


@pytest.fixture
def circuit_devices() -> list[HADevice]:
    return [
        HADevice(
            id=CIRCUIT_1_DEVICE_ID,
            name="Kitchen",
            model="Circuit",
            identifiers=[("span_ebus", f"{SERIAL}_{CIRCUIT_1_NODE_ID}")],
            via_device_id=PANEL_DEVICE_ID,
            area_id="area-kitchen",
            entities=[
                make_entity(
                    "sensor.span_kitchen_imported_energy",
                    f"{SERIAL}_{CIRCUIT_1_NODE_ID}_imported-energy",
                    CIRCUIT_1_DEVICE_ID,
                    device_class="energy",
                    state_class="total_increasing",
                ),
                make_entity(
                    "sensor.span_kitchen_active_power",
                    f"{SERIAL}_{CIRCUIT_1_NODE_ID}_active-power",
                    CIRCUIT_1_DEVICE_ID,
                    device_class="power",
                ),
            ],
        ),
        HADevice(
            id=CIRCUIT_2_DEVICE_ID,
            name="Garage",
            model="Circuit",
            identifiers=[("span_ebus", f"{SERIAL}_{CIRCUIT_2_NODE_ID}")],
            via_device_id=PANEL_DEVICE_ID,
            entities=[
                make_entity(
                    "sensor.span_garage_imported_energy",
                    f"{SERIAL}_{CIRCUIT_2_NODE_ID}_imported-energy",
                    CIRCUIT_2_DEVICE_ID,
                    device_class="energy",
                    state_class="total_increasing",
                ),
            ],
        ),
    ]


@pytest.fixture
def span_tree(
    panel_device: HADevice,
    site_meter_device: HADevice,
    solar_device: HADevice,
    battery_device: HADevice,
    circuit_devices: list[HADevice],
) -> SpanDeviceTree:
    panel_device.children = [site_meter_device, solar_device, battery_device] + circuit_devices
    return SpanDeviceTree(
        panel=panel_device,
        circuits=circuit_devices,
        site_metering=site_meter_device,
        solar=solar_device,
        battery=battery_device,
    )


@pytest.fixture
def sample_areas() -> list[HAArea]:
    return [
        HAArea(area_id="area-kitchen", name="Kitchen"),
        HAArea(area_id="area-living", name="Living Room"),
    ]


# --- Raw WS response fixtures for registry parsing ---

@pytest.fixture
def raw_devices() -> list[dict]:
    """Raw device registry responses as HA returns them."""
    return [
        {
            "id": PANEL_DEVICE_ID,
            "name": "SPAN Panel",
            "name_by_user": None,
            "model": "SPAN Panel",
            "identifiers": [["span_ebus", SERIAL]],
            "via_device_id": None,
            "area_id": None,
        },
        {
            "id": SITE_METER_DEVICE_ID,
            "name": "Site Metering",
            "name_by_user": None,
            "model": "Site Metering",
            "identifiers": [["span_ebus", f"{SERIAL}_site-meter"]],
            "via_device_id": PANEL_DEVICE_ID,
            "area_id": None,
        },
        {
            "id": CIRCUIT_1_DEVICE_ID,
            "name": "Kitchen",
            "name_by_user": None,
            "model": "Circuit",
            "identifiers": [["span_ebus", f"{SERIAL}_{CIRCUIT_1_NODE_ID}"]],
            "via_device_id": PANEL_DEVICE_ID,
            "area_id": "area-kitchen",
        },
        {
            "id": CIRCUIT_2_DEVICE_ID,
            "name": "Garage",
            "name_by_user": None,
            "model": "Circuit",
            "identifiers": [["span_ebus", f"{SERIAL}_{CIRCUIT_2_NODE_ID}"]],
            "via_device_id": PANEL_DEVICE_ID,
            "area_id": None,
        },
        # Non-SPAN device to verify filtering
        {
            "id": "dev-other-001",
            "name": "Hue Bridge",
            "name_by_user": None,
            "model": "BSB002",
            "identifiers": [["hue", "001788FFFE123456"]],
            "via_device_id": None,
            "area_id": None,
        },
    ]


@pytest.fixture
def raw_entities() -> list[dict]:
    """Raw entity registry responses."""
    return [
        {
            "entity_id": "sensor.span_site_imported_energy",
            "unique_id": f"{SERIAL}_site-meter_imported-energy",
            "platform": "span_ebus",
            "device_id": SITE_METER_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Imported Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_site_exported_energy",
            "unique_id": f"{SERIAL}_site-meter_exported-energy",
            "platform": "span_ebus",
            "device_id": SITE_METER_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Exported Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_kitchen_imported_energy",
            "unique_id": f"{SERIAL}_{CIRCUIT_1_NODE_ID}_imported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_1_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Imported Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_kitchen_active_power",
            "unique_id": f"{SERIAL}_{CIRCUIT_1_NODE_ID}_active-power",
            "platform": "span_ebus",
            "device_id": CIRCUIT_1_DEVICE_ID,
            "original_device_class": "power",
            "original_state_class": None,
            "original_unit_of_measurement": "W",
            "original_name": "Active Power",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_garage_imported_energy",
            "unique_id": f"{SERIAL}_{CIRCUIT_2_NODE_ID}_imported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_2_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Imported Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        # Non-SPAN entity
        {
            "entity_id": "light.hue_living_room",
            "unique_id": "hue-abc123",
            "platform": "hue",
            "device_id": "dev-other-001",
            "original_device_class": None,
            "original_state_class": None,
            "original_unit_of_measurement": None,
            "original_name": "Living Room",
            "disabled_by": None,
            "entity_category": None,
        },
    ]


@pytest.fixture
def raw_areas() -> list[dict]:
    return [
        {"area_id": "area-kitchen", "name": "Kitchen"},
        {"area_id": "area-living", "name": "Living Room"},
    ]
