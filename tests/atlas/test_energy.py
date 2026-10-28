"""``export`` and ``apply`` for the Energy Dashboard prefs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from hass_atlas.atlas import apply_cmd, energy_stage, export_cmd
from hass_atlas.atlas.domains.energy import normalize
from hass_atlas.atlas.energy_stage import plan_energy
from hass_atlas.atlas.yamlio import AtlasFormatError, load_atlas
from hass_atlas.energy_prefs import UNIFIED
from hass_atlas.ha_client import HAClientError
from tests._ha_energy_schema import (
    required_key_errors,
    validate_legacy_flows_with_ha,
    validate_with_ha,
)
from tests.atlas.fake_ha import (
    ENERGY_GET,
    ENERGY_SAVE,
    FIXTURES,
    FakeHA,
    empty_fixture,
    load_fixture,
)
from tests.atlas.test_apply import apply, export, invoke, write

GOLDEN = FIXTURES / "golden" / "energy.yaml"
FORECAST_ID = "01KBFAKEF0RECAST0000000001"
LEGACY_VERSION = "2026.2.3"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(export_cmd, "_package_version", lambda: "0.0.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(apply_cmd, "_stdin_is_tty", lambda: False)


def file_doc() -> dict[str, Any]:
    """The fixture's energy.yaml as loaded."""
    doc: dict[str, Any] = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return doc


def legacy_doc() -> dict[str, Any]:
    """The fixture's energy.yaml without the source fields HA 2026.2 lacks."""
    doc = file_doc()
    for source in doc["energy_sources"]:
        for name in ("name", "stat_soc", "capacity"):
            source.pop(name, None)
    return doc


def atlas(tmp_path: Path, doc: dict[str, Any], name: str = "a") -> Path:
    return write(tmp_path / name, {"energy": [doc]})


def with_energy(energy: dict[str, Any] | None, data: dict[str, Any] | None = None) -> FakeHA:
    data = data if data is not None else load_fixture()
    data["energy"] = copy.deepcopy(energy)
    return FakeHA(data)


def saves(fake: FakeHA) -> list[dict[str, Any]]:
    return [msg for cmd, msg in fake.writes if cmd == ENERGY_SAVE]


def energy_yaml(monkeypatch: pytest.MonkeyPatch, fake: FakeHA, out: Path) -> str:
    result = invoke(monkeypatch, fake, "export", "--out", str(out), "--only", "energy")
    assert result.exit_code == 0, result.output
    return (out / "energy.yaml").read_text(encoding="utf-8")


def flat(output: str) -> str:
    return " ".join(output.split())


# --- export ------------------------------------------------------------


def test_export_only_energy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    out = tmp_path / "atlas"
    text = energy_yaml(monkeypatch, fake, out)
    assert text == GOLDEN.read_text(encoding="utf-8")
    assert sorted(p.name for p in out.iterdir()) == ["atlas.yaml", "energy.yaml"]
    assert [cmd for cmd, _ in fake.sent] == [ENERGY_GET]
    assert load_atlas(out).manifest["domains"] == ["energy"]


def test_export_reports_entry_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result = invoke(monkeypatch, FakeHA(), "export", "--out", str(tmp_path / "a"))
    assert "energy (11)" in flat(result.output)


def test_never_saved_exports_empty_lists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = energy_yaml(monkeypatch, with_energy(None), tmp_path / "atlas")
    assert text == "energy_sources: []\ndevice_consumption: []\ndevice_consumption_water: []\n"


def test_forecast_dropped_with_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, FakeHA(), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert (
        "WARN solar source sensor.solar_energy: config_entry_solar_forecast is not exported"
        in flat(result.output)
    )
    for path in out.iterdir():
        assert FORECAST_ID.encode() not in path.read_bytes(), path.name


def test_derived_and_null_fields_not_exported() -> None:
    doc = file_doc()
    grid, _, battery, water, _ = doc["energy_sources"]
    assert "stat_rate" not in battery and "power_config" in battery
    assert water == {"type": "water", "stat_energy_from": "sensor.water_meter"}
    assert None not in grid.values()


def test_legacy_grid_exported_unified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = load_fixture()
    data["ha_version"] = LEGACY_VERSION
    fake = with_energy(data["energy_legacy"], data)
    assert yaml.safe_load(energy_yaml(monkeypatch, fake, tmp_path / "atlas")) == legacy_doc()


def test_export_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    for entries in energy.values():
        for i, entry in enumerate(entries):
            entries[i] = dict(reversed(entry.items()))
    first = energy_yaml(monkeypatch, FakeHA(), tmp_path / "a")
    assert energy_yaml(monkeypatch, with_energy(energy), tmp_path / "b") == first
    assert energy_yaml(monkeypatch, FakeHA(), tmp_path / "a") == first


def test_unknown_live_field_warned_and_left_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    energy["device_consumption"][0]["color"] = "red"
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, with_energy(energy), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "WARN device_consumption sensor.main_panel_energy: color is not exported" in flat(
        result.output
    )
    assert (out / "energy.yaml").read_bytes() == GOLDEN.read_bytes()


@pytest.mark.parametrize(
    ("list_name", "extra", "where"),
    [
        (
            "energy_sources",
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
            "solar source sensor.solar_energy",
        ),
        (
            "energy_sources",
            {
                "type": "battery",
                "stat_energy_from": "sensor.battery_discharge",
                "stat_energy_to": "sensor.battery_charge",
            },
            "battery source sensor.battery_discharge / sensor.battery_charge",
        ),
        (
            "device_consumption",
            {"stat_consumption": "sensor.pump_power"},
            "device_consumption sensor.pump_power",
        ),
    ],
)
def test_duplicate_live_key_exported_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, list_name: str, extra: Any, where: str
) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    energy[list_name].append(extra)
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, with_energy(energy), "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert f"WARN {where}: a second entry with this key is not exported" in flat(result.output)
    assert (out / "energy.yaml").read_bytes() == GOLDEN.read_bytes()
    load_atlas(out)


def test_empty_strings_export_as_a_loadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    energy["energy_sources"][3]["name"] = ""
    energy["device_consumption"][2]["stat_rate"] = ""
    energy["device_consumption_water"].append({"stat_consumption": ""})
    fake = with_energy(energy)
    out = tmp_path / "atlas"
    result = invoke(monkeypatch, fake, "export", "--out", str(out))
    assert result.exit_code == 0, result.output
    output = flat(result.output)
    assert "WARN device_consumption sensor.pump_power: stat_rate is empty and is not exported" in (
        output
    )
    assert "not exported: stat_consumption is required" in output
    doc = load_atlas(out).docs["energy"][0]
    assert doc["energy_sources"][3] == {
        "type": "water",
        "stat_energy_from": "sensor.water_meter",
        "name": "",
    }
    assert len(doc["device_consumption_water"]) == 2
    result = invoke(monkeypatch, fake, "apply", str(out), "--check")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- 1\n", "expected a mapping of energy_sources"),
        (
            "device_consumption: []\nenergy_sources: []\ndevice_consumption: []\n",
            "found duplicate key 'device_consumption'",
        ),
        ("energy_sources: []\nsolar: []\n", "unknown lists ['solar']"),
        ("device_consumption: null\n", "device_consumption must be a list"),
        (
            "device_consumption: [{name: x}]\n",
            "device_consumption item 0: stat_consumption is required",
        ),
        (
            "device_consumption: [{stat_consumption: sensor.a}, {stat_consumption: sensor.a}]\n",
            "device_consumption item 1: duplicate entry sensor.a",
        ),
        (
            "device_consumption: [{stat_consumption: sensor.a, colour: red}]\n",
            "unknown fields ['colour']",
        ),
        (
            "device_consumption: [{stat_consumption: null}]\n",
            "stat_consumption cannot be null",
        ),
        ("energy_sources: [{type: wind, stat_energy_from: sensor.a}]\n", "type must be one of"),
        (
            "energy_sources: [{type: grid, flow_from: [], flow_to: [], cost_adjustment_day: 0}]\n",
            "unified form",
        ),
        (
            "energy_sources: [{type: solar, stat_energy_from: sensor.a, "
            "config_entry_solar_forecast: [x]}]\n",
            "config_entry_solar_forecast holds config entry ids",
        ),
        ("energy_sources: [{type: grid, cost_adjustment_day: 0}]\n", "a grid source needs"),
        (
            "energy_sources: [{type: battery, stat_energy_from: sensor.a, stat_energy_to: sensor.b, "
            "capacity: 0}]\n",
            "capacity must be greater than 0",
        ),
        (
            "energy_sources: [{type: battery, stat_energy_from: sensor.a, stat_energy_to: sensor.b, "
            "power_config: {stat_rate: sensor.p}, stat_rate: sensor.p}]\n",
            "stat_rate is derived from power_config",
        ),
        (
            "energy_sources: [{type: battery, stat_energy_from: sensor.a, stat_energy_to: sensor.b, "
            "power_config: {stat_rate_from: sensor.p}}]\n",
            "power_config needs both stat_rate_from and stat_rate_to",
        ),
        (
            "energy_sources: [{type: grid, stat_energy_from: sensor.a, cost_adjustment_day: x}]\n",
            "cost_adjustment_day must be a number",
        ),
        (
            "energy_sources: [{type: battery, stat_energy_from: sensor.a}]\n",
            "stat_energy_to is required",
        ),
    ],
)
def test_load_errors(tmp_path: Path, text: str, message: str) -> None:
    path = atlas(tmp_path, {"device_consumption": []})
    (path / "energy.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(AtlasFormatError) as info:
        load_atlas(path)
    assert "energy.yaml" in str(info.value)
    assert message in str(info.value)


def test_load_keeps_lists_the_file_leaves_out(tmp_path: Path) -> None:
    path = atlas(tmp_path, {"device_consumption": []})
    assert load_atlas(path).docs["energy"] == [{"device_consumption": []}]


# --- apply: round trip and idempotence -----------------------------------


def test_rebuild_writes_file_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = atlas(tmp_path, file_doc())
    b = with_energy(None)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    [saved] = saves(b)
    assert list(saved) == ["energy_sources", "device_consumption", "device_consumption_water"]
    assert energy_yaml(monkeypatch, b, tmp_path / "b") == GOLDEN.read_text(encoding="utf-8")
    b.sent.clear()
    result = invoke(monkeypatch, b, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output
    assert "= energy  11 unchanged" in result.output


def test_export_then_check_is_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHA()
    a = export(monkeypatch, fake, tmp_path / "a")
    result = invoke(monkeypatch, fake, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output
    assert fake.writes == []


def test_second_apply_plans_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    energy["device_consumption"][2]["included_in_stat"] = "sensor.main_panel_energy"
    del energy["energy_sources"][3]
    b = with_energy(energy)
    a = atlas(tmp_path, file_doc())
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    assert len(saves(b)) == 1
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    assert len(saves(b)) == 1
    assert "Nothing to write." in result.output


# --- apply: merge and replace ---------------------------------------------


def test_merge_keeps_unmanaged_entries_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = load_fixture()
    unmanaged = source["energy_unmanaged"]
    devices = source["energy"]["device_consumption"]
    live = {
        "energy_sources": [unmanaged["energy_sources"][0], *source["energy"]["energy_sources"]],
        "device_consumption": [devices[0], unmanaged["device_consumption"][0], devices[2]],
        "device_consumption_water": [],
    }
    b = with_energy(live)
    result = apply(monkeypatch, b, atlas(tmp_path, file_doc()), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert "= keep 1 unmanaged device_consumption entry" in result.output
    assert "= keep 1 unmanaged energy_sources entry" in result.output
    assert b.energy is not None
    assert [d["stat_consumption"] for d in b.energy["device_consumption"]] == [
        "sensor.main_panel_energy",
        "sensor.dehumidifier_energy",
        "sensor.pump_power",
        "sensor.subpanel_energy",
        "sensor.kitchen_power",
    ]
    assert b.energy["energy_sources"][0] == unmanaged["energy_sources"][0]
    [saved] = saves(b)
    assert list(saved) == ["device_consumption", "device_consumption_water"]


def test_replace_sends_file_lists_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = load_fixture()
    live = copy.deepcopy(source["energy"])
    for name, entries in source["energy_unmanaged"].items():
        live[name][:0] = entries
    live["device_consumption"][1]["stat_rate"] = "sensor.panel_power"
    b = with_energy(live)
    a = atlas(tmp_path, file_doc())
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--energy", "replace")
    assert result.exit_code == 0, result.output
    assert "- device_consumption  sensor.dehumidifier_energy  remove" in result.output
    assert "- solar source  sensor.garage_solar_energy  remove" in result.output
    assert (
        "~ device_consumption  sensor.main_panel_energy  stat_rate: sensor.panel_power -> null"
        in result.output
    )
    assert "Summary: 0 created, 1 updated, 3 deleted, 10 unchanged." in result.output
    [saved] = saves(b)
    assert list(saved) == ["energy_sources", "device_consumption", "device_consumption_water"]
    doc = file_doc()
    for name in doc:
        assert [normalize(e) for e in saved[name] if e.get("type") != "solar"] == [
            normalize(e) for e in doc[name] if e.get("type") != "solar"
        ]
    [solar] = [e for e in saved["energy_sources"] if e["type"] == "solar"]
    assert solar["config_entry_solar_forecast"] == [FORECAST_ID]
    result = invoke(monkeypatch, b, "apply", str(a), "--check", "--energy", "replace")
    assert result.exit_code == 0, result.output


def test_replace_reorders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live = copy.deepcopy(load_fixture()["energy"])
    live["device_consumption_water"].reverse()
    b = with_energy(live)
    a = atlas(tmp_path, file_doc())
    result = invoke(monkeypatch, b, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--energy", "replace")
    assert result.exit_code == 0, result.output
    assert "~ device_consumption_water  (order)  reorder to the file's order" in result.output
    assert saves(b) == [{"device_consumption_water": file_doc()["device_consumption_water"]}]


def test_only_changed_lists_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = file_doc()
    doc["device_consumption_water"][0]["name"] = "Garden"
    b = FakeHA()
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert (
        '~ device_consumption_water  sensor.irrigation_water  name: "Irrigation" -> "Garden"'
        in result.output
    )
    [saved] = saves(b)
    assert list(saved) == ["device_consumption_water"]
    assert b.energy is not None
    assert b.energy["energy_sources"] == load_fixture()["energy"]["energy_sources"]


def test_absent_left_alone_and_null_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = file_doc()
    doc["device_consumption"][1] = {
        "stat_consumption": "sensor.subpanel_energy",
        "included_in_stat": None,
    }
    doc["energy_sources"][0]["number_energy_price_export"] = None
    b = FakeHA()
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.energy is not None
    assert b.energy["device_consumption"][1] == {
        "stat_consumption": "sensor.subpanel_energy",
        "stat_rate": "sensor.subpanel_power",
    }
    assert b.energy["energy_sources"][0]["number_energy_price_export"] is None
    assert b.energy["energy_sources"][0]["entity_energy_price"] == "sensor.grid_price"


def test_list_left_out_of_file_is_not_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = load_fixture()["energy"]
    b = with_energy(live)
    doc = {"device_consumption_water": file_doc()["device_consumption_water"]}
    doc["device_consumption_water"][0]["name"] = "Garden"
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap", "--energy", "replace")
    assert result.exit_code == 0, result.output
    assert [list(s) for s in saves(b)] == [["device_consumption_water"]]
    assert b.energy is not None
    for name in ("energy_sources", "device_consumption"):
        assert b.energy[name] == live[name]


def test_grid_side_added_to_matched_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = copy.deepcopy(load_fixture()["energy"])
    live["energy_sources"][0]["stat_energy_to"] = None
    b = with_energy(live)
    result = apply(monkeypatch, b, atlas(tmp_path, file_doc()), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert "stat_energy_to: null -> sensor.grid_export_energy" in result.output
    assert b.energy is not None
    grids = [s for s in b.energy["energy_sources"] if s["type"] == "grid"]
    assert [g["stat_energy_to"] for g in grids] == ["sensor.grid_export_energy"]


def test_grid_with_other_export_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live = copy.deepcopy(load_fixture()["energy"])
    live["energy_sources"][0]["stat_energy_to"] = "sensor.old_export"
    b = with_energy(live)
    result = apply(monkeypatch, b, atlas(tmp_path, file_doc()), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! grid source  sensor.house_energy_in / sensor.grid_export_energy  refused: the live grid "
        "source sensor.house_energy_in / sensor.old_export shares a statistic with it but has "
        "stat_energy_to sensor.old_export" in result.output
    )
    assert saves(b) == []


def test_duplicate_live_keys_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live = copy.deepcopy(load_fixture()["energy"])
    live["device_consumption"].append({"stat_consumption": "sensor.pump_power"})
    doc = file_doc()
    doc["device_consumption"][2]["name"] = "Pump"
    doc["device_consumption_water"][0]["name"] = "Garden"
    b = with_energy(live)
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! device_consumption  sensor.pump_power  refused: 2 live device_consumption entries "
        "have this key" in result.output
    )
    assert [list(s) for s in saves(b)] == [["device_consumption_water"]]


def test_price_conflict_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = file_doc()
    doc["energy_sources"][0]["number_energy_price"] = 0.3
    b = FakeHA()
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "refused: both entity_energy_price and number_energy_price would be set" in result.output
    )
    assert saves(b) == []


def test_new_grid_reusing_a_power_statistic_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = file_doc()
    doc["energy_sources"].append(
        {
            "type": "grid",
            "stat_energy_from": "sensor.solar_energy",
            "stat_rate": "sensor.grid_power",
            "cost_adjustment_day": 0.0,
        }
    )
    b = FakeHA()
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "refused: another grid source holds one of its statistics" in result.output
    assert saves(b) == []


def test_replace_keeps_live_field_unknown_to_hass_atlas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    energy = copy.deepcopy(load_fixture()["energy"])
    energy["energy_sources"][3]["future"] = 1
    fake = with_energy(energy)
    a = export(monkeypatch, fake, tmp_path / "a")
    result = invoke(monkeypatch, fake, "apply", str(a), "--check", "--energy", "replace")
    assert result.exit_code == 0, result.output
    water = {"type": "water", "stat_energy_from": "sensor.water_meter"}
    _, stage = plan_energy(
        {"energy_sources": [{**water, "name": "Water"}]},
        {"energy_sources": [{**water, "future": 1}]},
        mode="replace",
        fmt=UNIFIED,
        resolve=lambda entity_id: entity_id,
    )
    assert stage.payload["energy_sources"][0]["future"] == 1


def test_two_file_grid_entries_claiming_one_source_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = file_doc()
    grid = doc["energy_sources"][0]
    doc["energy_sources"][0] = {"type": "grid", "stat_energy_from": grid["stat_energy_from"]}
    doc["energy_sources"].append({"type": "grid", "stat_energy_to": grid["stat_energy_to"]})
    doc["energy_sources"][0]["name"] = "Import"
    doc["energy_sources"][-1]["name"] = "Export"
    b = FakeHA()
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        flat(result.output).count("refused: another file entry matches the same live entry") == 2
    )
    assert saves(b) == []


def test_update_naming_an_unmatched_file_entity_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    doc = yaml.safe_load((a / "energy.yaml").read_text(encoding="utf-8"))
    doc["device_consumption"][0]["included_in_stat"] = "sensor.kitchen_power"
    (a / "energy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    data = empty_fixture()
    data["entities"] = [e for e in data["entities"] if e["entity_id"] != "sensor.relay_power"]
    data["energy"] = load_fixture()["energy"]
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "energy")
    assert result.exit_code == 3, result.output
    assert (
        "! device_consumption  sensor.main_panel_energy  refused: the file's entity "
        "sensor.kitchen_power matches no live entity" in result.output
    )
    assert b.energy is not None
    assert "included_in_stat" not in b.energy["device_consumption"][0]


def test_external_statistic_with_entity_price_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gas = {
        "type": "gas",
        "stat_energy_from": "acme_gas:meter_total",
        "entity_energy_price": "sensor.grid_price",
    }
    b = with_energy(None)
    result = apply(monkeypatch, b, atlas(tmp_path, {"energy_sources": [gas]}), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "refused: stat_energy_from acme_gas:meter_total is an external statistic; set stat_cost "
        "for its price" in flat(result.output)
    )
    assert saves(b) == []


def test_new_grid_without_cost_adjustment_day_gets_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    grid = {"type": "grid", "stat_energy_from": "sensor.house_energy_in"}
    b = with_energy(None)
    result = apply(monkeypatch, b, atlas(tmp_path, {"energy_sources": [grid]}), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    [saved] = saves(b)
    assert saved["energy_sources"] == [{**grid, "cost_adjustment_day": 0.0}]


def test_replace_swaps_a_grid_source_with_another_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = copy.deepcopy(load_fixture()["energy"])
    live["energy_sources"][0]["stat_energy_to"] = "sensor.old_export"
    b = with_energy(live)
    result = apply(
        monkeypatch, b, atlas(tmp_path, file_doc()), tmp_path / "snap", "--energy", "replace"
    )
    assert result.exit_code == 0, result.output
    assert "- grid source  sensor.house_energy_in / sensor.old_export  remove" in result.output
    assert "+ grid source  sensor.house_energy_in / sensor.grid_export_energy" in result.output
    assert b.energy is not None
    grids = [s for s in b.energy["energy_sources"] if s["type"] == "grid"]
    assert [g["stat_energy_to"] for g in grids] == ["sensor.grid_export_energy"]


def test_update_reusing_another_grids_power_statistic_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    second = {
        "type": "grid",
        "stat_energy_from": "sensor.solar_energy",
        "stat_rate": "sensor.solar_power",
        "cost_adjustment_day": 0.0,
    }
    live = copy.deepcopy(load_fixture()["energy"])
    live["energy_sources"].append(second)
    b = with_energy(live)
    doc = file_doc()
    doc["energy_sources"].append({**second, "stat_rate": "sensor.grid_power"})
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! grid source  sensor.solar_energy / -  refused: another grid source holds one of its "
        "statistics" in result.output
    )
    assert saves(b) == []
    _, stage = plan_energy(doc, live, mode="merge", fmt=UNIFIED, resolve=lambda e: e)
    assert stage.lists == []


# --- apply: entity references ---------------------------------------------


def test_missing_entity_skipped_until_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    state = next(s for s in data["states"] if s["entity_id"] == "sensor.irrigation_water")
    data["states"].remove(state)
    b = with_energy(None, data)
    a = atlas(tmp_path, file_doc())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! device_consumption_water  sensor.irrigation_water  skipped: sensor.irrigation_water "
        "is not in HA; a rerun adds this entry once it is" in result.output
    )
    assert b.energy is not None
    assert [d["stat_consumption"] for d in b.energy["device_consumption_water"]] == [
        "sensor.shower_water"
    ]

    b.states.append(state)
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 0, result.output
    # merge appends a new entry; --energy replace would restore the file's order
    assert [d["stat_consumption"] for d in b.energy["device_consumption_water"]] == [
        "sensor.shower_water",
        "sensor.irrigation_water",
    ]
    result = invoke(monkeypatch, b, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output


def test_matched_entry_with_missing_entity_updated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = load_fixture()
    data["states"] = [s for s in data["states"] if s["entity_id"] != "sensor.irrigation_water"]
    b = with_energy(data["energy"], data)
    doc = file_doc()
    doc["device_consumption_water"][0]["name"] = "Garden"
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert b.energy is not None
    assert b.energy["device_consumption_water"][0]["name"] == "Garden"


def test_external_statistic_not_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = {"energy_sources": [s for s in file_doc()["energy_sources"] if s["type"] == "gas"]}
    b = with_energy(None)
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 0, result.output
    [saved] = saves(b)
    assert [normalize(e) for e in saved["energy_sources"]] == doc["energy_sources"]


def test_gas_and_water_sent_with_every_price_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = file_doc()
    doc["energy_sources"][3]["number_energy_price"] = 2.5
    b = with_energy(None)
    assert apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap").exit_code == 0
    [saved] = saves(b)
    water, gas = saved["energy_sources"][3:]
    assert water == {
        "type": "water",
        "stat_energy_from": "sensor.water_meter",
        "stat_cost": None,
        "entity_energy_price": None,
        "number_energy_price": 2.5,
    }
    assert (gas["entity_energy_price"], gas["number_energy_price"]) == (None, None)


def test_follows_stage_4_rename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = invoke(monkeypatch, b, "apply", str(a), dry_run=True)
    assert "+ device_consumption  sensor.kitchen_power" in result.output
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    [saved] = saves(b)
    ids = [d["stat_consumption"] for d in saved["device_consumption"]]
    assert ids[2:] == ["sensor.pump_power", "sensor.kitchen_power"]
    assert b.row("entities", "sensor.kitchen_power") is not None


def test_follows_live_entity_when_rename_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["states"].append({"entity_id": "sensor.kitchen_power", "state": "1", "attributes": {}})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "entity_id sensor.kitchen_power is held by a state with no registry entry" in result.output
    )
    assert '+ device_consumption  sensor.relay_power  name="Kitchen"' in result.output
    assert b.energy is not None
    assert b.energy["device_consumption"][3]["stat_consumption"] == "sensor.relay_power"


def test_follows_live_entity_with_only_energy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "energy")
    assert result.exit_code == 0, result.output
    assert [cmd for cmd, _ in b.writes] == [ENERGY_SAVE]
    assert b.energy is not None
    ids = [d["stat_consumption"] for d in b.energy["device_consumption"]]
    assert ids[2:] == ["sensor.pump_power_2", "sensor.relay_power"]


def test_unmatched_file_entity_skips_new_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["entities"] = [e for e in data["entities"] if e["entity_id"] != "sensor.relay_power"]
    data["states"].append({"entity_id": "sensor.kitchen_power", "state": "1", "attributes": {}})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap", "--only", "energy")
    assert result.exit_code == 3, result.output
    assert (
        "skipped: the file's entity sensor.kitchen_power matches no live entity" in result.output
    )


def test_two_entries_resolving_to_one_statistic_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    doc = yaml.safe_load((a / "energy.yaml").read_text(encoding="utf-8"))
    doc["device_consumption"].append({"stat_consumption": "sensor.relay_power"})
    (a / "energy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    data = empty_fixture()
    data["states"].append({"entity_id": "sensor.kitchen_power", "state": "1", "attributes": {}})
    b = FakeHA(data)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    output = flat(result.output)
    assert output.count("refused: another file entry resolves to the same statistics") == 2
    assert b.energy is not None
    ids = [d["stat_consumption"] for d in b.energy["device_consumption"]]
    assert "sensor.relay_power" not in ids
    load_atlas(export(monkeypatch, b, tmp_path / "b"))


def test_unsent_list_keeps_the_plans_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    data = empty_fixture()
    data["energy"] = copy.deepcopy(load_fixture()["energy"])
    data["energy"]["device_consumption_water"][0]["name"] = "Other"
    b = FakeHA(data)
    calls: list[int] = []

    def intrude(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 4 writes
            row = copy.deepcopy(fake.registries["entities"][0])
            row.update(entity_id="sensor.kitchen_power", id="intruder", unique_id="intruder")
            fake.registries["entities"].append(row)

    b.before["config/entity_registry/list"] = [intrude]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert "sensor.kitchen_power was registered in HA since the plan" in result.output
    assert "device_consumption  sensor.relay_power" not in result.output
    assert (
        '~ device_consumption_water  sensor.irrigation_water  name: "Other" -> "Irrigation"'
        in result.output
    )
    assert [list(s) for s in saves(b)] == [["device_consumption_water"]]


# --- apply: other Home Assistant versions ---------------------------------


def legacy_fake(energy: dict[str, Any] | None) -> FakeHA:
    data = load_fixture()
    data["ha_version"] = LEGACY_VERSION
    return with_energy(energy, data)


def test_legacy_grid_written_in_legacy_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = copy.deepcopy(load_fixture()["energy_legacy"])
    live["energy_sources"][0]["flow_to"] = []
    b = legacy_fake(live)
    a = atlas(tmp_path, legacy_doc())
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 0, result.output
    assert "stat_energy_to: null -> sensor.grid_export_energy" in result.output
    [saved] = saves(b)
    assert list(saved) == ["energy_sources"]
    grid = saved["energy_sources"][0]
    assert grid["flow_to"] == [
        {"stat_energy_to": "sensor.grid_export_energy", "number_energy_price": 0.05}
    ]
    assert "stat_energy_from" not in grid
    assert required_key_errors(saved, legacy=True) == []
    checked = validate_legacy_flows_with_ha([saved])
    assert checked is None or checked == [None]
    result = invoke(monkeypatch, b, "apply", str(a), "--check")
    assert result.exit_code == 0, result.output


def test_legacy_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = legacy_fake(None)
    a = atlas(tmp_path, legacy_doc())
    assert apply(monkeypatch, b, a, tmp_path / "snap").exit_code == 0
    [saved] = saves(b)
    assert [s.get("flow_from") is not None for s in saved["energy_sources"]][0]
    assert yaml.safe_load(energy_yaml(monkeypatch, b, tmp_path / "b")) == legacy_doc()


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        (
            "2026.4.1",
            [
                ("solar source  sensor.solar_energy", "name needs Home Assistant 2026.6"),
                ("battery source  sensor.battery_discharge / sensor.battery_charge", "stat_soc"),
                ("gas source  acme_gas:meter_total", "name needs Home Assistant 2026.6"),
            ],
        ),
        (
            "2026.7.0",
            [
                (
                    "battery source  sensor.battery_discharge / sensor.battery_charge",
                    "capacity needs Home Assistant 2026.8",
                )
            ],
        ),
    ],
)
def test_fields_the_target_release_lacks_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str, refused: list[tuple[str, str]]
) -> None:
    data = load_fixture()
    data["ha_version"] = version
    b = with_energy(None, data)
    result = apply(monkeypatch, b, atlas(tmp_path, file_doc()), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    for where, reason in refused:
        assert f"! {where}  refused: {reason}" in result.output
    [saved] = saves(b)
    assert len(saved["energy_sources"]) == 5 - len(refused)


def test_legacy_grid_name_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = legacy_fake(copy.deepcopy(load_fixture()["energy_legacy"]))
    doc = legacy_doc()
    doc["energy_sources"][0]["name"] = "Mains"
    a = atlas(tmp_path, doc)
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "refused: name needs Home Assistant 2026.6 or later" in result.output
    assert saves(b) == []


LEGACY_GRIDS = [
    {"type": "grid", "stat_energy_from": "sensor.house_energy_in", "cost_adjustment_day": 0.0},
    {
        "type": "grid",
        "stat_energy_from": "sensor.solar_energy",
        "stat_energy_to": "sensor.grid_export_energy",
        "cost_adjustment_day": 0.0,
    },
]


@pytest.mark.parametrize(
    "grids",
    [
        LEGACY_GRIDS,
        [LEGACY_GRIDS[1], {**LEGACY_GRIDS[0], "cost_adjustment_day": 1.0}],
    ],
    ids=["misaligned-export", "cost-adjustment"],
)
def test_legacy_grid_it_cannot_hold_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, grids: list[dict[str, Any]]
) -> None:
    b = legacy_fake(None)
    a = atlas(tmp_path, {"energy_sources": grids})
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "refused: Home Assistant before 2026.3 keeps one grid source" in flat(result.output)
    [saved] = saves(b)
    assert [normalize(g) for g in energy_stage.energy.live_list(saved, "energy_sources")] == [
        normalize(grids[0])
    ]
    result = apply(monkeypatch, b, a, tmp_path / "snap2")
    assert result.exit_code == 3, result.output
    assert len(saves(b)) == 1


def test_verify_compares_the_unified_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(energy_stage, "_check_legacy", lambda work: None)
    grids = [LEGACY_GRIDS[1], {**LEGACY_GRIDS[0], "cost_adjustment_day": 1.0}]
    b = legacy_fake(None)
    result = apply(monkeypatch, b, atlas(tmp_path, {"energy_sources": grids}), tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "Verify failed after stage 6; stopping:" in result.output
    assert "energy energy_sources item 1: expected" in result.output


# --- apply: safety ------------------------------------------------------


def test_dry_run_and_check_write_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = with_energy(None)
    a = atlas(tmp_path, file_doc())
    snap = tmp_path / "snap"
    result = invoke(monkeypatch, b, "apply", str(a), "--snapshot-dir", str(snap), dry_run=True)
    assert result.exit_code == 0, result.output
    assert "Summary: 11 create, 0 update, 0 unchanged. Dry run: nothing written." in result.output
    result = invoke(monkeypatch, b, "apply", str(a), "--check", "--snapshot-dir", str(snap))
    assert result.exit_code == 2, result.output
    assert b.writes == []
    assert not snap.exists()
    assert not (tmp_path / "home").exists()


def test_snapshot_holds_old_prefs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = file_doc()
    doc["device_consumption"][0]["name"] = "Mains"
    snap = tmp_path / "snap"
    b = FakeHA()
    assert apply(monkeypatch, b, atlas(tmp_path, doc), snap).exit_code == 0
    assert (snap / "before" / "energy.yaml").read_bytes() == GOLDEN.read_bytes()
    [record] = [json.loads(line) for line in (snap / "journal.jsonl").read_text().splitlines()]
    assert (record["stage"], record["domain"], record["op"]) == (6, "energy", "save")
    assert list(record["payload"]) == ["device_consumption"]
    assert record["before"] == {
        "device_consumption": load_fixture()["energy"]["device_consumption"]
    }
    assert record["result"] == "ok"


def test_concurrent_change_is_a_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = file_doc()
    doc["device_consumption"][0]["name"] = "Mains"
    doc["device_consumption_water"][0]["name"] = "Garden"
    b = FakeHA()
    calls: list[int] = []

    def edit(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 6 writes
            assert fake.energy is not None
            fake.energy["device_consumption"][1]["name"] = "Sub"

    b.before[ENERGY_GET] = [edit]
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert (
        "! device_consumption  sensor.main_panel_energy  conflict: device_consumption changed "
        "in HA since the plan" in result.output
    )
    assert [list(s) for s in saves(b)] == [["device_consumption_water"]]
    assert b.energy is not None
    assert b.energy["device_consumption"][0]["name"] == "Main panel"


def test_verify_mismatch_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = file_doc()
    doc["device_consumption"][0]["name"] = "Mains"
    b = FakeHA()

    def revert(fake: FakeHA, msg: dict[str, Any]) -> None:
        assert fake.energy is not None
        fake.energy["device_consumption"][0]["name"] = "Main panel"

    b.after[ENERGY_SAVE] = [revert]
    result = apply(monkeypatch, b, atlas(tmp_path, doc), tmp_path / "snap")
    assert result.exit_code == 1, result.output
    assert "Verify failed after stage 6; stopping:" in result.output
    assert "energy device_consumption item 0: expected" in result.output


def test_failed_save_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = with_energy(None)
    b.fail[ENERGY_SAVE] = HAClientError(f"{ENERGY_SAVE} failed: Unknown error", "unknown_error")
    snap = tmp_path / "snap"
    result = apply(monkeypatch, b, atlas(tmp_path, file_doc()), snap)
    assert result.exit_code == 1, result.output
    assert "x device_consumption  sensor.main_panel_energy  failed: " in result.output
    assert "The write failed in stage 6." in result.output
    [record] = [json.loads(line) for line in (snap / "journal.jsonl").read_text().splitlines()]
    assert record["result"]["code"] == "unknown_error"
    b.fail.clear()
    assert apply(monkeypatch, b, atlas(tmp_path, file_doc(), "a2"), tmp_path / "s2").exit_code == 0


def test_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = load_fixture()
    live = copy.deepcopy(source["energy"])
    live["device_consumption"][2]["included_in_stat"] = "sensor.main_panel_energy"
    live["device_consumption"].insert(0, source["energy_unmanaged"]["device_consumption"][0])
    del live["device_consumption_water"][0]
    b = with_energy(live)
    result = invoke(monkeypatch, b, "apply", str(atlas(tmp_path, file_doc())), dry_run=True)
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Plan for http://ha.test:8123  (HA 2026.10.4, atlas format 1)\n"
        "Stage 6  energy  (11 in file, 11 live)\n"
        "  ~ device_consumption  sensor.pump_power  "
        "included_in_stat: sensor.main_panel_energy -> sensor.subpanel_energy\n"
        '  + device_consumption_water  sensor.irrigation_water  name="Irrigation"\n'
        "  = keep 1 unmanaged device_consumption entry\n"
        "  = energy  9 unchanged\n"
        "Summary: 1 create, 1 update, 9 unchanged. Dry run: nothing written.\n"
    )


def test_saved_payloads_pass_home_assistant_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = load_fixture()
    fakes = [with_energy(None), FakeHA()]
    doc = file_doc()
    doc["device_consumption"][1]["included_in_stat"] = None
    doc["energy_sources"][0]["number_energy_price_export"] = None
    apply(monkeypatch, fakes[0], atlas(tmp_path, file_doc(), "a1"), tmp_path / "s1")
    apply(monkeypatch, fakes[1], atlas(tmp_path, doc, "a2"), tmp_path / "s2")
    live = copy.deepcopy(source["energy"])
    for name, entries in source["energy_unmanaged"].items():
        live[name] += entries
    fakes.append(with_energy(live))
    apply(
        monkeypatch, fakes[2], atlas(tmp_path, doc, "a3"), tmp_path / "s3", "--energy", "replace"
    )
    payloads = [msg for fake in fakes for msg in saves(fake)]
    assert len(payloads) == 3
    for msg in payloads:
        assert required_key_errors(msg, legacy=False) == []
    checked = validate_with_ha(payloads)
    if checked is None:
        pytest.skip("no Home Assistant install to import energy/data.py from")
    assert checked[1] == [None] * len(payloads)


def test_follows_rename_skipped_as_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = export(monkeypatch, FakeHA(), tmp_path / "a")
    b = FakeHA(empty_fixture())
    calls: list[int] = []

    def intrude(fake: FakeHA, msg: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 2:  # the re-read before stage 4 writes
            row = copy.deepcopy(fake.registries["entities"][0])
            row.update(entity_id="sensor.kitchen_power", id="intruder", unique_id="intruder")
            fake.registries["entities"].append(row)

    b.before["config/entity_registry/list"] = [intrude]
    result = apply(monkeypatch, b, a, tmp_path / "snap")
    assert result.exit_code == 3, result.output
    assert "sensor.kitchen_power was registered in HA since the plan" in result.output
    assert "+ device_consumption  sensor.relay_power  " in result.output
    assert b.energy is not None
    assert b.energy["device_consumption"][3]["stat_consumption"] == "sensor.relay_power"
