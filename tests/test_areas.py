"""Tests for the areas command logic."""

from __future__ import annotations

from hass_atlas.areas import _plan_assignments
from hass_atlas.models import HAArea

from tests.conftest import make_circuit_tree


def test_plan_assignments_default_mapping(sample_areas: list[HAArea]) -> None:
    """With no mapping file, device name → area name."""
    tree = make_circuit_tree([
        ("Kitchen", "area-kitchen"),  # already correct → skipped
        ("Garage", None),              # unassigned → plan assignment
    ])
    area_by_name = {a.name: a for a in sample_areas}
    actions = _plan_assignments([tree], {}, area_by_name)

    assert len(actions) == 1
    assert actions[0].device_name == "Garage"
    assert actions[0].area_name == "Garage"
    assert actions[0].needs_create is True  # "Garage" area doesn't exist


def test_plan_assignments_with_mapping(sample_areas: list[HAArea]) -> None:
    """Custom mapping overrides default."""
    tree = make_circuit_tree([
        ("Kitchen", "area-kitchen"),
        ("Garage", None),
    ])
    area_by_name = {a.name: a for a in sample_areas}
    mapping = {"Kitchen": "Kitchen", "Garage": "Living Room"}
    actions = _plan_assignments([tree], mapping, area_by_name)

    assert len(actions) == 1
    assert actions[0].device_name == "Garage"
    assert actions[0].area_name == "Living Room"
    assert actions[0].needs_create is False


def test_plan_assignments_skip_null(sample_areas: list[HAArea]) -> None:
    """null in mapping means skip."""
    tree = make_circuit_tree([
        ("Kitchen", "area-kitchen"),
        ("Garage", None),
    ])
    area_by_name = {a.name: a for a in sample_areas}
    mapping = {"Kitchen": "Kitchen", "Garage": None}
    actions = _plan_assignments([tree], mapping, area_by_name)
    assert len(actions) == 0


def test_plan_assignments_all_correct() -> None:
    """If all devices already assigned correctly, no actions."""
    tree = make_circuit_tree([
        ("Kitchen", "area-kitchen"),
        ("Garage", "area-garage"),
    ])
    area_by_name = {
        "Kitchen": HAArea(area_id="area-kitchen", name="Kitchen"),
        "Garage": HAArea(area_id="area-garage", name="Garage"),
    }
    actions = _plan_assignments([tree], {}, area_by_name)
    assert len(actions) == 0


def test_plan_assignments_reassign(sample_areas: list[HAArea]) -> None:
    """Device assigned to wrong area should be reassigned."""
    tree = make_circuit_tree([
        ("Kitchen", "area-living"),  # wrong area — should reassign to Kitchen
        ("Garage", None),
    ])
    area_by_name = {a.name: a for a in sample_areas}
    actions = _plan_assignments([tree], {}, area_by_name)

    kitchen_action = next(a for a in actions if a.device_name == "Kitchen")
    assert kitchen_action.area_name == "Kitchen"
