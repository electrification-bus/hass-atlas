"""The fake HA's registry writes behave as HA 2026.10.4's do."""

from __future__ import annotations

from typing import Any

import pytest

from hass_atlas.ha_client import HAClientError
from tests.atlas.fake_ha import FakeHA, empty_fixture


async def send(fake: FakeHA, cmd: str, **kwargs: Any) -> Any:
    return await fake.send_command(cmd, **kwargs)


async def error(fake: FakeHA, cmd: str, **kwargs: Any) -> HAClientError:
    with pytest.raises(HAClientError) as info:
        await fake.send_command(cmd, **kwargs)
    return info.value


async def test_ids_from_names_with_suffixes() -> None:
    fake = FakeHA(empty_fixture())
    first = await send(fake, "config/area_registry/create", name="Den")
    await send(fake, "config/area_registry/update", area_id="den", name="Library")
    second = await send(fake, "config/area_registry/create", name="Den")
    assert (first["area_id"], second["area_id"]) == ("den", "den_2")
    assert sorted(first) == sorted(
        [
            "aliases",
            "area_id",
            "floor_id",
            "humidity_entity_id",
            "icon",
            "labels",
            "name",
            "picture",
            "temperature_entity_id",
            "created_at",
            "modified_at",
        ]
    )


async def test_names_unique_after_normalizing() -> None:
    fake = FakeHA(empty_fixture())
    await send(fake, "config/floor_registry/create", name="Ground Floor")
    exc = await error(fake, "config/floor_registry/create", name="groundfloor")
    assert exc.code == "invalid_info"
    assert "The name groundfloor (groundfloor) is already in use" in str(exc)
    await send(fake, "config/floor_registry/create", name="Upper")
    exc = await error(fake, "config/floor_registry/update", floor_id="upper", name="GROUND floor")
    assert exc.code == "invalid_info"
    renamed = await send(
        fake, "config/floor_registry/update", floor_id="ground_floor", name="ground floor"
    )
    assert renamed["name"] == "ground floor"


async def test_schema_errors() -> None:
    fake = FakeHA(empty_fixture())
    for kwargs in (
        {"name": "A", "floor_id": None},
        {"name": "A", "icon": None},
        {"name": "A", "x": 1},
    ):
        exc = await error(fake, "config/area_registry/create", **kwargs)
        assert exc.code == "invalid_format"
    assert fake.registries["areas"] == []


async def test_unknown_id_on_update() -> None:
    exc = await error(
        FakeHA(empty_fixture()), "config/label_registry/update", label_id="x", name="X"
    )
    assert exc.code == "unknown_error"


async def test_area_references() -> None:
    fake = FakeHA(empty_fixture())
    await send(fake, "config/label_registry/create", name="Solar")
    row = await send(
        fake,
        "config/area_registry/create",
        name="Shed",
        floor_id="nowhere",
        labels=["solar", "gone"],
    )
    assert row["floor_id"] == "nowhere"
    assert row["labels"] == ["solar"]
    row = await send(
        fake, "config/area_registry/create", name="Hall", aliases=[" hall ", " ", "hallway"]
    )
    assert row["aliases"] == ["hall", "hallway"]


async def test_area_sensors_checked_when_changed() -> None:
    fake = FakeHA(empty_fixture())
    exc = await error(
        fake, "config/area_registry/create", name="Shed", temperature_entity_id="sensor.nope"
    )
    assert "Entity sensor.nope does not exist" in str(exc)
    exc = await error(
        fake,
        "config/area_registry/create",
        name="Shed",
        humidity_entity_id="sensor.workshop_temperature",
    )
    assert "is not a humidity sensor" in str(exc)
    await send(
        fake,
        "config/area_registry/create",
        name="Shed",
        temperature_entity_id="sensor.workshop_temperature",
    )
    fake.states.clear()
    row = await send(
        fake,
        "config/area_registry/update",
        area_id="shed",
        temperature_entity_id="sensor.workshop_temperature",
        name="Barn",
    )
    assert row["name"] == "Barn"


async def test_unknown_command_fails() -> None:
    with pytest.raises(AssertionError):
        await send(FakeHA(), "config/area_registry/delete", area_id="den")


@pytest.mark.parametrize(
    ("name", "area_id"),
    [("Don's room", "don_s_room"), ("1,000", "1000"), ("Ærø", "aero"), ("Straße", "strasse")],
)
async def test_ids_follow_python_slugify(name: str, area_id: str) -> None:
    fake = FakeHA(empty_fixture())
    assert (await send(fake, "config/area_registry/create", name=name))["area_id"] == area_id


async def test_label_icon_and_color_format() -> None:
    fake = FakeHA(empty_fixture())
    for fields in ({"icon": "flash"}, {"color": "Red"}, {"color": "#ff00"}):
        err = await error(fake, "config/label_registry/create", name="Solar", **fields)
        assert err.code == "invalid_format"
    await send(fake, "config/label_registry/create", name="Solar", icon="mdi:sun", color="#FFaa00")
    await send(fake, "config/label_registry/update", label_id="solar", color="light-blue")
    err = await error(fake, "config/label_registry/update", label_id="solar", icon="sun")
    assert err.code == "invalid_format"


def _device(fake: FakeHA, identifier: str | None) -> dict[str, Any]:
    """The device with identifier ``[acme_*, identifier]``, or with none."""
    return next(
        r
        for r in fake.registries["devices"]
        if [p[1] for p in r["identifiers"]] == ([identifier] if identifier else [])
    )


async def test_device_update_returns_row_and_checks_labels_only() -> None:
    fake = FakeHA()
    plug = _device(fake, None)
    row = await send(
        fake,
        "config/device_registry/update",
        device_id=plug["id"],
        area_id="nowhere",
        labels=["solar", "gone", "solar"],
        name_by_user="Lamp",
    )
    assert (row["area_id"], row["labels"], row["name_by_user"]) == ("nowhere", ["solar"], "Lamp")
    assert set(row) == set(plug)
    row = await send(
        fake, "config/device_registry/update", device_id=plug["id"], name_by_user=None
    )
    assert row["name_by_user"] is None


async def test_device_update_errors() -> None:
    fake = FakeHA()
    plug = _device(fake, None)["id"]
    exc = await error(fake, "config/device_registry/update", device_id="x", area_id="den")
    assert (exc.code, str(exc)) == (
        "not_found",
        "config/device_registry/update failed: Device not found",
    )
    for kwargs in (
        {"device_id": plug, "labels": None},
        {"device_id": plug, "disabled_by": "integration"},
        {"device_id": plug, "area_id": 3},
        {"device_id": plug, "name": "x"},
        {"area_id": "den"},
    ):
        exc = await error(fake, "config/device_registry/update", **kwargs)
        assert exc.code == "invalid_format", kwargs


async def test_device_equal_update_changes_nothing() -> None:
    fake = FakeHA()
    relay = next(r for r in fake.registries["devices"] if r.get("model") == "Relay R2")
    stamp = relay["modified_at"]
    row = await send(
        fake,
        "config/device_registry/update",
        device_id=relay["id"],
        labels=["critical_load", "solar"],
        name_by_user="Pump relay",
    )
    assert row["modified_at"] == stamp
    assert row["labels"] == ["solar", "critical_load"]


async def test_device_disable_cascades_to_children() -> None:
    fake = FakeHA()
    door = _device(fake, "sensor-0042")
    battery = _device(fake, "sensor-0042-battery")
    row = await send(
        fake, "config/device_registry/update", device_id=battery["id"], disabled_by=None
    )
    assert row["disabled_by"] == "device"  # its parent is disabled
    await send(fake, "config/device_registry/update", device_id=door["id"], disabled_by=None)
    assert (door["disabled_by"], battery["disabled_by"]) == (None, None)
    await send(fake, "config/device_registry/update", device_id=door["id"], disabled_by="user")
    assert (door["disabled_by"], battery["disabled_by"]) == ("user", "device")


async def test_device_enable_ignored_while_config_entry_disabled() -> None:
    fake = FakeHA()
    cloud = _device(fake, "acct-91")
    row = await send(
        fake, "config/device_registry/update", device_id=cloud["id"], disabled_by=None
    )
    assert row["disabled_by"] == "config_entry"
    row = await send(
        fake, "config/device_registry/update", device_id=cloud["id"], disabled_by="user"
    )
    assert row["disabled_by"] == "user"


def _entity(fake: FakeHA, entity_id: str) -> dict[str, Any]:
    row = fake.row("entities", entity_id)
    assert row is not None, entity_id
    return row


async def test_entity_list_is_partial_and_get_entries_extended() -> None:
    fake = FakeHA()
    rows = await send(fake, "config/entity_registry/list")
    assert len(rows) == 15
    assert not {"aliases", "device_class", "capabilities"} & set(rows[0])
    entries = await send(
        fake,
        "config/entity_registry/get_entries",
        entity_ids=["sensor.workshop_humidity", "sensor.nope"],
    )
    assert entries["sensor.nope"] is None
    assert entries["sensor.workshop_humidity"]["aliases"] == [None, "shop humidity"]
    assert set(entries["sensor.workshop_humidity"]) == set(rows[0]) | {
        "aliases",
        "capabilities",
        "device_class",
        "original_device_class",
        "original_icon",
    }


async def test_entity_update_values() -> None:
    fake = FakeHA()
    reply = await send(
        fake,
        "config/entity_registry/update",
        entity_id="switch.pump_relay",
        aliases=[None, " pump ", " ", "pump"],
        labels=["solar", "gone"],
        area_id="nowhere",
        categories={"helpers": "c1", "automation": None},
    )
    assert set(reply) == {"entity_entry"}
    row = reply["entity_entry"]
    assert row["aliases"] == [None, "pump", "pump"]
    assert (row["labels"], row["area_id"]) == (["solar"], "nowhere")
    assert row["categories"] == {"helpers": "c1"}
    row = (
        await send(
            fake,
            "config/entity_registry/update",
            entity_id="switch.pump_relay",
            categories={"helpers": None, "script": "c2"},
        )
    )["entity_entry"]
    assert row["categories"] == {"script": "c2"}


async def test_entity_options_domain_replaced_whole() -> None:
    fake = FakeHA()
    await send(
        fake,
        "config/entity_registry/update",
        entity_id="sensor.pump_power",
        options_domain="sensor",
        options={"display_precision": 3},
    )
    options = _entity(fake, "sensor.pump_power")["options"]
    assert options["sensor"] == {"display_precision": 3}
    assert options["sensor.private"] == {"suggested_unit_of_measurement": "W"}
    await send(
        fake,
        "config/entity_registry/update",
        entity_id="sensor.pump_power",
        options_domain="sensor.private",
        options=None,
    )
    assert "sensor.private" not in _entity(fake, "sensor.pump_power")["options"]


async def test_entity_update_schema_errors() -> None:
    fake = FakeHA()
    for kwargs in (
        {"entity_id": "sensor.pump_power", "labels": None},
        {"entity_id": "sensor.pump_power", "hidden_by": "integration"},
        {"entity_id": "sensor.pump_power", "disabled_by": "device"},
        {"entity_id": "sensor.pump_power", "options": {}},
        {"entity_id": "sensor.pump_power", "aliases": [3]},
        {"entity_id": "sensor.pump_power", "categories": {"Not Slug": "x"}},
        {"entity_id": "sensor.pump_power", "original_name": "x"},
        {"entity_id": "not an id"},
    ):
        exc = await error(fake, "config/entity_registry/update", **kwargs)
        assert exc.code == "invalid_format", kwargs
    exc = await error(fake, "config/entity_registry/update", entity_id="sensor.nope", name="x")
    assert (exc.code, str(exc)) == (
        "not_found",
        "config/entity_registry/update failed: Entity not found",
    )


@pytest.mark.parametrize(
    ("new", "message"),
    [
        ("sensor.pump_power_2", "Entity with this ID is already registered"),
        ("sensor.workshop_temperature", "Entity with this ID is already registered"),
        ("sensor.Bad", "Invalid entity ID"),
        ("switch.pump_power", "New entity ID should be same domain"),
    ],
)
async def test_entity_rename_errors(new: str, message: str) -> None:
    fake = FakeHA()
    fake.states.append({"entity_id": "sensor.state_only", "state": "1", "attributes": {}})
    exc = await error(
        fake, "config/entity_registry/update", entity_id="sensor.pump_power", new_entity_id=new
    )
    assert (exc.code, str(exc)) == (
        "invalid_info",
        f"config/entity_registry/update failed: {message}",
    )
    exc = await error(
        fake,
        "config/entity_registry/update",
        entity_id="sensor.pump_power",
        new_entity_id="sensor.state_only",
    )
    assert "already registered" in str(exc)


async def test_entity_rename_moves_state() -> None:
    fake = FakeHA()
    row = (
        await send(
            fake,
            "config/entity_registry/update",
            entity_id="sensor.workshop_temperature",
            new_entity_id="sensor.shop_temperature",
        )
    )["entity_entry"]
    assert row["entity_id"] == "sensor.shop_temperature"
    assert fake.row("entities", "sensor.workshop_temperature") is None
    ids = {s["entity_id"] for s in fake.states}
    assert "sensor.shop_temperature" in ids and "sensor.workshop_temperature" not in ids


async def test_entity_enable_under_disabled_device_and_reply() -> None:
    fake = FakeHA()
    exc = await error(
        fake, "config/entity_registry/update", entity_id="binary_sensor.door_contact",
        disabled_by=None,
    )  # fmt: skip
    assert (exc.code, str(exc)) == (
        "invalid_info",
        "config/entity_registry/update failed: Device is disabled",
    )
    reply = await send(
        fake, "config/entity_registry/update", entity_id="sensor.hub_signal", disabled_by=None
    )
    assert reply["reload_delay"] == 30
    # HA answers the same for an entity that was already enabled.
    _entity(fake, "sensor.kitchen_power")["config_entry_id"] = None
    reply = await send(
        fake, "config/entity_registry/update", entity_id="sensor.kitchen_power", disabled_by=None
    )
    assert reply["require_restart"] is True and "reload_delay" not in reply
    reply = await send(
        fake, "config/entity_registry/update", entity_id="sensor.kitchen_power", name="x"
    )
    assert set(reply) == {"entity_entry"}


async def test_entity_own_area_needs_own_name() -> None:
    fake = FakeHA()
    exc = await error(
        fake, "config/entity_registry/update", entity_id="binary_sensor.door_contact",
        area_id="den",
    )  # fmt: skip
    assert exc.code == "invalid_info"
    assert "An entity without a name of its own cannot have an area of its own" in str(exc)
    row = (
        await send(
            fake,
            "config/entity_registry/update",
            entity_id="binary_sensor.door_contact",
            area_id="den",
            name="Back door",
        )
    )["entity_entry"]
    assert row["area_id"] == "den"
    # An area held by an entity with a name of its own from the integration is fine.
    await send(fake, "config/entity_registry/update", entity_id="sensor.pump_power", area_id="den")


async def test_device_disable_cascades_to_entities() -> None:
    fake = FakeHA()
    door = _device(fake, "sensor-0042")
    await send(fake, "config/device_registry/update", device_id=door["id"], disabled_by=None)
    assert _entity(fake, "binary_sensor.door_contact")["disabled_by"] is None
    assert _entity(fake, "sensor.door_battery")["disabled_by"] is None  # through the child device
    hub = _device(fake, "hub-7f3a-controller")
    await send(fake, "config/device_registry/update", device_id=hub["id"], disabled_by="user")
    assert _entity(fake, "switch.hub_led")["disabled_by"] == "device"
    assert _entity(fake, "sensor.hub_signal")["disabled_by"] == "user"  # already disabled
    await send(fake, "config/device_registry/update", device_id=hub["id"], disabled_by=None)
    assert _entity(fake, "switch.hub_led")["disabled_by"] is None
    assert _entity(fake, "sensor.hub_signal")["disabled_by"] == "user"


async def test_category_list() -> None:
    fake = FakeHA()
    rows = await send(fake, "config/category_registry/list", scope="helpers")
    assert [r["category_id"] for r in rows] == ["01KB9C4T3G0000000000000001"]
    assert await send(fake, "config/category_registry/list", scope="automation") == []
    exc = await error(fake, "config/category_registry/list")
    assert exc.code == "invalid_format"
