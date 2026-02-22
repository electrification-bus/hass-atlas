"""Tests for the audit command logic."""

from __future__ import annotations

from ha_atlas.audit import _extract_energy_entity_ids, _report_energy_gaps, _report_no_area
from ha_atlas.models import SpanDeviceTree


def test_extract_energy_entity_ids_grid() -> None:
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
                "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
            }
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.kitchen_energy"},
        ],
    }
    ids = _extract_energy_entity_ids(prefs)
    assert ids == {"sensor.grid_import", "sensor.grid_export", "sensor.kitchen_energy"}


def test_extract_energy_entity_ids_solar_battery() -> None:
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
            {
                "type": "battery",
                "stat_energy_from": "sensor.batt_discharge",
                "stat_energy_to": "sensor.batt_charge",
            },
        ],
    }
    ids = _extract_energy_entity_ids(prefs)
    assert ids == {"sensor.solar_energy", "sensor.batt_discharge", "sensor.batt_charge"}


def test_extract_energy_entity_ids_empty() -> None:
    assert _extract_energy_entity_ids({}) == set()


def test_report_no_area_all_assigned(span_tree: SpanDeviceTree) -> None:
    # Assign areas to all circuits
    for circuit in span_tree.circuits:
        circuit.area_id = "some-area"
    # Should not raise — just prints "OK"
    _report_no_area([span_tree])


def test_report_no_area_missing(span_tree: SpanDeviceTree) -> None:
    span_tree.circuits[0].area_id = None
    span_tree.circuits[1].area_id = None
    # Should report 2 missing
    _report_no_area([span_tree])


def test_report_energy_gaps_none_missing(span_tree: SpanDeviceTree) -> None:
    # All energy entities are "in" the dashboard
    energy_ids = set()
    for device in [span_tree.panel, span_tree.site_metering] + span_tree.circuits:
        if device:
            for e in device.entities:
                if e.device_class == "energy":
                    energy_ids.add(e.entity_id)
    _report_energy_gaps([span_tree], energy_ids)


def test_report_energy_gaps_some_missing(span_tree: SpanDeviceTree) -> None:
    # Only site metering in dashboard
    energy_ids = {"sensor.span_site_imported_energy", "sensor.span_site_exported_energy"}
    _report_energy_gaps([span_tree], energy_ids)
