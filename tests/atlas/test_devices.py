"""``export`` and ``apply`` for devices."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from hass_atlas.atlas import apply_cmd, export_cmd
from hass_atlas.atlas.domains.devices import format_mac
from hass_atlas.atlas.yamlio import AtlasFormatError, load_atlas
from hass_atlas.ha_client import HAClientError
from tests.atlas.fake_ha import DEVICE_UPDATE, FakeHA, empty_fixture, load_fixture
from tests.atlas.test_apply import apply, commands, export, flat, invoke, write

PLUG = ("mac", "02:00:5e:10:00:01")
HUB_MAC = ("mac", "02:00:5e:10:00:02")
RELAY = ("acme_hub", "hub-7f3a")
DOOR = ("acme_hub", "sensor-0042")
BATTERY = ("acme_hub", "sensor-0042-battery")
CLOUD = ("acme_cloud", "acct-91")
MULTI = ("zwave_js", "4011223344-12")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(apply_cmd, "_stdin_is_tty", lambda: False)


def device(fake: FakeHA, pair: tuple[str, str], model: str | None = None) -> dict[str, Any]:
    """The one live device holding *pair* as an identifier or connection."""
    rows = [
        row
        for row in fake.registries["devices"]
        if list(pair) in row["identifiers"] + row.get("connections", [])
        and (model is None or row.get("model") == model)
    ]
    assert len(rows) == 1, (pair, model, len(rows))
    return rows[0]


def by_identifiers(*pairs: tuple[str, str], **match: str) -> dict[str, Any]:
    return {"identifiers": [list(p) for p in pairs], **match}


def device_writes(fake: FakeHA) -> list[dict[str, Any]]:
    return [msg for cmd, msg in fake.writes if cmd == DEVICE_UPDATE]


AREA_UPDATE = "config/area_registry/update"
SENSOR_FIELDS = ("temperature_entity_id", "humidity_entity_id")
SENSOR_PENDING = "~ area garage_2 temperature_entity_id: null -> sensor.workshop_temperature"


def rebuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sensors: bool = True
) -> tuple[Path, FakeHA]:
    """An export of the fixture, and a rebuilt HA with every other domain applied.

    With ``sensors=False`` the areas' sensors are left for stage 5 to write.
    """
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    first = a
    if not sensors:
        docs = load_atlas(a).docs
        for item in docs["areas"]:
            for name in SENSOR_FIELDS:
                item.pop(name, None)
        first = write(tmp_path / "a0", {k: v for k, v in docs.items() if k != "devices"})
    result = apply(monkeypatch, b, first, tmp_path / "snap0", "--only", "floors,labels,areas")
    assert result.exit_code == 0, result.output
    b.sent.clear()
    return a, b


# --- export ------------------------------------------------------------


def test_export_only_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, fake, "export", "--out", str(out), "--only", "devices")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["atlas.yaml", "devices.yaml"]
    assert [cmd for cmd, _ in fake.sent] == ["config/device_registry/list"]


def test_export_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = load_atlas(export(monkeypatch, FakeHA(), tmp_path / "a")).docs["devices"]
    text = (tmp_path / "a" / "devices.yaml").read_text(encoding="utf-8")
    assert "hub-7f3a-controller" not in text  # no user value
    assert "Hub H1" not in text and "Plug P1" not in text and "MS-6" not in text
    cloud = next(i for i in items if i["match"] == by_identifiers(CLOUD))
    assert cloud == {"match": by_identifiers(CLOUD), "name_by_user": "Weather bridge"}
    battery = next(i for i in items if i["match"] == by_identifiers(BATTERY))
    assert "disabled" not in battery  # disabled by its parent, not the user
    relays = [i["match"] for i in items if RELAY in map(tuple, i["match"].get("identifiers", []))]
    assert relays == [
        by_identifiers(RELAY, manufacturer="Acme", model="Relay R2"),
        by_identifiers(RELAY, manufacturer="Acme", model="Relay R4"),
    ]


def test_export_tie_fields_for_shared_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    other = copy.deepcopy(data["devices"][-1])
    other.update(id="0" * 32, identifiers=[], connections=[list(PLUG)], model="Plug P2")
    data["devices"].append(other)
    items = load_atlas(export(monkeypatch, FakeHA(data), tmp_path / "a")).docs["devices"]
    assert items[0]["match"] == {
        "connections": [list(PLUG)],
        "manufacturer": "Acme",
        "model": "Plug P1",
    }


def test_export_null_tie_fields_for_device_without_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    main = copy.deepcopy(data["devices"][0])
    main.update(id="0" * 32, identifiers=[list(BATTERY)], name_by_user="Spare", model="B1")
    data["devices"].append(main)
    a = export(monkeypatch, FakeHA(data), tmp_path / "a")
    items = load_atlas(a).docs["devices"]
    items = [i for i in items if i["match"].get("identifiers") == [list(BATTERY)]]
    assert [i["match"] for i in items] == [
        by_identifiers(BATTERY, manufacturer="Acme Sensors", model="B1"),
        {"identifiers": [list(BATTERY)], "manufacturer": None, "model": None},
    ]
    for row in data["devices"]:
        row["name_by_user"] = None
    fake = FakeHA(data)
    result = apply(monkeypatch, fake, a, tmp_path / "snap", "--only", "devices")
    assert result.exit_code == 0, result.output
    assert device(fake, BATTERY, "B1")["name_by_user"] == "Spare"
    [child] = [row for row in fake.registries["devices"] if "model" not in row]
    assert child["name_by_user"] == "Door battery"


def test_export_leaves_out_devices_it_cannot_tell_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    for row in data["devices"]:
        if row.get("model") == "Relay R4":
            row["model"] = "Relay R2"
            row["name_by_user"] = "Fridge relay"
    fake = FakeHA(data)
    out = tmp_path / "a"
    result = invoke(monkeypatch, fake, "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert (
        '2 devices have the match {"identifiers":[["acme_hub","hub-7f3a"]],'
        '"manufacturer":"Acme","model":"Relay R2"}; none is exported'
    ) in "".join(result.output.split("\n"))
    items = load_atlas(out).docs["devices"]
    assert len(items) == 5
    assert not any(list(RELAY) in i["match"].get("identifiers", []) for i in items)
    result = apply(monkeypatch, FakeHA(empty_fixture()), out, tmp_path / "snap")
    assert result.exit_code == 0, result.output


def test_export_counts_devices_without_user_values_as_ties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    for row in data["devices"]:
        if row.get("model") == "Relay R4":
            row.update(model="Relay R2", name_by_user=None, area_id=None, labels=[])
            row["disabled_by"] = None
    fake = FakeHA(data)
    out = tmp_path / "a"
    result = invoke(monkeypatch, fake, "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert (
        '2 devices have the match {"identifiers":[["acme_hub","hub-7f3a"]],'
        '"manufacturer":"Acme","model":"Relay R2"}; none is exported'
    ) in "".join(result.output.split("\n"))
    items = load_atlas(out).docs["devices"]
    assert not any(list(RELAY) in i["match"].get("identifiers", []) for i in items)
    result = invoke(monkeypatch, fake, "apply", str(out), "--check")
    assert result.exit_code == 0, result.output
    assert "refused" not in result.output


def test_export_warning_prints_identifiers_literally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    for row in data["devices"]:
        if row.get("identifiers") == [list(RELAY)]:
            row.update(identifiers=[["acme_hub", "a8:51:ab:9b"]], model="Relay R2")
    result = invoke(monkeypatch, FakeHA(data), "export", "--out", str(tmp_path / "a"))
    assert result.exit_code == 0, result.output
    assert '"a8:51:ab:9b"' in "".join(result.output.split("\n"))


def test_export_has_no_registry_or_config_entry_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = export(monkeypatch, FakeHA(), tmp_path / "a")
    exported = b"".join(p.read_bytes() for p in sorted(out.iterdir()))
    secrets: set[str] = set()
    for row in load_fixture()["devices"]:
        for name in ("id", "config_entry_id", "primary_config_entry", "via_device_id"):
            if row.get(name):
                secrets.add(row[name])
        secrets.update(row.get("config_entries", []))
        secrets.update(row.get("config_entries_subentries", {}))
        if row.get("parent_device_id"):
            secrets.add(row["parent_device_id"])
    assert len(secrets) == 13
    for secret in secrets:
        assert secret.encode() not in exported, secret
    for needle in (b"created_at", b"modified_at", b"serial", b"AS-000123"):
        assert needle not in exported


@pytest.mark.parametrize(
    ("raw", "mac"),
    [
        ("02:00:5E:10:00:01", "02:00:5e:10:00:01"),
        ("02-00-5E-10-00-01", "02:00:5e:10:00:01"),
        ("0200.5e10.0001", "02:00:5e:10:00:01"),
        ("02005E100001", "02:00:5e:10:00:01"),
        ("not-a-mac", "not-a-mac"),
    ],
)
def test_format_mac(raw: str, mac: str) -> None:
    assert format_mac(raw) == mac


# --- loading -------------------------------------------------------------


GOOD_MANIFEST = "format: 1\nhass_atlas: 0.0.0\ndomains: [devices]\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- area_id: den\n", "item 0: match is missing or not a mapping"),
        ("- match: {}\n", "match needs identifiers or connections, not both"),
        (
            "- match: {identifiers: [[a, b]], connections: [[mac, x]]}\n",
            "match needs identifiers or connections, not both",
        ),
        ("- match: {identifiers: []}\n", "must be a non-empty list of [domain, id] pairs"),
        (
            "- match: {identifiers: [[a, b, c]]}\n",
            "must be a non-empty list of [domain, id] pairs",
        ),
        ("- match: {identifiers: [a]}\n", "must be a non-empty list of [domain, id] pairs"),
        ("- match: {identifiers: [[a, b]], serial: x}\n", "unknown match fields ['serial']"),
        ("- match: {identifiers: [[a, b]], model: 2}\n", "match model must be a string or null"),
        (
            "- match: {identifiers: [[a, b], [c, d]]}\n- match: {identifiers: [[c, d], [a, b]]}\n",
            "item 1: duplicate match",
        ),
        (
            "- match: {connections: [[mac, 'AA:BB:CC:DD:EE:FF']]}\n"
            "- match: {connections: [[mac, aabbccddeeff]]}\n",
            "item 1: duplicate match",
        ),
    ],
)
def test_load_errors(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "atlas"
    path.mkdir()
    (path / "atlas.yaml").write_text(GOOD_MANIFEST, encoding="utf-8")
    (path / "devices.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(AtlasFormatError) as info:
        load_atlas(path)
    assert "devices.yaml: " in str(info.value)
    assert message in str(info.value)


# --- round trip and idempotence ----------------------------------------


def test_rebuild_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = rebuilt(monkeypatch, tmp_path)
    old_ids = {row["id"] for row in load_fixture()["devices"]}
    assert not old_ids & {row["id"] for row in b.registries["devices"]}
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert {cmd for cmd, _ in b.writes} == {DEVICE_UPDATE}
    sent = device_writes(b)
    assert len(sent) == 7
    assert {msg["device_id"] for msg in sent} <= {row["id"] for row in b.registries["devices"]}
    assert device(b, RELAY, "Relay R4")["area_id"] == "kitchen"
    assert device(b, RELAY, "Relay R2")["name_by_user"] == "Pump relay"
    assert device(b, BATTERY)["disabled_by"] == "device"  # HA's cascade from the door sensor
    exported = export(monkeypatch, b, tmp_path / "b")
    assert (exported / "devices.yaml").read_bytes() == (a / "devices.yaml").read_bytes()

    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert b.writes == []
    assert "= device  7 unchanged" in result.output


def test_dry_run_and_check_send_no_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = rebuilt(monkeypatch, tmp_path)
    result = invoke(monkeypatch, b, "apply", str(a), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "Summary: 0 create, 7 update, 12 unchanged. Dry run: nothing written." in result.output
    result = invoke(monkeypatch, b, "apply", str(a), "--check", "--only", "devices")
    assert result.exit_code == 2, result.output
    assert b.writes == []
    assert not (tmp_path / "home").exists()


def test_snapshot_before_holds_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = {"match": {"connections": [list(PLUG)]}, "area_id": "kitchen"}
    snap = tmp_path / "snap"
    fake = FakeHA()
    assert (
        apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), snap).exit_code == 0
    )
    assert load_atlas(snap / "before").manifest["domains"] == ["devices"]
    golden = Path(__file__).parent.parent / "fixtures" / "atlas" / "golden" / "devices.yaml"
    assert (snap / "before" / "devices.yaml").read_bytes() == golden.read_bytes()
    [record] = [json.loads(line) for line in (snap / "journal.jsonl").read_text().splitlines()]
    assert record["payload"] == {"device_id": device(fake, PLUG)["id"], "area_id": "kitchen"}
    assert record["before"] == {"area_id": "den"}


# --- matching ------------------------------------------------------------


def test_tie_broken_by_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = rebuilt(monkeypatch, tmp_path)
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    r2, r4 = device(b, RELAY, "Relay R2"), device(b, RELAY, "Relay R4")
    assert (r2["name_by_user"], r2["area_id"], sorted(r2["labels"])) == (
        "Pump relay",
        None,
        ["critical_load", "solar"],
    )
    assert (r4["name_by_user"], r4["area_id"]) == (None, "kitchen")


def test_identical_candidates_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    for row in data["devices"]:
        if row.get("model") == "Relay R4":
            row["model"] = "Relay R2"
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    out = flat(result)
    assert (
        "Pump relay (acme_hub hub-7f3a, Relay R2) refused: identifiers match 2 live devices; "
        "manufacturer and model do not tell them apart" in out
    )
    assert (
        "device (acme_hub hub-7f3a, Relay R4) refused: identifiers match 2 live devices; "
        "none has this manufacturer and model" in out
    )
    for row in b.registries["devices"]:
        if list(RELAY) in row["identifiers"]:
            assert (row["name_by_user"], row["area_id"], row["labels"]) == (None, None, [])
    assert device(b, PLUG)["area_id"] == "den"


def test_tie_without_manufacturer_and_model_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    atlas = write(
        tmp_path / "a", {"devices": [{"match": by_identifiers(RELAY), "area_id": "den"}]}
    )
    fake = FakeHA()
    result = apply(monkeypatch, fake, atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "refused: identifiers match 2 live devices; add manufacturer and model to the match "
        "to tell them apart" in flat(result)
    )
    atlas = write(
        tmp_path / "b",
        {"devices": [{"match": by_identifiers(RELAY, model="Relay R9"), "area_id": "den"}]},
    )
    result = apply(monkeypatch, fake, atlas, tmp_path / "snap2")
    assert "none has this manufacturer and model" in flat(result)
    assert fake.writes == []


def test_single_candidate_ignores_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = {"match": by_identifiers(DOOR, manufacturer="Other", model="X"), "area_id": "den"}
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert device(fake, DOOR)["area_id"] == "den"


def test_two_items_for_one_device_both_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [
        {"match": by_identifiers(("acme_hub", "hub-7f3a-controller")), "name_by_user": "Hub"},
        {"match": {"connections": [list(HUB_MAC)]}, "area_id": "den"},
        {"match": {"connections": [list(PLUG)]}, "area_id": "kitchen"},
    ]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert flat(result).count("refused: another file item matches the same live device") == 2
    assert [msg["area_id"] for msg in device_writes(fake)] == ["kitchen"]
    hub = device(fake, HUB_MAC)
    assert (hub["name_by_user"], hub["area_id"]) == (None, None)


def test_mac_connection_normalized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = {"match": {"connections": [["mac", "02-00-5E-10-00-01"]]}, "area_id": "kitchen"}
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert device(fake, PLUG)["area_id"] == "kitchen"


def test_unmatched_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        {"match": by_identifiers(("zwave_js", "4011223344-99")), "name_by_user": "Lost"},
        {"match": by_identifiers(("zwave_js", "4011223344-98"))},
        {"match": {"connections": [["mac", "02:00:5e:10:00:99"]]}, "area_id": "den"},
        {"match": by_identifiers(DOOR), "area_id": "den"},
    ]
    atlas = write(tmp_path / "a", {"devices": items})
    fake = FakeHA()
    result = invoke(monkeypatch, fake, "apply", str(atlas), dry_run=True)
    assert "  ? device  3 unmatched:      mac 1, zwave_js 2  (-v lists them)\n" in result.output
    result = invoke(monkeypatch, fake, "apply", str(atlas), "-v", dry_run=True)
    assert "  ? device  Lost (zwave_js 4011223344-99) unmatched: not in HA\n" in result.output
    assert "  ? device  (mac 02:00:5e:10:00:99) unmatched: not in HA\n" in result.output
    result = invoke(monkeypatch, fake, "apply", str(atlas), "--check")
    assert result.exit_code == 2, result.output

    result = apply(monkeypatch, fake, atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "Summary: 0 created, 1 updated, 3 unmatched, 0 unchanged." in result.output
    assert device(fake, DOOR)["area_id"] == "den"
    result = invoke(monkeypatch, fake, "apply", str(atlas), "--check")
    assert result.exit_code == 3, result.output


# --- references ----------------------------------------------------------


def test_references_follow_name_matched_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["areas"] = [
        {"area_id": "cookhouse", "name": "kitchen", "floor_id": None, "icon": None,
         "labels": [], "aliases": [], "picture": None, "temperature_entity_id": None,
         "humidity_entity_id": None},
    ]  # fmt: skip
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert device(b, RELAY, "Relay R4")["area_id"] == "cookhouse"
    assert b.row("areas", "kitchen") is None


def test_references_follow_ids_ha_assigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def intrude(registry: str, row: dict[str, Any], name: str) -> Any:
        def hook(fake: FakeHA, msg: dict[str, Any]) -> None:
            if msg.get("name") == name and fake.row(registry, next(iter(row.values()))) is None:
                fake.registries[registry].append(dict(row))

        return hook

    kitchen = {"area_id": "kitchen", "name": "Galley", "aliases": [], "labels": []}
    b.before["config/area_registry/create"] = [intrude("areas", kitchen, "Kitchen")]
    b.before["config/label_registry/create"] = [
        intrude("labels", {"label_id": "solar", "name": "Sun"}, "Solar")
    ]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert device(b, RELAY, "Relay R4")["area_id"] == "kitchen_2"
    assert sorted(device(b, RELAY, "Relay R2")["labels"]) == ["critical_load", "solar_2"]


def test_unknown_reference_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        {"match": by_identifiers(DOOR), "area_id": "attic"},
        {
            "match": by_identifiers(MULTI, ("zwave_js", "4011223344-12-271:2:1")),
            "labels": ["nope"],
        },
        {"match": by_identifiers(CLOUD), "area_id": "den"},
    ]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": items}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert "refused: area_id attic: no area attic in the file or in HA" in flat(result)
    assert "refused: labels nope: no label nope in the file or in HA" in flat(result)
    assert [msg["device_id"] for msg in device_writes(fake)] == [device(fake, CLOUD)["id"]]


def test_only_devices_refuses_areas_not_in_ha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "devices")
    assert result.exit_code == 3, result.output
    assert "area_id den: area den is not in HA, and --only leaves out areas" in flat(result)
    assert {msg["device_id"] for msg in device_writes(b)} == {
        device(b, CLOUD)["id"],
        device(b, DOOR)["id"],
        device(b, BATTERY)["id"],
    }


def test_area_create_failure_skips_its_devices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    calls: list[int] = []
    den = {"area_id": "den", "name": "Den", "aliases": [], "labels": []}

    def take(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 2 writes
            fake.registries["areas"].append(dict(den))

    b.before["config/area_registry/list"] = [take]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "conflict: den was created in HA since the plan" in flat(result)
    assert "! device Smart plug (mac 02:00:5e:10:00:01) skipped: area den was not created" in flat(
        result
    )
    assert device(b, PLUG)["area_id"] is None
    assert len(device_writes(b)) == 6


# --- fields --------------------------------------------------------------


def test_null_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        {"match": by_identifiers(MULTI, ("zwave_js", "4011223344-12-271:2:1")),
         "name_by_user": None, "area_id": None},
        {"match": by_identifiers(RELAY, model="Relay R2"), "labels": None},
    ]  # fmt: skip
    fake = FakeHA()
    multi, relay = device(fake, MULTI)["id"], device(fake, RELAY, "Relay R2")["id"]
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": items}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert device_writes(fake) == [
        {"device_id": multi, "name_by_user": None, "area_id": None},
        {"device_id": relay, "labels": []},
    ]
    assert (device(fake, MULTI)["name_by_user"], device(fake, MULTI)["area_id"]) == (None, None)
    assert (
        '~ device  Garage multisensor (zwave_js 4011223344-12) name_by_user: "Garage multisensor" -> null'
        in result.output
    )


def test_disable_and_enable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        {"match": {"connections": [list(PLUG)]}, "disabled": True},
        {"match": by_identifiers(DOOR), "disabled": False},
    ]
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": items}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert [msg.get("disabled_by", "-") for msg in device_writes(fake)] == ["user", None]
    assert device(fake, PLUG)["disabled_by"] == "user"
    assert device(fake, DOOR)["disabled_by"] is None
    assert device(fake, BATTERY)["disabled_by"] is None  # HA's cascade
    assert "disabled_by: null -> user" in result.output
    assert "disabled_by: user -> null" in result.output

    items[1]["disabled"] = None
    fake.sent.clear()
    result = apply(monkeypatch, fake, write(tmp_path / "b", {"devices": items}), tmp_path / "s2")
    assert result.exit_code == 0, result.output
    assert fake.writes == []


@pytest.mark.parametrize("held", ["config_entry", "integration", "device"])
@pytest.mark.parametrize("disabled", [True, False, None])
def test_disable_set_by_ha_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, held: str, disabled: bool | None
) -> None:
    data = load_fixture()
    for row in data["devices"]:
        if list(CLOUD) in row["identifiers"]:
            row["disabled_by"] = held
    fake = FakeHA(data)
    item = {"match": by_identifiers(CLOUD), "name_by_user": "Renamed", "disabled": disabled}
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert f"refused: disabled_by is {held} in HA; apply sets or clears only user" in flat(result)
    assert fake.writes == []
    assert device(fake, CLOUD)["name_by_user"] == "Weather bridge"


def test_config_entry_disabled_device_still_renamed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = {"match": by_identifiers(CLOUD), "name_by_user": "Renamed"}
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), tmp_path / "s")
    assert result.exit_code == 0, result.output
    assert device(fake, CLOUD)["name_by_user"] == "Renamed"
    assert device(fake, CLOUD)["disabled_by"] == "config_entry"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("disabled", "yes", "disabled must be true or false"),
        ("name_by_user", 3, "name_by_user must be a string"),
        ("labels", "solar", "labels must be a list of strings"),
    ],
)
def test_bad_field_type_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: Any, message: str
) -> None:
    item = {"match": by_identifiers(DOOR), field: value}
    fake = FakeHA()
    result = apply(monkeypatch, fake, write(tmp_path / "a", {"devices": [item]}), tmp_path / "s")
    assert result.exit_code == 3, result.output
    assert f"refused: {message}" in flat(result)
    assert fake.writes == []


# --- safety --------------------------------------------------------------


def test_concurrent_change_is_a_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = rebuilt(monkeypatch, tmp_path)
    calls: list[int] = []

    def edit(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 3 writes
            device(fake, PLUG)["area_id"] = "kitchen"

    b.before["config/device_registry/list"] = [edit]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! device Smart plug (mac 02:00:5e:10:00:01) conflict: area_id changed in HA since "
        "the plan (null -> kitchen)" in flat(result)
    )
    assert device(b, PLUG)["area_id"] == "kitchen"
    assert len(device_writes(b)) == 6


def test_verify_mismatch_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = rebuilt(monkeypatch, tmp_path, sensors=False)

    def revert(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg["device_id"] == device(fake, PLUG)["id"]:
            device(fake, PLUG)["area_id"] = None

    b.after[DEVICE_UPDATE] = [revert]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "Verify failed after stage 3; stopping:" in result.output
    assert "device Smart plug (mac 02:00:5e:10:00:01): area_id: expected den, HA has null" in (
        flat(result)
    )
    assert SENSOR_PENDING in flat(result)
    assert AREA_UPDATE not in commands(b)


def test_failed_device_write_stops_later_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a, b = rebuilt(monkeypatch, tmp_path, sensors=False)
    plug = device(b, PLUG)["id"]

    def lose_plug(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg["device_id"] == plug:
            raise HAClientError(f"{DEVICE_UPDATE} failed: Device not found", "not_found")

    b.before[DEVICE_UPDATE] = [lose_plug]
    snap = tmp_path / "snap"
    result = apply(monkeypatch, b, a, snap)
    assert result.exit_code == 1, result.output
    assert "x device Smart plug (mac 02:00:5e:10:00:01) failed: " in flat(result)
    assert "later stages were not run" in result.output
    assert SENSOR_PENDING in flat(result)
    assert commands(b) == [DEVICE_UPDATE] * 7
    records = [json.loads(line) for line in (snap / "journal.jsonl").read_text().splitlines()]
    assert [r["result"] for r in records].count("ok") == 6
    assert device(b, PLUG)["area_id"] is None

    b.before.clear()
    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert commands(b) == [DEVICE_UPDATE, AREA_UPDATE]
    exported = export(monkeypatch, b, tmp_path / "b")
    assert (exported / "devices.yaml").read_bytes() == (a / "devices.yaml").read_bytes()
    b.sent.clear()
    assert apply(monkeypatch, b, a, tmp_path / "snap3").exit_code == 0
    assert b.writes == []


# --- rendering -----------------------------------------------------------


def test_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = [
        {"match": {"connections": [list(PLUG)]}, "area_id": "kitchen", "disabled": True},
        {"match": by_identifiers(RELAY, manufacturer="Acme", model="Relay R4"),
         "name_by_user": "Fridge relay", "labels": ["solar", "critical_load"]},
        {"match": by_identifiers(DOOR), "disabled": True},
        {"match": by_identifiers(CLOUD), "disabled": False},
        {"match": by_identifiers(("zwave_js", "4011223344-99")), "name_by_user": "Lost"},
    ]  # fmt: skip
    fake = FakeHA()
    atlas = write(tmp_path / "a", {"devices": items})
    result = invoke(monkeypatch, fake, "apply", str(atlas), dry_run=True)
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Plan for http://ha.test:8123  (HA 2026.10.4, atlas format 1)\n"
        "Stage 3  devices  (5 in file, 8 live)\n"
        "  ~ device  Smart plug (mac 02:00:5e:10:00:01) area_id: den -> kitchen\n"
        "                                               disabled_by: null -> user\n"
        '  ~ device  Fridge relay (acme_hub hub-7f3a, Relay R4) name_by_user: null -> "Fridge relay"\n'
        "                                                       labels: [] -> [solar, critical_load]\n"
        "  ! device  Weather bridge (acme_cloud acct-91) refused: disabled_by is config_entry in HA; "
        "apply sets or clears only user\n"
        "  ? device  1 unmatched:      zwave_js 1  (-v lists them)\n"
        "  = device  1 unchanged\n"
        "Summary: 0 create, 2 update, 1 refused, 1 unmatched, 1 unchanged. Dry run: nothing written.\n"
    )
    assert fake.writes == []
