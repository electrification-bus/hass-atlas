"""Tests for the panel-cascade walker.

Mirrors the four cases the span-hass reference implementation
(``custom_components/span_ebus/__init__.py::_resolve_upstream_panel``) covers
in ``tests/test_init.py``:

1. Upstream is another panel → follow the link.
2. Upstream is a BESS → stop, this panel is the cascade top.
3. Triplet not published → utility feed; stop.
4. Upstream is an unrecognized device-type → stop defensively.
"""

from __future__ import annotations

from hass_atlas.cascade import (
    DEVICE_TYPE_BESS,
    DEVICE_TYPE_PANEL,
    UpstreamPointer,
    build_cascade,
    read_upstream_pointer,
)
from hass_atlas.models import HAEntity

from tests.conftest import make_panel_tree_with_lugs


def _set_pointer(tree, fed_by_id, fed_by_type, fed_by_status="OK"):
    """Inject lugs-up connection entities + matching states on a synthetic tree."""
    panel_serial = tree.serial
    lugs_up = tree.lugs_up
    assert lugs_up is not None
    states: dict[str, dict] = {}
    for prop, value in [
        ("fed-by-device-id", fed_by_id),
        ("fed-by-device-type", fed_by_type),
        ("fed-by-device-status", fed_by_status),
    ]:
        eid = f"sensor.{panel_serial}_lugs_up_connection_{prop.replace('-', '_')}"
        lugs_up.entities.append(HAEntity(
            entity_id=eid,
            unique_id=f"{panel_serial}_{panel_serial}-lugs-up_connection_{prop}",
            platform="span_ebus",
            device_id=lugs_up.id,
        ))
        states[eid] = {"state": "unknown" if value is None else value, "attributes": {}}
    return states


# ---------------------------------------------------------------------------
# read_upstream_pointer
# ---------------------------------------------------------------------------


def test_read_pointer_panel_upstream() -> None:
    tree = make_panel_tree_with_lugs("nt-0000-lc2", num_circuits=1)
    states = _set_pointer(tree, "nt-0000-lc1", DEVICE_TYPE_PANEL, "OK")
    p = read_upstream_pointer(tree, states)
    assert p.fed_by_device_id == "nt-0000-lc1"
    assert p.fed_by_device_type == DEVICE_TYPE_PANEL
    assert p.fed_by_device_status == "OK"


def test_read_pointer_bess_upstream() -> None:
    tree = make_panel_tree_with_lugs("nt-0000-lc1", num_circuits=1)
    states = _set_pointer(tree, "nt-0000-lc1-tg111", DEVICE_TYPE_BESS, "OK")
    p = read_upstream_pointer(tree, states)
    assert p.fed_by_device_id == "nt-0000-lc1-tg111"
    assert p.fed_by_device_type == DEVICE_TYPE_BESS


def test_read_pointer_utility_feed_returns_none_fields() -> None:
    """No triplet published → None across the board (utility feed inferred at walk time)."""
    tree = make_panel_tree_with_lugs("nt-0000-lc1", num_circuits=1)
    # No entities added, no states injected
    p = read_upstream_pointer(tree, {})
    assert p.fed_by_device_id is None
    assert p.fed_by_device_type is None


def test_read_pointer_no_lugs_up() -> None:
    """A tree without an upstream-lugs device yields an all-None pointer."""
    tree = make_panel_tree_with_lugs("nt-0000-lc1", num_circuits=1)
    tree.lugs_up = None
    p = read_upstream_pointer(tree, {})
    assert p == UpstreamPointer(None, None, None)


# ---------------------------------------------------------------------------
# build_cascade — the four termination cases
# ---------------------------------------------------------------------------


def test_cascade_panel_chain() -> None:
    """3-panel cascade: lc1 (BESS) ← lc2 ← lc3 — lc2/lc3 hang under lc1, lc1 under BESS."""
    lc1 = make_panel_tree_with_lugs("nt-0000-lc1", num_circuits=1)
    lc2 = make_panel_tree_with_lugs("nt-0000-lc2", num_circuits=1)
    lc3 = make_panel_tree_with_lugs("nt-0000-lc3", num_circuits=1)

    states = {}
    states.update(_set_pointer(lc1, "nt-0000-lc1-tg111", DEVICE_TYPE_BESS))
    states.update(_set_pointer(lc2, "nt-0000-lc1", DEVICE_TYPE_PANEL))
    states.update(_set_pointer(lc3, "nt-0000-lc2", DEVICE_TYPE_PANEL))

    forest = build_cascade([lc1, lc2, lc3], states)

    # One root in the forest: the BESS feeding lc1
    bess_roots = [n for n in forest if n.kind == "bess"]
    assert len(bess_roots) == 1
    bess = bess_roots[0]
    assert bess.label == "nt-0000-lc1-tg111"

    # BESS → lc1
    assert len(bess.children) == 1
    lc1_node = bess.children[0]
    assert lc1_node.label == "nt-0000-lc1"

    # lc1 → lc2 → lc3
    assert len(lc1_node.children) == 1
    lc2_node = lc1_node.children[0]
    assert lc2_node.label == "nt-0000-lc2"
    assert len(lc2_node.children) == 1
    assert lc2_node.children[0].label == "nt-0000-lc3"


def test_cascade_bess_root_multiple_panels() -> None:
    """Two panels each report the same BESS upstream → share one BESS root."""
    pa = make_panel_tree_with_lugs("nt-0000-pa01", num_circuits=1)
    pb = make_panel_tree_with_lugs("nt-0000-pb01", num_circuits=1)

    states = {}
    states.update(_set_pointer(pa, "shared-bess-001", DEVICE_TYPE_BESS))
    states.update(_set_pointer(pb, "shared-bess-001", DEVICE_TYPE_BESS))

    forest = build_cascade([pa, pb], states)
    bess_roots = [n for n in forest if n.kind == "bess" and n.label == "shared-bess-001"]
    assert len(bess_roots) == 1
    assert {c.label for c in bess_roots[0].children} == {"nt-0000-pa01", "nt-0000-pb01"}


def test_cascade_utility_feed_root() -> None:
    """No triplet published → panel hangs under a synthesized utility-feed root."""
    p = make_panel_tree_with_lugs("nt-0000-utl1", num_circuits=1)
    # No pointer entities/states — simulates utility feed
    forest = build_cascade([p], {})
    utility = [n for n in forest if n.kind == "utility"]
    assert len(utility) == 1
    assert utility[0].children[0].label == "nt-0000-utl1"


def test_cascade_unknown_device_type_is_defensive() -> None:
    """An unrecognized fed-by-device-type does not chain further upward."""
    p = make_panel_tree_with_lugs("nt-0000-x01", num_circuits=1)
    states = _set_pointer(p, "weird-thing-123", "energy.ebus.device.something-unexpected")

    forest = build_cascade([p], states)
    unknown = [n for n in forest if n.kind == "unknown"]
    assert len(unknown) == 1
    assert unknown[0].children[0].label == "nt-0000-x01"


def test_cascade_panel_pointer_to_panel_not_in_set() -> None:
    """When the publisher names an upstream panel we don't have, surface it as external."""
    p = make_panel_tree_with_lugs("nt-0000-orphan", num_circuits=1)
    states = _set_pointer(p, "nt-0000-missing-parent", DEVICE_TYPE_PANEL)
    forest = build_cascade([p], states)
    externals = [n for n in forest if n.kind == "panel-external"]
    assert len(externals) == 1
    assert externals[0].label == "nt-0000-missing-parent"
    assert externals[0].children[0].label == "nt-0000-orphan"
