"""``export`` and ``apply`` for entities."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from hass_atlas.atlas import apply_cmd, export_cmd
from hass_atlas.atlas.domains import entities as entities_mod
from hass_atlas.atlas.yamlio import AtlasFormatError, load_atlas
from hass_atlas.ha_client import HAClientError
from tests.atlas.fake_ha import (
    ENTITY_GET,
    ENTITY_LIST,
    ENTITY_UPDATE,
    FakeHA,
    empty_fixture,
    load_fixture,
)
from tests.atlas.test_apply import apply, commands, export, flat, invoke, write

WEBHOOK = "c0ffee5a1d2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5"
AREA_UPDATE = "config/area_registry/update"
DEVICE_UPDATE = "config/device_registry/update"
POWER = "hub-7f3a-r2-power"
POWER_AVG = "hub-7f3a-r2-power-avg"
KITCHEN = "hub-7f3a-r4-power"
SIGNAL = "hub-7f3a-controller-rssi"
TEMPERATURE = "4011223344-12-49-0-Air temperature"
SUBENTRY = "01KC2W8N4R0000000000000001"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(apply_cmd, "_stdin_is_tty", lambda: False)


def entity(fake: FakeHA, unique_id: str) -> dict[str, Any]:
    """The one live entity whose unique_id is *unique_id*."""
    rows = [r for r in fake.registries["entities"] if r["unique_id"] == unique_id]
    assert len(rows) == 1, unique_id
    return rows[0]


def by_id(fake: FakeHA, entity_id: str) -> dict[str, Any]:
    row = fake.row("entities", entity_id)
    assert row is not None, entity_id
    return row


def sensor_writes(fake: FakeHA) -> list[dict[str, Any]]:
    """The stage 5 writes: area updates that set a temperature or humidity sensor."""
    return [
        msg
        for cmd, msg in fake.writes
        if cmd == AREA_UPDATE and ("temperature_entity_id" in msg or "humidity_entity_id" in msg)
    ]


def entity_writes(fake: FakeHA) -> list[dict[str, Any]]:
    return [msg for cmd, msg in fake.writes if cmd == ENTITY_UPDATE]


def renames(fake: FakeHA) -> list[tuple[str, str]]:
    return [
        (m["entity_id"], m["new_entity_id"]) for m in entity_writes(fake) if "new_entity_id" in m
    ]


def stable(
    unique_id: str, entity_id: str, platform: str = "acme_hub", **fields: Any
) -> dict[str, Any]:
    return {
        "match": {"platform": platform, "unique_id": unique_id},
        "entity_id": entity_id,
        **fields,
    }


def area_row(fake: FakeHA) -> dict[str, Any]:
    row = fake.row("areas", "garage_2")
    assert row is not None
    return row


def intruder(row: dict[str, Any], entity_id: str) -> dict[str, Any]:
    """Another entity, like *row*, registered at *entity_id*."""
    other = copy.deepcopy(row)
    other.update(id="0" * 32, unique_id="intruder", entity_id=entity_id)
    return other


def on_list_call(fake: FakeHA, number: int, action: Any) -> None:
    """Run *action* on *fake* before its *number*th entity list command."""
    calls: list[int] = []

    def hook(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == number:
            action(fake)

    fake.before[ENTITY_LIST] = [hook]


def conversation_row(data: dict[str, Any], unique_id: str, entity_id: str) -> dict[str, Any]:
    """A conversation agent from a config subentry, which HA gives a random id."""
    row = copy.deepcopy(next(r for r in data["entities"] if r["platform"] == "integration"))
    row.update(
        id="5d1e" * 8,
        platform="acme_assist",
        entity_id=entity_id,
        unique_id=unique_id,
        config_entry_id="01KC2W8N4R0000000000000009",
        config_subentry_id=SUBENTRY,
        name="Kitchen assistant",
        original_name="Acme assist",
        icon=None,
        categories={},
    )
    return row


def rebuilt_data(**renamed: str) -> dict[str, Any]:
    """A rebuilt HA in which the entities named by key have the entity_id given as value."""
    data = load_fixture()
    data["rebuilt_entity_ids"] = {**data["rebuilt_entity_ids"], **renamed}
    return empty_fixture(data)


# --- export ------------------------------------------------------------


def test_export_only_entities(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, fake, "export", "--out", str(out), "--only", "entities")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["atlas.yaml", "entities.yaml"]
    assert [cmd for cmd, _ in fake.sent] == [ENTITY_LIST, ENTITY_GET]
    manifest = load_atlas(out).manifest
    assert (manifest["domains"], manifest["entities"]) == (["entities"], "all")


def test_export_every_entity_and_only_user_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = load_atlas(export(monkeypatch, FakeHA(), tmp_path / "a")).docs["entities"]
    assert len(items) == len(load_fixture()["entities"])
    by_key = {json.dumps(i["match"], sort_keys=True): i for i in items}

    def item(platform: str, **match: str) -> dict[str, Any]:
        return by_key[json.dumps({"platform": platform, **match}, sort_keys=True)]

    assert item("acme_hub", unique_id="hub-7f3a-controller-led") == {
        "match": {"platform": "acme_hub", "unique_id": "hub-7f3a-controller-led"},
        "entity_id": "switch.hub_led",
    }  # hidden by the integration, so no user value
    assert item("acme_hub", unique_id="sensor-0042-contact")["hidden"] is True
    assert "disabled" not in item("acme_hub", unique_id="sensor-0042-contact")
    assert item("acme_hub", unique_id=SIGNAL)["disabled"] is True
    assert "disabled" not in item("acme_cloud", unique_id="acct-91-weather")
    assert item("acme_hub", unique_id=POWER)["options"] == {"sensor": {"display_precision": 1}}
    assert item("acme_hub", unique_id=KITCHEN)["options"] == {
        "sensor": {"unit_of_measurement": "kW"}
    }
    assert "options" not in item("acme_hub", unique_id=POWER_AVG)
    assert "aliases" not in item("zwave_js", unique_id=TEMPERATURE)
    assert item("acme_plug", unique_id="02005e100001-light")["aliases"] == []
    humidity = item("zwave_js", unique_id="4011223344-12-49-0-Humidity")
    assert humidity["aliases"] == [None, "shop humidity"]
    assert item("acme_hub", unique_id="hub-7f3a-r2-switch")["labels"] == ["critical_load", "solar"]


def test_unstable_keys_matched_by_entity_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = load_atlas(export(monkeypatch, FakeHA(), tmp_path / "a")).docs["entities"]
    matches = {i["match"]["platform"]: i for i in items if "entity_id" in i["match"]}
    assert sorted(matches) == ["integration", "mobile_app", "utility_meter"]
    assert matches["mobile_app"] == {
        "match": {"platform": "mobile_app", "entity_id": "sensor.pixel_battery_level"},
        "area_id": "den",
    }
    assert all("unique_id" not in i["match"] for i in matches.values())


def test_export_has_no_secrets_or_registry_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = export(monkeypatch, FakeHA(), tmp_path / "a")
    exported = b"".join(p.read_bytes() for p in sorted(out.iterdir()))
    data = load_fixture()
    secrets = {WEBHOOK}
    for row in data["entities"]:
        for name in ("id", "device_id", "config_entry_id"):
            if row.get(name):
                secrets.add(row[name])
        if row["platform"] in ("mobile_app", "integration", "utility_meter"):
            secrets.add(row["unique_id"])
    for row in data["devices"]:
        secrets.add(row["id"])
        secrets.update(row.get("config_entries", []))
    assert len(secrets) == 34
    for secret in secrets:
        assert secret.encode() not in exported, secret
        assert secret.lower().encode() not in exported, secret


def test_entity_id_built_from_webhook_not_exported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    row = next(r for r in data["entities"] if r["platform"] == "mobile_app")
    row["entity_id"] = f"sensor.mobile_app_{WEBHOOK}_battery_level"
    out = tmp_path / "a"
    result = invoke(monkeypatch, FakeHA(data), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert (
        "a mobile_app entity (Pixel battery level) has an entity_id built from its unique_id "
        "or config entry or subentry id; it is not exported" in flat(result)
    )
    assert WEBHOOK not in result.output
    assert WEBHOOK.encode() not in (out / "entities.yaml").read_bytes()
    assert len(load_atlas(out).docs["entities"]) == len(data["entities"]) - 1


def test_entity_id_built_from_config_entry_not_exported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    row = next(r for r in data["entities"] if r["platform"] == "utility_meter")
    entry = row["config_entry_id"]
    row["entity_id"] = f"sensor.utility_meter_{entry.lower()}"
    out = tmp_path / "a"
    result = invoke(monkeypatch, FakeHA(data), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "a utility_meter entity (Grid import peak) has an entity_id built from" in flat(result)
    exported = (out / "entities.yaml").read_text(encoding="utf-8")
    assert entry not in exported and entry.lower() not in exported


def test_subentry_unique_id_matched_by_entity_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    data["entities"].append(conversation_row(data, SUBENTRY, "conversation.acme_assist"))
    a = export(monkeypatch, FakeHA(data), tmp_path / "a")
    assert SUBENTRY.encode() not in (a / "entities.yaml").read_bytes()
    [item] = [i for i in load_atlas(a).docs["entities"] if i["match"]["platform"] == "acme_assist"]
    assert item == {
        "match": {"platform": "acme_assist", "entity_id": "conversation.acme_assist"},
        "name": "Kitchen assistant",
    }
    b = FakeHA(empty_fixture(data))
    assert by_id(b, "conversation.acme_assist")["unique_id"] != SUBENTRY
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert by_id(b, "conversation.acme_assist")["name"] == "Kitchen assistant"


def test_entity_id_built_from_subentry_not_exported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    entity_id = f"conversation.acme_{SUBENTRY.lower()}"
    data["entities"].append(conversation_row(data, f"{SUBENTRY}-agent", entity_id))
    out = tmp_path / "a"
    result = invoke(monkeypatch, FakeHA(data), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "a acme_assist entity (Kitchen assistant) has an entity_id built from" in flat(result)
    assert SUBENTRY.lower().encode() not in (out / "entities.yaml").read_bytes()


def test_categories_not_exported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    assert b"categories" not in (a / "entities.yaml").read_bytes()
    b = FakeHA(empty_fixture())
    assert by_id(b, "sensor.house_energy_in")["categories"] == {}
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert by_id(b, "sensor.house_energy_in")["icon"] == "mdi:transmission-tower"


def test_next_line_character_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    row = next(r for r in data["entities"] if r["unique_id"] == KITCHEN)
    row.update(unique_id="hub-7f3a-r4\x85power", name="Fridge\x85power")
    fake = FakeHA(data)
    a = export(monkeypatch, fake, tmp_path / "a")
    [item] = [i for i in load_atlas(a).docs["entities"] if i.get("name") == "Fridge\x85power"]
    assert item["match"]["unique_id"] == "hub-7f3a-r4\x85power"
    result = invoke(monkeypatch, fake, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output


def test_export_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = load_fixture()
    data["entities"].reverse()
    for row in data["entities"]:
        row["labels"].reverse()
        row["categories"] = dict(reversed(row["categories"].items()))
        row["modified_at"] += 1000
        row["options"] = {
            k: dict(reversed(v.items())) for k, v in reversed(row["options"].items())
        }
    second = export(monkeypatch, FakeHA(data), tmp_path / "b")
    assert (first / "entities.yaml").read_bytes() == (second / "entities.yaml").read_bytes()


def test_export_fetches_in_batches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(entities_mod, "BATCH", 4)
    fake = FakeHA()
    out = tmp_path / "a"
    assert invoke(monkeypatch, fake, "export", "--out", str(out)).exit_code == 0
    sizes = [len(msg["entity_ids"]) for cmd, msg in fake.sent if cmd == ENTITY_GET]
    assert sizes == [4, 4, 4, 3]
    golden = Path(__file__).parent.parent / "fixtures" / "atlas" / "golden" / "entities.yaml"
    assert (out / "entities.yaml").read_bytes() == golden.read_bytes()


def test_export_skips_invalid_option_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    entity_row = next(r for r in data["entities"] if r["unique_id"] == POWER)
    entity_row["options"]["sensor"]["display_precision"] = "one"
    out = tmp_path / "a"
    result = invoke(monkeypatch, FakeHA(data), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "sensor.pump_power: options sensor display_precision holds 'one'; not exported" in (
        flat(result)
    )
    result = invoke(monkeypatch, FakeHA(data), "apply", str(out), "--check")
    assert result.exit_code == 0, result.output


def test_export_then_check_is_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    a = export(monkeypatch, fake, tmp_path / "a")
    fake.sent.clear()
    result = invoke(monkeypatch, fake, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output
    assert "= entity  15 unchanged" in result.output
    assert "refused" not in result.output and "unmatched" not in result.output
    assert fake.writes == []


# --- loading -------------------------------------------------------------


GOOD_MANIFEST = "format: 1\nhass_atlas: 0.0.0\ndomains: [entities]\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- entity_id: sensor.a\n", "item 0: match is missing or not a mapping"),
        ("- match: {unique_id: a}\n", "match platform is missing or not a string"),
        ("- match: {platform: p}\n", "match needs unique_id or entity_id, not both"),
        (
            "- match: {platform: p, unique_id: a, entity_id: sensor.a}\n",
            "match needs unique_id or entity_id, not both",
        ),
        ("- match: {platform: p, unique_id: 3}\n", "match unique_id must be a string"),
        ("- match: {platform: p, entity_id: Sensor.A}\n", "is not a valid entity id"),
        (
            "- match: {platform: p, entity_id: sensor.a}\n  entity_id: sensor.b\n",
            "an item matched by entity_id cannot set entity_id",
        ),
        ("- match: {platform: p, unique_id: a, serial: x}\n", "unknown match fields ['serial']"),
        (
            "- match: {platform: p, unique_id: a}\n  entity_id: sensor.a\n"
            "- match: {platform: p, unique_id: a}\n  entity_id: sensor.b\n",
            "item 1: duplicate match",
        ),
        ("- match: {platform: p, unique_id: a}\n  hidden_by: user\n", "unknown fields"),
    ],
)
def test_load_errors(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "atlas"
    path.mkdir()
    (path / "atlas.yaml").write_text(GOOD_MANIFEST, encoding="utf-8")
    (path / "entities.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(AtlasFormatError) as info:
        load_atlas(path)
    assert "entities.yaml: " in str(info.value)
    assert message in str(info.value)


def test_same_unique_id_in_two_domains_is_two_items(tmp_path: Path) -> None:
    items = [
        stable("a", "sensor.a", name="A"),
        stable("a", "switch.a", name="A"),
    ]
    atlas = load_atlas(write(tmp_path / "a", {"entities": items}))
    assert len(atlas.docs["entities"]) == 2


# --- round trip and idempotence ----------------------------------------


def test_rebuild_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    assert by_id(b, "sensor.pump_power")["unique_id"] == POWER_AVG
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert entity(b, POWER)["entity_id"] == "sensor.pump_power"
    assert entity(b, POWER_AVG)["entity_id"] == "sensor.pump_power_2"
    assert entity(b, KITCHEN)["entity_id"] == "sensor.kitchen_power"
    assert by_id(b, "binary_sensor.door_contact")["disabled_by"] == "device"  # device disabled
    exported = export(monkeypatch, b, tmp_path / "b")
    assert (exported / "entities.yaml").read_bytes() == (a / "entities.yaml").read_bytes()

    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert b.writes == []
    assert "= entity  15 unchanged" in result.output


def test_rebuild_with_entity_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    weather = next(r for r in data["entities"] if r["platform"] == "acme_cloud")
    data["entities"].remove(weather)
    b = FakeHA(data)
    result = invoke(monkeypatch, b, "apply", str(a), dry_run=True)
    assert "  ? entity  1 unmatched:      acme_cloud 1  (-v lists them)\n" in result.output
    result = invoke(monkeypatch, b, "apply", str(a), "-v", dry_run=True)
    assert "  ? entity  weather.home      unmatched: not in HA\n" in result.output

    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    docs_a = load_atlas(a).docs["entities"]
    docs_b = load_atlas(export(monkeypatch, b, tmp_path / "b")).docs["entities"]
    assert docs_b == [i for i in docs_a if i["match"]["platform"] != "acme_cloud"]

    b.registries["entities"].append(weather)  # its integration is set up again
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert by_id(b, "weather.home")["options"]["weather"]["wind_speed_unit"] == "km/h"


def test_dry_run_and_check_send_no_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    snap = tmp_path / "snap"
    result = invoke(monkeypatch, b, "apply", str(a), "--snapshot-dir", str(snap), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "> entity  sensor.relay_power -> sensor.kitchen_power\n" in result.output
    result = invoke(monkeypatch, b, "apply", str(a), "--check", "--only", "entities")
    assert result.exit_code == 2, result.output
    assert b.writes == []
    assert not snap.exists()
    assert not (tmp_path / "home").exists()


def test_snapshot_before_holds_entities(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    item = stable(POWER_AVG, "sensor.pump_power_2", name="Average")
    snap = tmp_path / "snap"
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), snap)
    assert result.exit_code == 0, result.output
    golden = Path(__file__).parent.parent / "fixtures" / "atlas" / "golden" / "entities.yaml"
    assert (snap / "before" / "entities.yaml").read_bytes() == golden.read_bytes()
    [record] = [json.loads(line) for line in (snap / "journal.jsonl").read_text().splitlines()]
    assert record["payload"] == {"entity_id": "sensor.pump_power_2", "name": "Average"}
    assert record["before"] == {"name": "Pump power average"}
    plan = (snap / "plan.json").read_bytes()
    assert WEBHOOK.encode() not in plan


# --- renames -------------------------------------------------------------


def test_swap_goes_through_temporary_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert renames(b) == [
        ("sensor.pump_power_2", "sensor.pump_power_atlas_tmp"),
        ("sensor.pump_power", "sensor.pump_power_2_atlas_tmp"),
        ("sensor.relay_power", "sensor.kitchen_power"),
        ("sensor.pump_power_atlas_tmp", "sensor.pump_power"),
        ("sensor.pump_power_2_atlas_tmp", "sensor.pump_power_2"),
    ]
    assert (
        "> entity  sensor.pump_power_2 -> sensor.pump_power  (through sensor.pump_power_atlas_tmp)"
        in result.output
    )
    assert "Summary: 22 created, 20 updated, 3 renamed, 3 unchanged." in result.output
    # The field writes go to the entities' new ids.
    [average] = [m for m in entity_writes(b) if m.get("name") == "Pump power average"]
    assert average["entity_id"] == "sensor.pump_power_2"
    assert by_id(b, "sensor.pump_power_2")["unique_id"] == POWER_AVG


def test_rename_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(
        rebuilt_data(
            **{
                "sensor.pump_power": "sensor.kitchen_power",
                "sensor.pump_power_2": "sensor.pump_power",
                "sensor.kitchen_power": "sensor.pump_power_2",
            }
        )
    )
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert len(renames(b)) == 6
    assert all(new.endswith("_atlas_tmp") for _, new in renames(b)[:3])
    assert (entity(b, POWER)["entity_id"], entity(b, POWER_AVG)["entity_id"]) == (
        "sensor.pump_power",
        "sensor.pump_power_2",
    )
    assert entity(b, KITCHEN)["entity_id"] == "sensor.kitchen_power"


def test_rename_target_held_by_other_entity_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    other = copy.deepcopy(next(r for r in data["entities"] if r["unique_id"] == KITCHEN))
    other.update(id="0" * 32, unique_id="someone-else", entity_id="sensor.kitchen_power")
    data["entities"].append(other)
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! entity sensor.relay_power refused: entity_id sensor.kitchen_power is held by "
        "another entity" in flat(result)
    )
    # The entity's other fields are still written, at the id it keeps.
    assert by_id(b, "sensor.relay_power")["options"]["sensor"]["unit_of_measurement"] == "kW"
    assert ("sensor.relay_power", "sensor.kitchen_power") not in renames(b)


def test_rename_target_held_by_state_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["states"].append({"entity_id": "sensor.kitchen_power", "state": "1", "attributes": {}})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "refused: entity_id sensor.kitchen_power is held by a state with no registry entry"
        in flat(result)
    )
    assert renames(b)[-2:] == [
        ("sensor.pump_power_atlas_tmp", "sensor.pump_power"),
        ("sensor.pump_power_2_atlas_tmp", "sensor.pump_power_2"),
    ]


def test_refused_rename_refuses_rename_onto_its_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [
        stable(POWER, "sensor.pump_power_2"),  # held by POWER_AVG, which cannot move
        stable(POWER_AVG, "sensor.kitchen_power"),  # held by KITCHEN, which stays
        stable(KITCHEN, "sensor.kitchen_power"),
    ]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert "sensor.pump_power_2 refused: entity_id sensor.kitchen_power is held by another" in out
    assert "sensor.pump_power refused: entity_id sensor.pump_power_2 is held by another" in out
    assert fake.writes == []


def test_two_items_with_one_target_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [stable(POWER, "sensor.pump_new"), stable(POWER_AVG, "sensor.pump_new")]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert flat(result).count("refused: 2 file items set entity_id sensor.pump_new") == 2
    assert fake.writes == []


def test_rename_into_another_domain_unmatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A unique_id matches only within the domain of the file's entity_id.
    item = stable(KITCHEN, "switch.kitchen_power", name="Fridge")
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "1 unmatched" in result.output
    assert fake.writes == []


@pytest.mark.parametrize("held_by", ["entity", "state"])
def test_swap_with_temporary_id_taken_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, held_by: str
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    temp = "sensor.pump_power_atlas_tmp"
    if held_by == "entity":
        kitchen = next(r for r in data["entities"] if r["unique_id"] == KITCHEN)
        data["entities"].append(intruder(kitchen, temp))
    else:
        data["states"].append({"entity_id": temp, "state": "1", "attributes": {}})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert f"sensor.pump_power_2 refused: temporary entity_id {temp} is taken" in out
    assert "sensor.pump_power refused: entity_id sensor.pump_power_2 is held by another" in out
    assert renames(b) == [("sensor.relay_power", "sensor.kitchen_power")]


def test_temporary_id_registered_since_plan_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    other = intruder(entity(b, KITCHEN), "sensor.pump_power_atlas_tmp")
    on_list_call(b, 2, lambda fake: fake.registries["entities"].append(other))
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert (
        "sensor.pump_power_2 conflict: sensor.pump_power_atlas_tmp was registered in HA since "
        "the plan" in out
    )
    assert "sensor.pump_power skipped: the rename of sensor.pump_power_2 did not run" in out
    assert renames(b) == [("sensor.relay_power", "sensor.kitchen_power")]


def test_rename_onto_id_of_held_back_rename_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [
        stable(POWER_AVG, "sensor.pump_power"),  # through a temporary id, as POWER moves
        stable(POWER, "sensor.pump_new"),
    ]
    fake = FakeHA()
    other = intruder(entity(fake, KITCHEN), "sensor.pump_new")
    on_list_call(fake, 2, lambda fake: fake.registries["entities"].append(other))
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert "sensor.pump_power conflict: sensor.pump_new was registered in HA since the plan" in out
    assert "sensor.pump_power_2 skipped: the rename of sensor.pump_power did not run" in out
    assert renames(fake) == []
    assert entity(fake, POWER_AVG)["entity_id"] == "sensor.pump_power_2"


def test_failed_first_pass_moves_partner_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def refuse(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg.get("new_entity_id") == "sensor.pump_power_2_atlas_tmp":
            raise HAClientError(f"{ENTITY_UPDATE} failed: Unknown error", "unknown_error")

    b.before[ENTITY_UPDATE] = [refuse]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    out = flat(result)
    assert "sensor.pump_power failed: " in out
    assert "sensor.pump_power_2 skipped: sensor.pump_power is still held; moved back" in out
    assert "sensor.pump_power skipped: its rename did not run" in out
    assert renames(b) == [
        ("sensor.pump_power_2", "sensor.pump_power_atlas_tmp"),
        ("sensor.pump_power", "sensor.pump_power_2_atlas_tmp"),
        ("sensor.relay_power", "sensor.kitchen_power"),
        ("sensor.pump_power_atlas_tmp", "sensor.pump_power_2"),
    ]
    assert entity(b, POWER)["entity_id"] == "sensor.pump_power_2"
    assert "display_precision" not in entity(b, POWER)["options"]["sensor"]

    b.before.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    exported = export(monkeypatch, b, tmp_path / "b")
    assert (exported / "entities.yaml").read_bytes() == (a / "entities.yaml").read_bytes()


def test_rename_target_held_by_state_refused_with_only_entities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    data["states"].append({"entity_id": "sensor.kitchen_new", "state": "1", "attributes": {}})
    fake = FakeHA(data)
    atlas = write(tmp_path / "a", {"entities": [stable(KITCHEN, "sensor.kitchen_new")]})
    result = apply(monkeypatch, fake, atlas, tmp_path / "s", "--only", "entities")
    assert result.exit_code == 3, result.output
    assert (
        "refused: entity_id sensor.kitchen_new is held by a state with no registry entry"
        in flat(result)
    )
    assert fake.writes == []


def test_failed_rename_stops_later_stages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(rebuilt_data(**{"sensor.workshop_temperature": "sensor.air_temperature"}))

    def refuse(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg.get("new_entity_id") == "sensor.pump_power" and msg["entity_id"].endswith("_tmp"):
            raise HAClientError(f"{ENTITY_UPDATE} failed: Unknown error", "unknown_error")

    b.before[ENTITY_UPDATE] = [refuse]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "x entity  sensor.pump_power_2 failed: " in result.output
    assert "later stages were not run" in result.output
    assert sensor_writes(b) == []
    assert entity(b, POWER)["entity_id"] == "sensor.pump_power_atlas_tmp"
    assert entity(b, POWER_AVG)["entity_id"] == "sensor.pump_power_2"

    b.before.clear()
    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert renames(b) == [("sensor.pump_power_atlas_tmp", "sensor.pump_power")]
    assert len(sensor_writes(b)) == 1
    exported = export(monkeypatch, b, tmp_path / "b")
    for name in ("entities.yaml", "areas.yaml"):
        assert (exported / name).read_bytes() == (a / name).read_bytes(), name


def test_area_sensors_set_after_renames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(rebuilt_data(**{"sensor.workshop_temperature": "sensor.air_temperature"}))
    result = invoke(monkeypatch, b, "apply", str(a), dry_run=True)
    assert "> entity  sensor.air_temperature -> sensor.workshop_temperature\n" in result.output
    assert "temperature_entity_id: null -> sensor.workshop_temperature" in result.output
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    sent = b.writes
    last_entity = max(i for i, (cmd, _) in enumerate(sent) if cmd == ENTITY_UPDATE)
    assert sent[last_entity + 1 : -1] == [(AREA_UPDATE, sensor_writes(b)[0])]
    assert sent[-1][0] == "energy/save_prefs"
    assert sensor_writes(b)[0]["temperature_entity_id"] == "sensor.workshop_temperature"
    assert area_row(b)["temperature_entity_id"] == "sensor.workshop_temperature"


def test_area_sensor_uses_live_id_when_rename_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = rebuilt_data(**{"sensor.workshop_temperature": "sensor.air_temperature"})
    data["states"].append(
        {"entity_id": "sensor.workshop_temperature", "state": "1", "attributes": {}}
    )
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert area_row(b)["temperature_entity_id"] == "sensor.air_temperature"


def test_area_sensor_follows_entity_with_only_areas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = rebuilt_data(**{"sensor.workshop_temperature": "sensor.air_temperature"})
    real = next(r for r in data["entities"] if r["unique_id"] == TEMPERATURE)
    outdoor = intruder(real, "sensor.workshop_temperature")
    outdoor.update(unique_id="outdoor", device_id=None)
    data["entities"].append(outdoor)
    state = next(s for s in data["states"] if s["entity_id"] == "sensor.air_temperature")
    data["states"].append({**copy.deepcopy(state), "entity_id": "sensor.workshop_temperature"})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "labels,floors,areas")
    assert result.exit_code == 0, result.output
    assert area_row(b)["temperature_entity_id"] == "sensor.air_temperature"
    assert entity_writes(b) == []


def test_area_sensor_of_unmatched_entity_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    real = next(r for r in data["entities"] if r["unique_id"] == TEMPERATURE)
    data["entities"].remove(real)  # its integration is not set up yet
    outdoor = intruder(real, "sensor.workshop_temperature")
    outdoor.update(platform="acme_weather", unique_id="outdoor", device_id=None)
    data["entities"].append(outdoor)  # and another sensor took its entity_id
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "garage_2 refused: temperature_entity_id sensor.workshop_temperature: the file's "
        "entity sensor.workshop_temperature matches no live entity" in flat(result)
    )
    assert area_row(b)["temperature_entity_id"] is None


def test_rename_conflicts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    calls: list[int] = []
    intruder = copy.deepcopy(entity(b, KITCHEN))
    intruder.update(id="0" * 32, unique_id="intruder", entity_id="sensor.kitchen_power")

    def race(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the read before stage 4 writes
            fake.registries["entities"].append(intruder)
            entity(fake, POWER_AVG)["entity_id"] = "sensor.someone_renamed_it"

    b.before[ENTITY_LIST] = [race]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert (
        "sensor.relay_power conflict: sensor.kitchen_power was registered in HA since the plan"
        in out
    )
    assert (
        "sensor.pump_power conflict: sensor.pump_power was removed from HA since the plan" in out
    )
    assert renames(b) == [
        ("sensor.pump_power_2", "sensor.pump_power_atlas_tmp"),
        ("sensor.pump_power_atlas_tmp", "sensor.pump_power"),
    ]


# --- fields --------------------------------------------------------------


def test_options_merge_keeps_integration_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    assert entity(b, POWER)["options"] == {
        "sensor": {"display_precision": 1, "suggested_display_precision": 2},
        "sensor.private": {"suggested_unit_of_measurement": "W"},
        "conversation": {"should_expose": False},
    }
    [sent] = [
        m
        for m in entity_writes(b)
        if m.get("options_domain") and m["entity_id"] == "sensor.pump_power"
    ]
    assert sent == {
        "entity_id": "sensor.pump_power",
        "options_domain": "sensor",
        "options": {"suggested_display_precision": 2, "display_precision": 1},
    }


def test_options_from_live_at_write_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = stable(KITCHEN, "sensor.kitchen_power", options={"sensor": {"display_precision": 3}})
    fake = FakeHA()
    calls: list[int] = []

    def integration_writes(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the read before stage 4 writes
            entity(fake, KITCHEN)["options"]["sensor"]["suggested_display_precision"] = 1

    fake.before[ENTITY_LIST] = [integration_writes]
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert entity(fake, KITCHEN)["options"]["sensor"] == {
        "unit_of_measurement": "kW",
        "suggested_display_precision": 1,
        "display_precision": 3,
    }


def test_null_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        stable(POWER, "sensor.pump_power", options=None),
        stable("hub-7f3a-r2-switch", "switch.pump_relay", labels=None, icon=None, area_id=None),
        stable("4011223344-12-49-0-Humidity", "sensor.workshop_humidity", platform="zwave_js",
               aliases=None),
        {"match": {"platform": "integration", "entity_id": "sensor.house_energy_in"},
         "categories": None},
        stable("sensor-0042-contact", "binary_sensor.door_contact", hidden=None),
    ]  # fmt: skip
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert entity(fake, POWER)["options"]["sensor"] == {"suggested_display_precision": 2}
    relay = by_id(fake, "switch.pump_relay")
    assert (relay["labels"], relay["icon"], relay["area_id"]) == ([], None, None)
    assert by_id(fake, "sensor.workshop_humidity")["aliases"] == [None]
    assert by_id(fake, "sensor.house_energy_in")["categories"] == {}
    assert by_id(fake, "binary_sensor.door_contact")["hidden_by"] is None
    assert "options.sensor.display_precision: 1 -> null" in result.output
    assert 'categories.helpers: "01KB9C4T3G0000000000000001" -> null' in result.output

    fake.sent.clear()
    result = apply(monkeypatch, fake, write(tmp_path / "b", {"entities": items}), tmp_path / "s2")
    assert result.exit_code == 0, result.output
    assert fake.writes == []


def test_absent_fields_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = stable("hub-7f3a-r2-switch", "switch.pump_relay", name="Pool pump")
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert entity_writes(fake) == [{"entity_id": "switch.pump_relay", "name": "Pool pump"}]
    assert by_id(fake, "switch.pump_relay")["area_id"] == "garage_2"


def test_enable_refused_under_disabled_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    hub = next(d for d in data["devices"] if d.get("model") == "Hub H1")
    hub["disabled_by"] = "user"
    item = stable(SIGNAL, "sensor.hub_signal", disabled=False)
    fake = FakeHA(data)
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert (
        "! entity sensor.hub_signal refused: its device is disabled (user); enable the device "
        "first" in flat(result)
    )
    assert fake.writes == []

    device = {"match": {"identifiers": [["acme_hub", "hub-7f3a-controller"]]}, "disabled": False}
    atlas = write(tmp_path / "b", {"devices": [device], "entities": [item]})
    result = apply(monkeypatch, fake, atlas, tmp_path / "s2")
    assert result.exit_code == 0, result.output
    assert commands(fake) == [DEVICE_UPDATE, ENTITY_UPDATE]
    assert by_id(fake, "sensor.hub_signal")["disabled_by"] is None
    assert "Enabled entities: 1 load when their integration reloads, after 30 s." in result.output


def test_enable_needing_restart_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    for row in data["entities"]:
        if row["unique_id"] == SIGNAL:
            row["config_entry_id"] = None
    items = [
        stable(SIGNAL, "sensor.hub_signal", disabled=False),
        stable("hub-7f3a-controller-led", "switch.hub_led", disabled=True),
    ]
    fake = FakeHA(data)
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert "Enabled entities: 1 load once Home Assistant restarts." in result.output
    assert by_id(fake, "switch.hub_led")["disabled_by"] == "user"


@pytest.mark.parametrize(
    ("field", "value", "held", "message"),
    [
        ("disabled", False, "disabled_by integration", "apply clears only user"),
        ("hidden", True, "hidden_by integration", "apply sets or clears only user"),
        ("hidden", False, "hidden_by integration", "apply sets or clears only user"),
    ],
)
def test_enable_or_hide_set_by_ha_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: bool,
    held: str,
    message: str,
) -> None:
    data = load_fixture()
    name, by = held.split()
    for row in data["entities"]:
        if row["unique_id"] == KITCHEN:
            row[name] = by
    item = stable(KITCHEN, "sensor.kitchen_power", **{field: value})
    fake = FakeHA(data)
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert f"refused: {name} is {by} in HA; {message}" in flat(result)
    assert fake.writes == []


@pytest.mark.parametrize("by", ["device", "integration", "config_entry"])
def test_disable_replaces_one_set_by_ha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, by: str
) -> None:
    data = load_fixture()
    for row in data["entities"]:
        if row["unique_id"] == KITCHEN:
            row["disabled_by"] = by
    item = stable(KITCHEN, "sensor.kitchen_power", disabled=True, name="Fridge")
    fake = FakeHA(data)
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert entity_writes(fake) == [
        {"entity_id": "sensor.kitchen_power", "name": "Fridge", "disabled_by": "user"}
    ]
    assert entity(fake, KITCHEN)["disabled_by"] == "user"


def test_rebuild_keeps_user_disable_under_disabled_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    for row in data["entities"]:
        if row["entity_id"] == "binary_sensor.door_contact":
            row["disabled_by"] = "user"  # disabled before its device, so HA kept user
    a = export(monkeypatch, FakeHA(data), tmp_path / "a")
    b = FakeHA(empty_fixture(data))
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    door = by_id(b, "binary_sensor.door_contact")
    assert (door["disabled_by"], door["hidden_by"], door["device_class"]) == (
        "user",
        "user",
        "window",
    )
    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert b.writes == []


def test_refused_field_leaves_other_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = stable("hub-7f3a-controller-led", "switch.hub_led", hidden=True, name="LED")
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "switch.hub_led refused: hidden_by is integration in HA" in flat(result)
    assert entity_writes(fake) == [{"entity_id": "switch.hub_led", "name": "LED"}]
    assert by_id(fake, "switch.hub_led")["hidden_by"] == "integration"


def test_enable_with_device_in_same_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    hub = next(d for d in data["devices"] if d.get("model") == "Hub H1")
    hub["disabled_by"] = "user"
    for row in data["entities"]:
        if row["entity_id"] == "switch.hub_led":
            row["disabled_by"] = "device"
    device = {"match": {"identifiers": [["acme_hub", "hub-7f3a-controller"]]}, "disabled": False}
    item = stable("hub-7f3a-controller-led", "switch.hub_led", disabled=False)
    fake = FakeHA(data)
    atlas = write(tmp_path / "a", {"devices": [device], "entities": [item]})
    result = apply(monkeypatch, fake, atlas, tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert commands(fake) == [DEVICE_UPDATE]  # enabling the device enables the entity
    assert by_id(fake, "switch.hub_led")["disabled_by"] is None


def test_enable_under_config_entry_disabled_device_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = stable("acct-91-weather", "weather.home", platform="acme_cloud", disabled=False)
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "refused: its device is disabled (config_entry); enable the device first" in (
        flat(result)
    )


def test_own_area_without_own_name_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = stable("sensor-0042-contact", "binary_sensor.door_contact", area_id="den")
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "an entity without a name of its own cannot have an area of its own" in flat(result)
    assert fake.writes == []

    item["name"] = "Back door"
    result = apply(monkeypatch, fake, write(tmp_path / "b", {"entities": [item]}), tmp_path / "s2")
    assert result.exit_code == 0, result.output
    assert by_id(fake, "binary_sensor.door_contact")["area_id"] == "den"


def test_unknown_references_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        stable("hub-7f3a-r2-switch", "switch.pump_relay", area_id="attic"),
        stable(POWER_AVG, "sensor.pump_power_2", labels=["nope"]),
        {"match": {"platform": "integration", "entity_id": "sensor.house_energy_in"},
         "categories": {"helpers": "01KB9C4T3G00000000000000ZZ"}},
        stable(KITCHEN, "sensor.kitchen_power", name="Fridge"),
    ]  # fmt: skip
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert "refused: area_id attic: no area attic in the file or in HA" in out
    assert "refused: labels nope: no label nope in the file or in HA" in out
    assert "refused: categories helpers 01KB9C4T3G00000000000000ZZ: no such category in HA" in out
    assert entity_writes(fake) == [{"entity_id": "sensor.kitchen_power", "name": "Fridge"}]


def test_unknown_category_refuses_only_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = stable(
        KITCHEN,
        "sensor.kitchen_power",
        icon="mdi:fridge",
        categories={"helpers": "01KB9C4T3G0000000000000001"},
    )
    data = empty_fixture()
    data["categories"] = {}
    b = FakeHA(data)
    result = apply(monkeypatch, b, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "no such category in HA" in flat(result)
    assert renames(b) == [("sensor.relay_power", "sensor.kitchen_power")]
    assert entity(b, KITCHEN)["categories"] == {}
    assert entity(b, KITCHEN)["icon"] == "mdi:fridge"


def test_references_follow_ids_ha_assigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def take(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg.get("name") == "garage_2" and fake.row("areas", "garage_2") is None:
            fake.registries["areas"].append(
                {"area_id": "garage_2", "name": "Other garage", "aliases": [], "labels": []}
            )
        if msg.get("name") == "Solar" and fake.row("labels", "solar") is None:
            fake.registries["labels"].append({"label_id": "solar", "name": "Sun"})

    b.before["config/area_registry/create"] = [take]
    b.before["config/label_registry/create"] = [take]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    relay = by_id(b, "switch.pump_relay")
    assert relay["area_id"] == "garage_2_2"
    assert relay["labels"] == ["critical_load", "solar_2"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("entity_id", "sensor.Bad", "entity_id 'sensor.Bad' is not a valid entity id"),
        ("name", 3, "name must be a string"),
        ("labels", "solar", "labels must be a list of strings"),
        ("aliases", [1], "aliases must be a list of strings and nulls"),
        ("categories", {"Bad Scope": "x"}, "categories must be a mapping from scope to category"),
        ("hidden", "yes", "hidden must be true or false"),
        ("options", {"light": {}}, "options light is not one of sensor, weather"),
        ("options", {"sensor": {"suggested_display_precision": 1}}, "options sensor "
         "suggested_display_precision is not one of display_precision, unit_of_measurement"),
        ("options", {"sensor": {"display_precision": "1"}}, "options sensor display_precision "
         "must be a whole number"),
    ],
)  # fmt: skip
def test_bad_field_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: Any, message: str
) -> None:
    item = {**stable(POWER, "sensor.pump_power"), field: value}
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert f"refused: {message}" in flat(result)
    assert fake.writes == []


# --- matching ------------------------------------------------------------


def test_unstable_key_held_by_other_platform_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = {
        "match": {"platform": "template", "entity_id": "sensor.house_energy_in"},
        "icon": "x:y",
    }
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "refused: sensor.house_energy_in is held by platform integration" in flat(result)


def test_mobile_app_matched_after_reregistration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    pixel = by_id(b, "sensor.pixel_battery_level")
    assert WEBHOOK not in pixel["unique_id"]
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    assert by_id(b, "sensor.pixel_battery_level")["area_id"] == "den"
    assert by_id(b, "sensor.house_energy_in")["icon"] == "mdi:transmission-tower"


def test_two_items_for_one_entity_both_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [
        stable(KITCHEN, "sensor.kitchen_power", name="A"),
        {"match": {"platform": "acme_hub", "unique_id": KITCHEN}, "icon": "mdi:x"},
    ]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert flat(result).count("refused: another file item matches the same live entity") == 2
    assert fake.writes == []


def test_unique_id_matched_within_domain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    twin = copy.deepcopy(next(r for r in data["entities"] if r["unique_id"] == KITCHEN))
    twin.update(id="0" * 32, entity_id="switch.kitchen_power", options={})
    data["entities"].append(twin)
    fake = FakeHA(data)
    a = export(monkeypatch, fake, tmp_path / "a")
    keys = [
        i["match"]
        for i in load_atlas(a).docs["entities"]
        if i["match"].get("unique_id") == KITCHEN
    ]
    assert len(keys) == 2
    item = stable(KITCHEN, "switch.kitchen_power", name="Fridge switch")
    result = apply(monkeypatch, fake, write(tmp_path / "b", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert by_id(fake, "switch.kitchen_power")["name"] == "Fridge switch"
    item = {"match": {"platform": "acme_hub", "unique_id": KITCHEN}, "name": "Either"}
    result = apply(monkeypatch, fake, write(tmp_path / "c", {"entities": [item]}), tmp_path / "s2")
    assert result.exit_code == 3, result.output
    assert "refused: unique_id matches 2 live entities" in flat(result)


# --- safety --------------------------------------------------------------


def test_concurrent_change_is_a_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    calls: list[int] = []

    def edit(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 3:  # the read after the renames, before the other stage 4 writes
            by_id(fake, "switch.pump_relay")["name"] = "Someone's name"

    b.before[ENTITY_LIST] = [edit]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! entity switch.pump_relay conflict: name changed in HA since the plan "
        '(null -> "Someone\'s name")' in flat(result)
    )
    assert by_id(b, "switch.pump_relay")["area_id"] is None
    assert "switch.pump_relay" not in {m["entity_id"] for m in entity_writes(b)}


def test_entity_replaced_since_plan_not_renamed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    other = intruder(entity(b, KITCHEN), "sensor.relay_power")

    def swap(fake: FakeHA) -> None:
        entity(fake, KITCHEN)["entity_id"] = "sensor.moved_by_user"
        fake.registries["entities"].append(other)

    on_list_call(b, 2, swap)  # the read before stage 4 writes
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert (
        "sensor.relay_power conflict: sensor.relay_power is another entity since the plan" in out
    )
    assert "sensor.kitchen_power skipped: its rename did not run" in out
    assert ("sensor.relay_power", "sensor.kitchen_power") not in renames(b)
    assert entity(b, "intruder")["entity_id"] == "sensor.relay_power"
    assert "unit_of_measurement" not in entity(b, "intruder")["options"]["sensor"]
    assert entity(b, KITCHEN)["entity_id"] == "sensor.moved_by_user"


def test_entity_replaced_since_plan_not_updated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = stable(KITCHEN, "sensor.kitchen_power", name="Fridge")
    fake = FakeHA()
    other = intruder(entity(fake, KITCHEN), "sensor.kitchen_power")

    def swap(fake: FakeHA) -> None:
        entity(fake, KITCHEN)["entity_id"] = "sensor.moved_by_user"
        fake.registries["entities"].append(other)

    on_list_call(fake, 2, swap)
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert (
        "sensor.kitchen_power conflict: sensor.kitchen_power is another entity since the plan"
        in flat(result)
    )
    assert entity_writes(fake) == []


def test_verify_checks_the_planned_entity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = stable(KITCHEN, "sensor.kitchen_power", name="Fridge")
    fake = FakeHA()

    def swap(fake: FakeHA, msg: dict[str, Any]) -> None:
        real = entity(fake, KITCHEN)
        fake.registries["entities"].append(intruder(real, "sensor.kitchen_power"))
        real.update(entity_id="sensor.moved_by_user", name=None)

    fake.after[ENTITY_UPDATE] = [swap]
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"entities": [item]}), tmp_path / "s")
    assert result.exit_code == 1, result.output
    assert 'entity sensor.kitchen_power: name: expected "Fridge", HA has null' in flat(result)


def test_verify_mismatch_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def revert(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg.get("entity_id") == "light.den_lamp":
            by_id(fake, "light.den_lamp")["icon"] = None

    b.after[ENTITY_UPDATE] = [revert]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "Verify failed after stage 4; stopping:" in result.output
    assert "entity light.den_lamp: icon: expected mdi:lamp, HA has null" in flat(result)
    assert sensor_writes(b) == []


# --- rendering -----------------------------------------------------------


def test_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        stable(POWER, "sensor.pump_power", name="Pump"),
        stable(POWER_AVG, "sensor.pump_power_2"),
        stable(KITCHEN, "sensor.fridge_power", options={"sensor": {"display_precision": 0}}),
        stable(SIGNAL, "sensor.hub_signal", hidden=True),
        stable("hub-7f3a-r2-switch", "switch.pump_relay"),
        stable("gone", "sensor.gone"),
        stable("gone-too", "sensor.gone_too", platform="zwave_js"),
        stable("hub-7f3a-controller-led", "switch.hub_led", hidden=False),
    ]
    fake = FakeHA(empty_fixture())
    atlas = write(tmp_path / "a", {"entities": items})
    result = invoke(monkeypatch, fake, "apply", str(atlas), dry_run=True)
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Plan for http://ha.test:8123  (HA 2026.10.4, atlas format 1)\n"
        "Stage 4  entities  (8 in file, 15 live)\n"
        "  > entity  sensor.pump_power_2 -> sensor.pump_power  (through sensor.pump_power_atlas_tmp)\n"
        "  > entity  sensor.pump_power -> sensor.pump_power_2  (through sensor.pump_power_2_atlas_tmp)\n"
        "  > entity  sensor.relay_power -> sensor.fridge_power\n"
        '  ~ entity  sensor.pump_power name: null -> "Pump"\n'
        "  ~ entity  sensor.fridge_power options.sensor.display_precision: null -> 0\n"
        "  ~ entity  sensor.hub_signal hidden_by: null -> user\n"
        "  ! entity  switch.hub_led    refused: hidden_by is integration in HA; apply sets or "
        "clears only user\n"
        "  ? entity  2 unmatched:      acme_hub 1, zwave_js 1  (-v lists them)\n"
        "  = entity  1 unchanged\n"
        "Summary: 0 create, 3 update, 3 rename, 1 refused, 2 unmatched, 1 unchanged. "
        "Dry run: nothing written.\n"
    )
    assert fake.writes == []
