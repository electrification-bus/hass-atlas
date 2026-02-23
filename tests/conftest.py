"""Shared test fixtures with mock HA registry data."""

from __future__ import annotations

from typing import Any

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
            # Topology property entities
            make_entity(
                "sensor.span_pv_relative_position",
                f"{SERIAL}_pv_relative-position",
                SOLAR_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_pv_vendor_name",
                f"{SERIAL}_pv_vendor-name",
                SOLAR_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_pv_product_name",
                f"{SERIAL}_pv_product-name",
                SOLAR_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_pv_feed",
                f"{SERIAL}_pv_feed",
                SOLAR_DEVICE_ID,
                entity_category="diagnostic",
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
            # Topology property entities
            make_entity(
                "sensor.span_bess_relative_position",
                f"{SERIAL}_bess_relative-position",
                BATTERY_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_bess_vendor_name",
                f"{SERIAL}_bess_vendor-name",
                BATTERY_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_bess_model",
                f"{SERIAL}_bess_model",
                BATTERY_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_bess_serial_number",
                f"{SERIAL}_bess_serial-number",
                BATTERY_DEVICE_ID,
                entity_category="diagnostic",
            ),
            make_entity(
                "sensor.span_bess_feed",
                f"{SERIAL}_bess_feed",
                BATTERY_DEVICE_ID,
                entity_category="diagnostic",
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
                    "sensor.span_kitchen_energy",
                    f"{SERIAL}_{CIRCUIT_1_NODE_ID}_exported-energy",
                    CIRCUIT_1_DEVICE_ID,
                    device_class="energy",
                    state_class="total_increasing",
                ),
                make_entity(
                    "sensor.span_kitchen_energy_returned",
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
                    "sensor.span_garage_energy",
                    f"{SERIAL}_{CIRCUIT_2_NODE_ID}_exported-energy",
                    CIRCUIT_2_DEVICE_ID,
                    device_class="energy",
                    state_class="total_increasing",
                ),
                make_entity(
                    "sensor.span_garage_energy_returned",
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
            "entity_id": "sensor.span_kitchen_energy",
            "unique_id": f"{SERIAL}_{CIRCUIT_1_NODE_ID}_exported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_1_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_kitchen_energy_returned",
            "unique_id": f"{SERIAL}_{CIRCUIT_1_NODE_ID}_imported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_1_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Energy Returned",
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
            "entity_id": "sensor.span_garage_energy",
            "unique_id": f"{SERIAL}_{CIRCUIT_2_NODE_ID}_exported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_2_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Energy",
            "disabled_by": None,
            "entity_category": None,
        },
        {
            "entity_id": "sensor.span_garage_energy_returned",
            "unique_id": f"{SERIAL}_{CIRCUIT_2_NODE_ID}_imported-energy",
            "platform": "span_ebus",
            "device_id": CIRCUIT_2_DEVICE_ID,
            "original_device_class": "energy",
            "original_state_class": "total_increasing",
            "original_unit_of_measurement": "kWh",
            "original_name": "Energy Returned",
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


# ---------------------------------------------------------------------------
# Topology test fixtures — Powerwall, Enphase, PV feed circuit, topology states
# ---------------------------------------------------------------------------

PV_FEED_CIRCUIT_DEVICE_ID = "dev-circuit-pv-feed"
PV_FEED_CIRCUIT_NODE_ID = "pv-feed-node"
BESS_FEED_CIRCUIT_DEVICE_ID = "dev-circuit-bess-feed"
BESS_FEED_CIRCUIT_NODE_ID = "bess-feed-node"
PW_DEVICE_ID = "dev-powerwall-001"
ENPHASE_DEVICE_ID = "dev-enphase-001"


def make_platform_entity(
    entity_id: str,
    unique_id: str,
    platform: str,
    device_id: str,
    device_class: str | None = None,
    state_class: str | None = None,
) -> HAEntity:
    """Create a test entity for a non-SPAN platform."""
    return HAEntity(
        entity_id=entity_id,
        unique_id=unique_id,
        platform=platform,
        device_id=device_id,
        device_class=device_class,
        state_class=state_class,
        unit_of_measurement="kWh" if device_class == "energy" else None,
        original_name=entity_id.split(".")[-1],
    )


@pytest.fixture
def powerwall_device() -> HADevice:
    return HADevice(
        id=PW_DEVICE_ID,
        name="Powerwall",
        model="Gateway",
        identifiers=[("powerwall", "pw-gateway-001")],
    )


@pytest.fixture
def powerwall_entities() -> list[HAEntity]:
    return [
        make_platform_entity(
            "sensor.powerwall_site_import",
            "pw_site_import",
            "powerwall",
            PW_DEVICE_ID,
            device_class="energy",
            state_class="total_increasing",
        ),
        make_platform_entity(
            "sensor.powerwall_site_export",
            "pw_site_export",
            "powerwall",
            PW_DEVICE_ID,
            device_class="energy",
            state_class="total_increasing",
        ),
        make_platform_entity(
            "sensor.powerwall_battery_import",
            "pw_battery_import",
            "powerwall",
            PW_DEVICE_ID,
            device_class="energy",
            state_class="total_increasing",
        ),
        make_platform_entity(
            "sensor.powerwall_battery_export",
            "pw_battery_export",
            "powerwall",
            PW_DEVICE_ID,
            device_class="energy",
            state_class="total_increasing",
        ),
    ]


@pytest.fixture
def enphase_device() -> HADevice:
    return HADevice(
        id=ENPHASE_DEVICE_ID,
        name="Enphase Envoy",
        model="Envoy-S",
        identifiers=[("enphase_envoy", "envoy-001")],
    )


@pytest.fixture
def enphase_entities() -> list[HAEntity]:
    return [
        make_platform_entity(
            "sensor.envoy_lifetime_energy_production",
            "envoy_lifetime_production",
            "enphase_envoy",
            ENPHASE_DEVICE_ID,
            device_class="energy",
            state_class="total_increasing",
        ),
    ]


@pytest.fixture
def pv_feed_circuit() -> HADevice:
    """A circuit that feeds PV (solar inverter connected here)."""
    return HADevice(
        id=PV_FEED_CIRCUIT_DEVICE_ID,
        name="Commissioned PV System",
        model="Circuit",
        identifiers=[("span_ebus", f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}")],
        via_device_id=PANEL_DEVICE_ID,
        entities=[
            make_entity(
                "sensor.span_pv_system_energy",
                f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}_exported-energy",
                PV_FEED_CIRCUIT_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
            make_entity(
                "sensor.span_pv_system_energy_returned",
                f"{SERIAL}_{PV_FEED_CIRCUIT_NODE_ID}_imported-energy",
                PV_FEED_CIRCUIT_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
        ],
    )


@pytest.fixture
def bess_feed_circuit() -> HADevice:
    """A circuit that feeds a battery (BESS connected IN_PANEL)."""
    return HADevice(
        id=BESS_FEED_CIRCUIT_DEVICE_ID,
        name="Battery Circuit",
        model="Circuit",
        identifiers=[("span_ebus", f"{SERIAL}_{BESS_FEED_CIRCUIT_NODE_ID}")],
        via_device_id=PANEL_DEVICE_ID,
        entities=[
            make_entity(
                "sensor.span_battery_circuit_energy",
                f"{SERIAL}_{BESS_FEED_CIRCUIT_NODE_ID}_exported-energy",
                BESS_FEED_CIRCUIT_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
            make_entity(
                "sensor.span_battery_circuit_energy_returned",
                f"{SERIAL}_{BESS_FEED_CIRCUIT_NODE_ID}_imported-energy",
                BESS_FEED_CIRCUIT_DEVICE_ID,
                device_class="energy",
                state_class="total_increasing",
            ),
        ],
    )


def make_topology_states(
    *,
    battery: HADevice | None = None,
    solar: HADevice | None = None,
    bess_position: str = "UPSTREAM",
    bess_vendor: str = "Tesla",
    bess_model: str = "Powerwall 2 AC",
    bess_serial: str = "TG121153003K7G",
    bess_feed_name: str | None = None,
    bess_feed_circuit_id: str | None = None,
    pv_position: str = "IN_PANEL",
    pv_vendor: str = "Enphase",
    pv_product: str = "IQ7PLUS-72-x-US-&",
    pv_feed_name: str = "Commissioned PV System",
    pv_feed_circuit_id: str = "pv-feed-node",
) -> dict[str, dict[str, Any]]:
    """Build mock entity states for SPAN topology properties.

    Uses entity_ids from the sub-device entities to match how the production
    code looks up states (by entity_id found on the sub-device, not constructed
    from the serial).
    """
    states: dict[str, dict[str, Any]] = {}

    def _find_eid(device: HADevice | None, suffix: str) -> str | None:
        if device is None:
            return None
        for entity in device.entities:
            if entity.unique_id.endswith(suffix):
                return entity.entity_id
        return None

    def _add(device: HADevice | None, suffix: str, value: str | None, attrs: dict | None = None) -> None:
        eid = _find_eid(device, suffix)
        if eid is None:
            return
        states[eid] = {
            "state": value if value else "unknown",
            "attributes": attrs or {},
        }

    _add(battery, "_relative-position", bess_position)
    _add(battery, "_vendor-name", bess_vendor)
    _add(battery, "_model", bess_model)
    _add(battery, "_serial-number", bess_serial)
    _add(battery, "_feed", bess_feed_name,
         {"circuit_id": bess_feed_circuit_id} if bess_feed_circuit_id else {})

    _add(solar, "_relative-position", pv_position)
    _add(solar, "_vendor-name", pv_vendor)
    _add(solar, "_product-name", pv_product)
    _add(solar, "_feed", pv_feed_name,
         {"circuit_id": pv_feed_circuit_id} if pv_feed_circuit_id else {})

    return states
