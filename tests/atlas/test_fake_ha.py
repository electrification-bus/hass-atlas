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
