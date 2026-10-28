"""``apply`` for floors, labels, areas, devices and entities."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from hass_atlas.atlas import apply_cmd, export_cmd, snapshot as snapshot_mod
from hass_atlas.atlas.planner import is_slug, next_id, normalize_name, simple_slug
from hass_atlas.atlas.yamlio import load_atlas, write_atlas
from hass_atlas.cli import cli
from hass_atlas.context import Context
from hass_atlas.ha_client import HAClientError, HAConnectionLost
from tests.atlas.fake_ha import FakeHA, empty_fixture, load_fixture

URL = "http://ha.test:8123"
FILES = (
    "atlas.yaml",
    "floors.yaml",
    "labels.yaml",
    "areas.yaml",
    "devices.yaml",
    "entities.yaml",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(apply_cmd, "_stdin_is_tty", lambda: False)


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeHA,
    *args: str,
    dry_run: bool = False,
    input: str | None = None,
) -> Result:
    monkeypatch.setattr(Context, "client", lambda self: fake)
    pre = ["--dry-run"] if dry_run else []
    return CliRunner().invoke(cli, ["--url", URL, "--token", "t", *pre, *args], input=input)


def export(monkeypatch: pytest.MonkeyPatch, fake: FakeHA, out: Path) -> Path:
    result = invoke(monkeypatch, fake, "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    return out


def apply(
    monkeypatch: pytest.MonkeyPatch, fake: FakeHA, atlas: Path, snap: Path | None, *args: str
) -> Result:
    extra = ["--snapshot-dir", str(snap)] if snap is not None else []
    return invoke(monkeypatch, fake, "apply", str(atlas), "--yes", *extra, *args)


def write(path: Path, docs: dict[str, list[dict[str, Any]]]) -> Path:
    manifest = {
        "format": 1,
        "hass_atlas": "0.0.0",
        "ha_version": "2026.10.4",
        "domains": [d for d in ("floors", "labels", "areas", "devices", "entities") if d in docs],
    }
    write_atlas(path, docs, manifest)
    return path


def file_docs() -> dict[str, list[dict[str, Any]]]:
    """The small fixture as export writes it."""
    return {
        "floors": [
            {
                "floor_id": "upstairs",
                "name": "Upstairs",
                "level": 1,
                "icon": "mdi:home-floor-1",
                "aliases": ["attic level", "second floor"],
            },
            {"floor_id": "ground", "name": "Ground", "level": 0},
            {"floor_id": "basement", "name": "Cellar"},
        ],
        "labels": [
            {
                "label_id": "critical_load",
                "name": "Critical load",
                "color": "red",
                "icon": "mdi:flash-alert",
                "description": "On the backup subpanel",
            },
            {"label_id": "solar", "name": "Solar", "color": "amber", "icon": "mdi:solar-power"},
            {"label_id": "unused", "name": "Unused"},
        ],
        "areas": [
            {
                "area_id": "garage_2",
                "name": "Workshop",
                "floor_id": "ground",
                "icon": "mdi:tools",
                "labels": ["critical_load", "solar"],
                "aliases": ["bench", "shop"],
                "temperature_entity_id": "sensor.workshop_temperature",
                "humidity_entity_id": "sensor.workshop_humidity",
            },
            {"area_id": "den", "name": "Den", "floor_id": "upstairs"},
            {"area_id": "den_2", "name": "Library", "floor_id": "upstairs"},
            {
                "area_id": "cafe",
                "name": "Café",
                "icon": "mdi:coffee",
                "aliases": ["coffee: corner"],
            },
            {"area_id": "kitchen", "name": "Kitchen"},
        ],
    }


def flat(result: Result) -> str:
    """The output with Rich's line wrapping undone."""
    return " ".join(result.output.split())


def commands(fake: FakeHA) -> list[str]:
    return [cmd for cmd, _ in fake.writes]


def area(fake: FakeHA, area_id: str) -> dict[str, Any]:
    row = fake.row("areas", area_id)
    assert row is not None, area_id
    return row


# --- names -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Garage", "garage"),
        ("Critical load", "critical_load"),
        ("  Two  spaces ", "two_spaces"),
        ("a - b", "a_b"),
        ("a_-b", "a_b"),
        ("-x-", "x"),
        ("Floor 2", "floor_2"),
        ("Café", None),
        ("Don's room", None),
        ("1,000", None),
        ("---", None),
        ("", None),
    ],
)
def test_simple_slug(name: str, slug: str | None) -> None:
    assert simple_slug(name) == slug


def test_slug_helpers() -> None:
    assert is_slug("garage_2")
    assert not is_slug("garage__2")
    assert not is_slug("_garage")
    assert not is_slug("Garage")
    assert next_id("den", {"den", "den_2"}) == "den_3"
    assert normalize_name("Living Room") == normalize_name("livingroom")
    assert normalize_name("Straße") == normalize_name("STRASSE")


# --- round trip and idempotence ---------------------------------------


def test_rebuild_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    exported = export(monkeypatch, b, tmp_path / "b")
    for name in FILES:
        assert (exported / name).read_bytes() == (a / name).read_bytes(), name


def test_second_apply_sends_only_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    b.sent.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert b.writes == []
    assert "Summary: 0 create, 0 update, 34 unchanged. Nothing to write." in result.output
    assert not (tmp_path / "snap2").exists()


def test_apply_to_source_sends_only_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    fake = FakeHA()
    result = apply(monkeypatch, fake, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert fake.writes == []


# --- dry run, --check and confirmation ---------------------------------


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    snap = tmp_path / "snap"
    result = invoke(monkeypatch, b, "apply", str(a), "--snapshot-dir", str(snap), dry_run=True)
    assert result.exit_code == 0, result.output
    assert b.writes == []
    assert {cmd for cmd, _ in b.sent} == {
        "config/label_registry/list",
        "config/floor_registry/list",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "config/entity_registry/get_entries",
        "get_states",
    }
    assert (
        "Summary: 11 create, 20 update, 3 rename, 3 unchanged. Dry run: nothing written."
        in result.output
    )
    assert not snap.exists()
    assert not (tmp_path / "home").exists()


def test_check_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = invoke(monkeypatch, b, "apply", str(a), "--check", "--yes")
    assert result.exit_code == 2, result.output
    assert b.writes == []
    assert not (tmp_path / "home").exists()

    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    result = invoke(monkeypatch, b, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output

    docs = file_docs()
    docs["areas"][1]["floor_id"] = "attic"
    refused = write(tmp_path / "refused", docs)
    sent = len(b.writes)
    result = invoke(monkeypatch, b, "apply", str(refused), "--check")
    assert result.exit_code == 3, result.output
    assert len(b.writes) == sent


def test_no_tty_without_yes_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = invoke(monkeypatch, b, "apply", str(a), "--snapshot-dir", str(tmp_path / "snap"))
    assert result.exit_code == 1
    assert "pass --yes" in flat(result)
    assert b.writes == []
    assert not (tmp_path / "snap").exists()


@pytest.mark.parametrize(("answer", "code"), [("y\n", 0), ("n\n", 1), ("\n", 1)])
def test_tty_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: str, code: int
) -> None:
    monkeypatch.setattr(apply_cmd, "_stdin_is_tty", lambda: True)
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    snap = tmp_path / "snap"
    result = invoke(monkeypatch, b, "apply", str(a), "--snapshot-dir", str(snap), input=answer)
    assert result.exit_code == code, result.output
    assert result.output.count(f"Apply 34 changes to {URL}? [y/N]") == 1
    assert bool(b.writes) == (code == 0)
    assert snap.exists() == (code == 0)


def test_empty_plan_needs_no_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    result = invoke(monkeypatch, FakeHA(), "apply", str(a))
    assert result.exit_code == 0, result.output


# --- input checks ------------------------------------------------------


def test_unknown_format_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    (a / "atlas.yaml").write_text("format: 2\ndomains: []\n", encoding="utf-8")
    fake = FakeHA()
    result = apply(monkeypatch, fake, a, None)
    assert result.exit_code == 1
    assert "unsupported format 2" in result.output
    assert fake.sent == []


def test_version_mismatch_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    fake = FakeHA()
    fake.ha_version = "2026.11.0"
    result = invoke(monkeypatch, fake, "apply", str(a), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "from Home Assistant 2026.10.4; this one runs 2026.11.0" in flat(result)
    fake.ha_version = "2026.10.9"
    result = invoke(monkeypatch, fake, "apply", str(a), dry_run=True)
    assert "WARN" not in result.output


def test_only_must_be_in_atlas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    atlas = write(tmp_path / "a", {"labels": file_docs()["labels"]})
    fake = FakeHA()
    result = apply(monkeypatch, fake, atlas, None, "--only", "areas")
    assert result.exit_code == 2
    assert "areas not in the atlas" in result.output
    assert fake.sent == []


def test_only_reads_references_but_writes_only_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["floors"] = [copy.deepcopy(load_fixture()["floors"][1])]  # ground only
    data["labels"] = copy.deepcopy(load_fixture()["labels"])
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "areas")
    assert result.exit_code == 3, result.output
    assert set(commands(b)) == {"config/area_registry/create", "config/area_registry/update"}
    assert (
        "floor_id upstairs: floor upstairs is not in HA, and --only leaves out floors"
        in result.output
    )
    assert b.row("areas", "den") is None
    assert area(b, "garage_2")["floor_id"] == "ground"


# --- identity ----------------------------------------------------------


def test_slug_then_rename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    floors = [(cmd, msg) for cmd, msg in b.writes if cmd.startswith("config/floor_registry")]
    assert floors[-2:] == [
        ("config/floor_registry/create", {"name": "basement"}),
        ("config/floor_registry/update", {"floor_id": "basement", "name": "Cellar"}),
    ]
    assert ("config/area_registry/create", {"name": "Den", "floor_id": "upstairs"}) in b.writes
    assert ("config/area_registry/update", {"area_id": "cafe", "name": "Café"}) in b.writes
    assert [row["area_id"] for row in b.registries["areas"]] == [
        "garage_2",
        "den",
        "den_2",
        "cafe",
        "kitchen",
    ]
    assert '+ floor   basement          create as "basement", rename -> "Cellar"' in result.output


def test_temp_name_avoids_taken_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = empty_fixture()
    data["floors"] = [
        {"floor_id": "other", "name": "BASEMENT", "level": None, "icon": None, "aliases": []}
    ]
    b = FakeHA(data)
    atlas = write(tmp_path / "a", {"floors": [{"floor_id": "basement_2", "name": "Cellar"}]})
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.writes[0] == ("config/floor_registry/create", {"name": "basement_2"})
    assert b.row("floors", "basement_2") is not None


def test_id_taken_during_run_is_mapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def intrude(registry: str, row: dict[str, Any], name: str) -> Any:
        def hook(fake: FakeHA, msg: dict[str, Any]) -> None:
            if msg.get("name") == name and fake.row(registry, next(iter(row.values()))) is None:
                fake.registries[registry].append(dict(row))

        return hook

    b.before["config/floor_registry/create"] = [
        intrude("floors", {"floor_id": "ground", "name": "Lobby", "aliases": []}, "Ground")
    ]
    b.before["config/label_registry/create"] = [
        intrude("labels", {"label_id": "solar", "name": "Sun"}, "Solar")
    ]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.row("floors", "ground_2") is not None
    assert b.row("floors", "ground_2")["name"] == "Ground"  # type: ignore[index]
    garage = area(b, "garage_2")
    assert garage["floor_id"] == "ground_2"
    assert garage["labels"] == ["critical_load", "solar_2"]
    assert (
        '+ floor   ground            create name="Ground" level=0  (HA assigned ground_2; later references use it)'
        in result.output
    )
    assert "floor_id=ground_2" in result.output.split("Stage 2  areas\n", 1)[1]
    journal = (tmp_path / "snap" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    created = [json.loads(line) for line in journal if '"key": "ground"' in line]
    assert created[0]["live_id"] == "ground_2"


def test_name_fallback_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["floors"] = [
        {"floor_id": "ground_floor", "name": "ground", "level": 0, "icon": None, "aliases": []}
    ]
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.row("floors", "ground") is None
    assert b.row("floors", "ground_floor")["name"] == "Ground"  # type: ignore[index]
    assert area(b, "garage_2")["floor_id"] == "ground_floor"
    assert (
        '~ floor   ground            name: "ground" -> "Ground"  (matched live ground_floor by name)'
        in result.output
    )


def test_name_fallback_onto_bound_item_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["labels"] = [
        {
            "label_id": "critical_load",
            "name": "Solar",
            "color": None,
            "icon": None,
            "description": None,
        }
    ]
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! label   solar             refused: name matches live critical_load, which is matched to critical_load"
        in result.output
    )
    assert (
        "! area    garage_2          refused: labels solar: label solar is refused"
        in result.output
    )
    assert not any(msg.get("name") == "Solar" for _, msg in b.writes)
    assert b.row("areas", "garage_2") is None
    assert b.row("areas", "den") is not None


def test_two_name_candidates_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = empty_fixture()
    data["floors"] = [
        {"floor_id": "g1", "name": "Ground", "level": None, "icon": None, "aliases": []},
        {"floor_id": "g2", "name": "GROUND", "level": None, "icon": None, "aliases": []},
    ]
    b = FakeHA(data)
    atlas = write(
        tmp_path / "a",
        {
            "floors": [{"floor_id": "ground", "name": "Ground", "level": 0}],
            "labels": [],
            "areas": [{"area_id": "hall", "name": "Hall", "floor_id": "ground"}],
        },
    )
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "refused: name matches live g1, g2" in result.output
    assert "refused: floor_id ground: floor ground is refused" in result.output
    assert b.writes == []
    assert not (tmp_path / "snap").exists()


# --- field semantics ---------------------------------------------------


def _with_den(**fields: Any) -> FakeHA:
    data = load_fixture()
    data["areas"][1].update(fields)
    return FakeHA(data)


def test_null_clears(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _with_den(icon="mdi:sofa", aliases=["lounge"], floor_id="ground")
    docs = file_docs()
    docs["areas"][1].update(icon=None, aliases=None, floor_id=None)
    atlas = write(tmp_path / "a", docs)
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.writes == [
        (
            "config/area_registry/update",
            {"area_id": "den", "floor_id": None, "icon": None, "aliases": []},
        )
    ]
    den = area(b, "den")
    assert (den["icon"], den["aliases"], den["floor_id"]) == (None, [], None)


def test_absent_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _with_den(icon="mdi:sofa", aliases=["lounge"])
    docs = file_docs()
    docs["areas"][1]["name"] = "Den room"
    atlas = write(tmp_path / "a", docs)
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.writes == [("config/area_registry/update", {"area_id": "den", "name": "Den room"})]
    den = area(b, "den")
    assert (den["icon"], den["aliases"]) == ("mdi:sofa", ["lounge"])


def test_live_items_not_in_file_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    atlas = write(tmp_path / "a", {"floors": [], "labels": [], "areas": []})
    fake = FakeHA()
    result = apply(monkeypatch, fake, atlas, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert fake.writes == []


def test_unknown_reference_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    docs = file_docs()
    docs["areas"][1]["floor_id"] = "attic"
    docs["areas"][4]["labels"] = ["nope"]
    atlas = write(tmp_path / "a", docs)
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! area    den               refused: floor_id attic: no floor attic in the file or in HA"
        in result.output
    )
    assert (
        "! area    kitchen           refused: labels nope: no label nope in the file or in HA"
        in result.output
    )
    assert b.row("areas", "den") is None
    assert b.row("areas", "kitchen") is None
    assert b.row("areas", "den_2") is not None


def test_bad_field_type_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    docs = file_docs()
    docs["floors"][1]["level"] = "zero"
    docs["labels"][0]["name"] = None
    atlas = write(tmp_path / "a", docs)
    result = apply(monkeypatch, FakeHA(), atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "refused: level must be a whole number" in result.output
    assert "refused: name cannot be cleared" in result.output


def test_area_sensor_must_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["states"] = [s for s in data["states"] if s["entity_id"] != "sensor.workshop_humidity"]
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "refused: humidity_entity_id sensor.workshop_humidity: entity does not exist"
        in result.output
    )
    assert area(b, "garage_2")["temperature_entity_id"] is None

    data = empty_fixture()
    data["states"][1]["attributes"]["device_class"] = "moisture"
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 3, result.output
    assert "humidity_entity_id sensor.workshop_humidity: not a humidity sensor" in result.output


def test_area_sensors_written_after_areas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    assert b.writes[-1] == (
        "config/area_registry/update",
        {
            "area_id": "garage_2",
            "temperature_entity_id": "sensor.workshop_temperature",
            "humidity_entity_id": "sensor.workshop_humidity",
        },
    )
    create = next(msg for cmd, msg in b.writes if msg.get("name") == "garage_2")
    assert "temperature_entity_id" not in create


# --- safety ------------------------------------------------------------


def test_concurrent_change_is_a_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _with_den(floor_id="ground")
    calls: list[int] = []

    def edit(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 2 writes
            area(fake, "den")["floor_id"] = "basement"

    b.before["config/area_registry/list"] = [edit]
    a = write(tmp_path / "a", file_docs())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert b.writes == []
    assert area(b, "den")["floor_id"] == "basement"
    assert (
        "! area    den               conflict: floor_id changed in HA since the plan (ground -> basement)"
        in result.output
    )


def test_failed_write_stops_later_stages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    b.fail["config/label_registry/create"] = HAClientError(
        "config/label_registry/create failed: Unknown error", code="unknown_error"
    )
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert (
        "x label   solar             failed: config/label_registry/create failed: Unknown error"
        in result.output
    )
    assert "later stages were not run" in result.output
    assert [row["floor_id"] for row in b.registries["floors"]] == [
        "upstairs",
        "ground",
        "basement",
    ]
    assert b.registries["areas"] == []
    assert not any(cmd.startswith("config/area_registry") for cmd in commands(b))
    journal = [
        json.loads(line)
        for line in (tmp_path / "snap" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failed = [r for r in journal if r["result"] != "ok"]
    assert len(failed) == 3
    assert failed[0]["result"] == {
        "error": "config/label_registry/create failed: Unknown error",
        "code": "unknown_error",
    }

    b.fail.clear()
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    exported = export(monkeypatch, b, tmp_path / "b")
    for name in FILES:
        assert (exported / name).read_bytes() == (a / name).read_bytes(), name


def test_failed_rename_converges_on_rerun(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = FakeHA(empty_fixture())
    b.fail["config/floor_registry/update"] = HAClientError(
        "config/floor_registry/update failed: Unknown error", code="unknown_error"
    )
    atlas = write(tmp_path / "a", {"floors": [{"floor_id": "attic", "name": "Loft"}]})
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert b.row("floors", "attic")["name"] == "attic"  # type: ignore[index]
    journal = (tmp_path / "snap" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in journal]
    assert [r["op"] for r in records] == ["create", "rename"]
    assert records[1]["before"] == {"name": "attic"}
    assert records[1]["result"]["code"] == "unknown_error"

    b.fail.clear()
    result = apply(monkeypatch, b, atlas, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert b.writes[-1] == ("config/floor_registry/update", {"floor_id": "attic", "name": "Loft"})
    assert b.row("floors", "attic")["name"] == "Loft"  # type: ignore[index]


def test_verify_mismatch_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    data["areas"][1]["floor_id"] = "ground"
    data["areas"][0]["humidity_entity_id"] = None
    b = FakeHA(data)

    def revert(fake: FakeHA, msg: dict[str, Any]) -> None:
        if msg.get("area_id") == "den":
            area(fake, "den")["floor_id"] = "ground"

    b.after["config/area_registry/update"] = [revert]
    a = write(tmp_path / "a", file_docs())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "Verify failed after stage 2; stopping:" in result.output
    assert "area den: floor_id: expected upstairs, HA has ground" in result.output
    assert commands(b) == ["config/area_registry/update"]
    assert area(b, "garage_2")["humidity_entity_id"] is None


# --- snapshot ----------------------------------------------------------


def test_snapshot_contents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    data["areas"][1]["floor_id"] = "ground"
    data["labels"] = data["labels"][:2]
    b = FakeHA(data)
    a = write(tmp_path / "a", file_docs())

    inodes: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        inodes.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(snapshot_mod.os, "fsync", fsync)
    snap = tmp_path / "snap"
    result = apply(monkeypatch, b, a, snap)
    assert result.exit_code == 0, result.output
    assert f"Snapshot and journal: {snap}" in result.output.replace("\n", "")

    assert snap.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in snap.iterdir()) == ["before", "journal.jsonl", "plan.json"]
    before = load_atlas(snap / "before")
    assert before.manifest["domains"] == ["labels", "areas"]
    assert [i["label_id"] for i in before.docs["labels"]] == ["critical_load", "solar"]
    assert before.docs["areas"][1] == {"area_id": "den", "name": "Den", "floor_id": "ground"}

    plan = json.loads((snap / "plan.json").read_text(encoding="utf-8"))
    assert plan["url"] == URL
    writes = [c for c in plan["changes"] if c["op"] in ("create", "update")]
    assert [(c["domain"], c["key"], c["op"]) for c in writes] == [
        ("labels", "unused", "create"),
        ("areas", "den", "update"),
    ]
    assert writes[1]["before"] == {"floor_id": "ground"}

    journal_path = snap / "journal.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records == [
        {
            "stage": 1,
            "domain": "labels",
            "op": "create",
            "key": "unused",
            "payload": {"name": "Unused"},
            "before": None,
            "result": "ok",
            "live_id": "unused",
        },
        {
            "stage": 2,
            "domain": "areas",
            "op": "update",
            "key": "den",
            "payload": {"area_id": "den", "floor_id": "upstairs"},
            "before": {"floor_id": "ground"},
            "result": "ok",
        },
    ]
    assert inodes.count(journal_path.stat().st_ino) == len(lines)


def test_default_snapshot_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(2026, 10, 28, 12, 0, 5, tzinfo=tz)

    monkeypatch.setattr(snapshot_mod, "datetime", Clock)
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    root = tmp_path / "home" / ".local" / "state" / "hass-atlas" / "snapshots" / "ha.test"
    first = root / "20261028T120005Z"
    assert apply(monkeypatch, FakeHA(empty_fixture()), a, None).exit_code == 0
    journal = (first / "journal.jsonl").read_bytes()
    assert apply(monkeypatch, FakeHA(empty_fixture()), a, None).exit_code == 0
    assert sorted(root.iterdir()) == [first, root / "20261028T120005Z-2"]
    assert (first / "journal.jsonl").read_bytes() == journal
    for path in root.iterdir():
        assert path.stat().st_mode & 0o777 == 0o700
        assert (path / "journal.jsonl").is_file()


def test_snapshot_dir_must_be_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "old").write_text("x", encoding="utf-8")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, snap)
    assert result.exit_code == 1
    assert f"snapshot directory {snap} exists and is not empty" in result.output
    assert b.writes == []


def test_snapshot_failure_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device", "plan.json")

    monkeypatch.setattr(snapshot_mod, "_write_synced", fail)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1
    assert "cannot write snapshot" in result.output
    assert "No space left on device" in result.output
    assert b.writes == []


def test_journal_failure_stops_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())

    def append(self: snapshot_mod.Journal, record: dict[str, Any]) -> None:
        raise snapshot_mod.SnapshotError(f"cannot write {self.path}: disk full")

    monkeypatch.setattr(snapshot_mod.Journal, "append", append)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 1
    assert "disk full" in result.output
    assert len(b.writes) == 1


# --- rendering ---------------------------------------------------------


GOLDEN_PLAN = """\
Plan for http://ha.test:8123  (HA 2026.10.4, atlas format 1)
Stage 1  labels  (3 in file, 1 live)
  + label   solar             create name="Solar" color=amber icon=mdi:solar-power
  + label   unused            create name="Unused"
  = label   1 unchanged
Stage 1  floors  (3 in file, 2 live)
  = floor   ground            unchanged  (matched live ground_floor by name)
  + floor   basement          create as "basement", rename -> "Cellar"
  = floor   1 unchanged
Stage 2  areas  (5 in file, 3 live)
  + area    garage_2          create as "garage_2", rename -> "Workshop" floor_id=ground_floor icon=mdi:tools labels=[critical_load, solar] aliases=[bench, shop]
  ~ area    den               name: "den" -> "Den"
                              floor_id: null -> upstairs
  ! area    cafe              refused: floor_id attic: no floor attic in the file or in HA
  ~ area    kitchen           icon: mdi:stove -> null
  = area    1 unchanged
Stage 5  area sensors  (1 in file)
  ~ area    garage_2          temperature_entity_id: null -> sensor.workshop_temperature
                              humidity_entity_id: null -> sensor.workshop_humidity
Summary: 4 create, 3 update, 1 refused, 4 unchanged. Dry run: nothing written.
"""


def test_render_golden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = load_fixture()
    data = empty_fixture()
    data["labels"] = [source["labels"][1]]
    data["floors"] = [
        source["floors"][0],
        {"floor_id": "ground_floor", "name": "Ground", "level": 0, "icon": None, "aliases": []},
    ]
    den = dict(source["areas"][1], floor_id=None, name="den")
    kitchen = dict(source["areas"][4], icon="mdi:stove")
    data["areas"] = [den, source["areas"][2], kitchen]
    docs = file_docs()
    docs["areas"][3]["floor_id"] = "attic"
    docs["areas"][4]["icon"] = None
    atlas = write(tmp_path / "a", docs)
    fake = FakeHA(data)
    result = invoke(monkeypatch, fake, "apply", str(atlas), dry_run=True)
    assert result.exit_code == 0, result.output
    assert result.output == GOLDEN_PLAN
    assert fake.writes == []


def test_verbose_lists_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    result = invoke(monkeypatch, FakeHA(), "apply", str(a), "-v", dry_run=True)
    assert result.exit_code == 0, result.output
    assert "  = floor   basement          unchanged\n" in result.output
    assert "  = area    kitchen           unchanged\n" in result.output
    assert "unchanged\n  = area    1 unchanged" not in result.output


def test_progress_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    run = result.output.split("Summary: 11 create, 20 update, 3 rename, 3 unchanged.\n", 1)[1]
    assert run.startswith("Stage 1  labels, floors\n  + label   critical_load")
    assert "Stage 3  devices\n  ~ device  Smart plug (mac 02:00:5e:10:00:01)" in run
    assert "Stage 5  area sensors\n  ~ area    garage_2" in run
    assert "Summary: 11 created, 20 updated, 3 renamed, 3 unchanged." in run


# --- races, lost connections and restore --------------------------------


def _on_call(fake: FakeHA, command: str, number: int, action: Any) -> None:
    """Run *action(fake)* before the *number*-th *command* the fake receives."""
    calls: list[int] = []

    def hook(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == number:
            action(fake)

    fake.before.setdefault(command, []).append(hook)


def _row(registry: str, **fields: Any) -> dict[str, Any]:
    base = {"floors": load_fixture()["floors"][0], "areas": load_fixture()["areas"][4]}
    row = {k: None for k in base[registry]}
    row.update(aliases=[], **({"labels": []} if registry == "areas" else {}))
    row.update(fields)
    return row


def test_create_conflict_skips_its_area_sensors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = FakeHA(empty_fixture())
    foreign = _row("areas", area_id="office", name="Someone else")
    _on_call(b, "config/area_registry/list", 2, lambda f: f.registries["areas"].append(foreign))
    office = {
        "area_id": "office",
        "name": "Office",
        "temperature_entity_id": "sensor.workshop_temperature",
    }
    atlas = write(tmp_path / "a", {"areas": [office]})
    result = apply(monkeypatch, b, atlas, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "conflict: office was created in HA since the plan" in result.output
    assert "  ! area    office            skipped: area office was not created" in result.output
    assert "Summary: 0 created, 0 updated, 1 conflict, 1 skipped, 0 unchanged." in result.output
    assert b.writes == []
    assert area(b, "office")["temperature_entity_id"] is None


def test_floor_conflict_skips_area_and_its_sensors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    ground = _row("floors", floor_id="ground", name="Ground floor")
    _on_call(b, "config/floor_registry/list", 2, lambda f: f.registries["floors"].append(ground))
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "! floor   ground            conflict: ground was created in HA since the plan" in (
        result.output
    )
    assert "! area    garage_2          skipped: floor ground was not created" in result.output
    assert "! area    garage_2          skipped: area garage_2 was not created" in result.output
    assert "skipped: area garage_2 was not created" in result.output.split("Stage 3")[-1]
    assert "removed from HA" not in result.output
    assert "1 conflict, 4 skipped" in result.output
    assert "! entity  switch.pump_relay skipped: area garage_2 was not created" in result.output
    assert b.row("areas", "garage_2") is None


def test_update_target_removed_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    b = _with_den(floor_id="ground")

    def remove(fake: FakeHA) -> None:
        fake.registries["areas"].remove(area(fake, "den"))

    _on_call(b, "config/area_registry/list", 2, remove)
    result = apply(monkeypatch, b, write(tmp_path / "a", file_docs()), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "! area    den               conflict: den was removed from HA since the plan" in (
        result.output
    )
    assert b.writes == []


def _journal(snap: Path) -> list[dict[str, Any]]:
    lines = (snap / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_lost_connection_is_unknown_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    lost = HAConnectionLost("Command 'config/label_registry/create' timed out after 30s")

    def drop(fake: FakeHA, msg: dict[str, Any]) -> None:
        raise lost

    b.after["config/label_registry/create"] = [drop]
    snap = tmp_path / "snap"
    result = apply(monkeypatch, b, a, snap)
    assert result.exit_code == 1, result.output
    assert f"? label   critical_load     unknown: {lost}" in result.output
    assert "Summary: 0 created, 0 updated, 1 unknown, 3 unchanged." in result.output
    assert f"Snapshot and journal: {snap}" in result.output.replace("\n", "")
    assert commands(b) == ["config/label_registry/create"]
    assert b.row("labels", "critical_load") is not None
    [record] = _journal(snap)
    assert record["result"] == {"unknown": str(lost)}


def test_read_failure_after_writes_shows_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    lost = HAConnectionLost("Connection lost during 'config/floor_registry/list'")

    def fail(fake: FakeHA) -> None:
        raise lost

    _on_call(b, "config/floor_registry/list", 3, fail)  # the verify read after stage 1
    snap = tmp_path / "snap"
    result = apply(monkeypatch, b, a, snap)
    assert result.exit_code == 1, result.output
    assert str(lost) in result.output
    assert "Summary: 6 created, 0 updated, 3 unchanged." in result.output
    assert f"Snapshot and journal: {snap}" in result.output.replace("\n", "")
    assert len(_journal(snap)) == len(b.writes) == 7


def test_label_icon_and_color_format_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docs = file_docs()
    docs["labels"][1]["icon"] = "flash"
    docs["labels"][2]["color"] = "Red"
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, write(tmp_path / "a", docs), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert '! label   solar             refused: icon must have the form "prefix:name"' in (
        result.output
    )
    assert (
        "! label   unused            refused: color must be #RRGGBB or a theme color such as "
        "red or light-blue" in result.output
    )
    assert [row["label_id"] for row in b.registries["labels"]] == ["critical_load"]


def test_apply_snapshot_before_restores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = FakeHA()
    original = export(monkeypatch, b, tmp_path / "orig")
    docs = file_docs()
    docs["areas"][1].update(name="Study", icon="mdi:desk")
    docs["floors"][1]["level"] = 2
    docs["labels"][0]["color"] = "blue"
    assert apply(monkeypatch, b, write(tmp_path / "a", docs), tmp_path / "snap").exit_code == 0

    result = apply(monkeypatch, b, tmp_path / "snap" / "before", tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    restored = load_atlas(export(monkeypatch, b, tmp_path / "restored")).docs
    # den had no icon, and restore leaves alone a field that was empty.
    assert restored["areas"][1].pop("icon") == "mdi:desk"
    assert restored == load_atlas(original).docs
