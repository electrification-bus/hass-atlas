"""Tests for the areas command logic."""

from __future__ import annotations

from hass_atlas.areas import _plan_assignments
from hass_atlas.models import HAArea, SpanDeviceTree


def test_plan_assignments_default_mapping(
    span_tree: SpanDeviceTree, sample_areas: list[HAArea]
) -> None:
    """With no mapping file, device name → area name."""
    area_by_name = {a.name: a for a in sample_areas}
    actions = _plan_assignments([span_tree], {}, area_by_name)

    # Kitchen already has area-kitchen, should be skipped
    # Garage has no area, should be planned
    assert len(actions) == 1
    assert actions[0].device_name == "Garage"
    assert actions[0].area_name == "Garage"
    assert actions[0].needs_create is True  # "Garage" area doesn't exist


def test_plan_assignments_with_mapping(
    span_tree: SpanDeviceTree, sample_areas: list[HAArea]
) -> None:
    """Custom mapping overrides default."""
    area_by_name = {a.name: a for a in sample_areas}
    mapping = {"Kitchen": "Kitchen", "Garage": "Living Room"}
    actions = _plan_assignments([span_tree], mapping, area_by_name)

    # Kitchen already correct → skipped
    # Garage → Living Room (exists) → planned
    assert len(actions) == 1
    assert actions[0].device_name == "Garage"
    assert actions[0].area_name == "Living Room"
    assert actions[0].needs_create is False


def test_plan_assignments_skip_null(
    span_tree: SpanDeviceTree, sample_areas: list[HAArea]
) -> None:
    """null in mapping means skip."""
    area_by_name = {a.name: a for a in sample_areas}
    mapping = {"Kitchen": "Kitchen", "Garage": None}
    actions = _plan_assignments([span_tree], mapping, area_by_name)
    # Both skipped (Kitchen correct, Garage null)
    assert len(actions) == 0


def test_plan_assignments_all_correct(
    span_tree: SpanDeviceTree, sample_areas: list[HAArea]
) -> None:
    """If all devices already assigned correctly, no actions."""
    # Set both circuits to have correct areas
    span_tree.circuits[0].area_id = "area-kitchen"
    span_tree.circuits[1].area_id = "area-garage"
    area_by_name = {
        "Kitchen": HAArea(area_id="area-kitchen", name="Kitchen"),
        "Garage": HAArea(area_id="area-garage", name="Garage"),
    }
    actions = _plan_assignments([span_tree], {}, area_by_name)
    assert len(actions) == 0


def test_plan_assignments_reassign(
    span_tree: SpanDeviceTree, sample_areas: list[HAArea]
) -> None:
    """Device assigned to wrong area should be reassigned."""
    # Kitchen assigned to Living Room instead of Kitchen
    span_tree.circuits[0].area_id = "area-living"
    area_by_name = {a.name: a for a in sample_areas}
    actions = _plan_assignments([span_tree], {}, area_by_name)

    kitchen_action = next(a for a in actions if a.device_name == "Kitchen")
    assert kitchen_action.area_name == "Kitchen"
