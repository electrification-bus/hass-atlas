"""span_ebus Feeds Role (span-hass 0.6.0b1 ``connection/feeds-role``) on edited r202639 captures.

The public captures publish no circuit Feeds Role value, so each test edits a
copy in memory: ``_edit`` sets ``connection/feeds-role`` on named circuits and
can drop published properties.  The fixture files are not modified.
"""

from __future__ import annotations

from typing import Any

import pytest

from hass_atlas import energy
from hass_atlas.energy import _build_topology
from hass_atlas.models import HADevice, HAEntity
from hass_atlas.registry import build_span_trees
from hass_atlas.span_panel import CircuitRoleError, resolve_ebus_circuit_roles
from hass_atlas.topology import EnergyTopology, _circuit_node_id
from tests._fixture_loader import capture_data, load_capture, load_tree_data

C = "xg-7814-15i6h"  # r202639-c, MAIN_40, BESS fed by circuit-5
D = "xg-1334-066e8"  # r202639-d, MLO_24
E = "xg-6159-74dlx"  # r202639-e, MLO_48
# Published on r202639-c circuit-5, the link from that circuit to the BESS.
BESS_LINK = [
    "connection/feeds-device-id", "connection/feeds-device-type",
    "connection/feeds-device-status", "connection/count",
]


def _edit(
    handle: str,
    feeds_roles: dict[str, str] | None = None,
    drop: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    data = capture_data(handle)
    for device_id, role in (feeds_roles or {}).items():
        data["devices"][device_id]["properties"]["connection/feeds-role"] = role
    for device_id, props in (drop or {}).items():
        device = data["devices"][device_id]
        for prop in props:
            device["properties"].pop(prop, None)
            device.get("numeric_properties", {}).pop(prop, None)
    return data


class Site:
    """One or more edited captures loaded as one Home Assistant."""

    def __init__(self, *captures: dict[str, Any], disable_feeds_role: bool = False) -> None:
        self.devices: list[HADevice] = []
        self.entities: list[HAEntity] = []
        self.states: dict[str, dict[str, Any]] = {}
        for n, data in enumerate(captures):
            devices, entities, states = load_tree_data(data, prefix=f"p{n}_" if n else "")
            self.devices += devices
            self.entities += entities
            self.states.update(states)
        if disable_feeds_role:
            for e in self.entities:
                if e.unique_id.endswith("_connection_feeds-role"):
                    e.disabled_by = "user"
        self.trees = build_span_trees(self.devices, self.entities)

    def tree(self, serial: str) -> Any:
        return next(t for t in self.trees if t.serial == serial)

    def circuit(self, serial: str, node_id: str) -> HADevice:
        return next(c for c in self.tree(serial).circuits if _circuit_node_id(c) == node_id)

    def eid(self, device: HADevice | None, suffix: str) -> str:
        assert device is not None
        return next(e.entity_id for e in device.entities if e.unique_id.endswith(suffix))

    def topology(self, *roles: str) -> EnergyTopology:
        resolved = resolve_ebus_circuit_roles(list(roles), self.trees)
        return _build_topology(self.trees, self.devices, self.entities, self.states, resolved)


def _preferred(topo: EnergyTopology, role: str) -> set[str]:
    return {a.entity_id for a in topo.role_assignments if a.preferred and a.role == role}


def _skipped(topo: EnergyTopology, role: str) -> set[str]:
    return {a.entity_id for a in topo.role_assignments if not a.preferred and a.role == role}


def _gaps(topo: EnergyTopology, kind: str) -> list[Any]:
    return [g for g in topo.gaps if g.kind == kind]


def _role_of(topo: EnergyTopology, circuit: HADevice) -> Any:
    return next(cr for cr in topo.circuit_roles if cr.circuit.id == circuit.id)


# ---------------------------------------------------------------------------
# Loader: the Feeds Role entity span-hass 0.6.0b1 creates
# ---------------------------------------------------------------------------


def test_loader_creates_feeds_role_entity() -> None:
    devices, entities, states = load_capture("r202639-c")
    entity = next(
        e for e in entities if e.unique_id == f"{C}_circuit-1_connection_feeds-role"
    )
    assert entity.original_name == "Feeds Role"
    assert entity.disabled_by is None
    assert entity.entity_category == "diagnostic"
    assert entity.entity_id == "sensor.bedroom_lights_feeds_role"
    assert states[entity.entity_id]["state"] == "unknown"
    shared = [e for e in entities if e.unique_id.endswith("_shared-with-device-ids")]
    assert shared and all(e.disabled_by == "integration" for e in shared)
    assert {e.original_name for e in shared} == {"Meter Shared With", "Relay Shared With"}


def test_loader_circuit_fallback_name() -> None:
    data = capture_data("r202639-c")
    del data["devices"]["circuit-1"]["properties"]["info/name"]
    devices, _, _ = load_tree_data(data)
    assert "Circuit circuit-1" in {d.name for d in devices}


# ---------------------------------------------------------------------------
# SOLAR
# ---------------------------------------------------------------------------


def test_solar_role_circuit_is_solar_source() -> None:
    site = Site(_edit("r202639-c", {"circuit-4": "SOLAR"}))
    topo = site.topology()
    solar = site.circuit(C, "circuit-4")
    panel = topo.panels[0]
    assert panel.solar_position == "IN_PANEL"
    assert panel.solar_role_circuits == [("circuit-4", "Other")]
    assert panel.feeds_roles == {"circuit-4": "SOLAR"}
    assert _preferred(topo, "solar") == {site.eid(solar, "_meter_imported-energy")}
    rate = next(a.rate_entity_id for a in topo.role_assignments if a.role == "solar")
    assert rate == site.eid(solar, "_meter_active-power")


def test_solar_role_circuit_is_not_a_consumer() -> None:
    site = Site(_edit("r202639-c", {"circuit-4": "SOLAR"}))
    topo = site.topology()
    solar = site.circuit(C, "circuit-4")
    consumption = site.eid(solar, "_meter_exported-energy")
    assert consumption not in _preferred(topo, "device_consumption")
    assert consumption in _skipped(topo, "device_consumption")
    role = _role_of(topo, solar)
    assert (role.role, role.skip_consumption, role.skip_return_energy) == ("pv_feed", True, False)


def test_no_feeds_role_value_keeps_current_behavior() -> None:
    site = Site(capture_data("r202639-c"))
    topo = site.topology()
    other = site.circuit(C, "circuit-4")
    assert topo.panels[0].feeds_roles == {}
    assert _preferred(topo, "solar") == set()
    assert site.eid(other, "_meter_exported-energy") in _preferred(topo, "device_consumption")
    assert not [g for g in topo.gaps if g.kind.startswith("feeds_role")]


def test_disabled_feeds_role_keeps_current_behavior_and_reports_gap() -> None:
    site = Site(_edit("r202639-c", {"circuit-4": "SOLAR"}), disable_feeds_role=True)
    topo = site.topology()
    assert _preferred(topo, "solar") == set()
    assert _role_of(topo, site.circuit(C, "circuit-4")).role == "load"
    [gap] = _gaps(topo, "feeds_role_disabled")
    assert gap.serial == C
    assert "5 Feeds Role sensor(s) are disabled" in gap.message
    assert gap.hint is not None and "and 2 more" in gap.hint


# ---------------------------------------------------------------------------
# STORAGE
# ---------------------------------------------------------------------------


def test_storage_agrees_with_bess_link() -> None:
    site = Site(_edit("r202639-c", {"circuit-5": "STORAGE"}))
    topo = site.topology()
    panel = topo.panels[0]
    assert (panel.battery_position, panel.battery_feed_circuit_id) == ("IN_PANEL", "circuit-5")
    assert panel.battery_by_role is False
    assert not _gaps(topo, "feeds_role_unhandled")


def test_storage_marks_battery_feed_without_bess_link() -> None:
    site = Site(_edit("r202639-c", {"circuit-5": "STORAGE"}, drop={"circuit-5": BESS_LINK}))
    topo = site.topology()
    panel = topo.panels[0]
    battery = site.circuit(C, "circuit-5")
    assert (panel.battery_position, panel.battery_feed_circuit_id) == ("IN_PANEL", "circuit-5")
    assert panel.battery_by_role is True
    assert _preferred(topo, "battery_discharge") == {site.eid(battery, "_meter_imported-energy")}
    assert _preferred(topo, "battery_charge") == {site.eid(battery, "_meter_exported-energy")}
    rates = {a.rate_entity_id for a in topo.role_assignments if a.role.startswith("battery_")}
    assert rates == {site.eid(site.tree(C).battery, "_meter_active-power")}
    assert site.eid(battery, "_meter_exported-energy") not in _preferred(topo, "device_consumption")
    assert _role_of(topo, battery).role == "bess_feed"


def test_bess_without_feed_circuit_keeps_current_behavior() -> None:
    site = Site(_edit("r202639-c", drop={"circuit-5": BESS_LINK}))
    topo = site.topology()
    assert topo.panels[0].battery_position is None
    assert _preferred(topo, "battery_discharge") == set()


def test_storage_on_other_circuit_than_bess_link_is_a_gap() -> None:
    site = Site(_edit("r202639-c", {"circuit-3": "STORAGE"}))
    topo = site.topology()
    assert topo.panels[0].battery_feed_circuit_id == "circuit-5"
    [gap] = _gaps(topo, "feeds_role_unhandled")
    assert gap.circuit_id == "circuit-3"
    assert "battery feed circuit is Tesla (circuit-5)" in gap.message
    assert _role_of(topo, site.circuit(C, "circuit-3")).role == "load"


@pytest.mark.parametrize("role", ["GENERATOR", "MIXED"])
def test_generator_and_mixed_are_gaps(role: str) -> None:
    site = Site(_edit("r202639-c", {"circuit-2": role}))
    topo = site.topology()
    [gap] = _gaps(topo, "feeds_role_unhandled")
    assert (gap.circuit_id, f"Feeds Role {role}" in gap.message) == ("circuit-2", True)
    other = site.circuit(C, "circuit-2")
    assert site.eid(other, "_meter_exported-energy") in _preferred(topo, "device_consumption")


# ---------------------------------------------------------------------------
# SUBPANEL
# ---------------------------------------------------------------------------


def test_subpanel_suggests_the_one_unfed_panel() -> None:
    site = Site(_edit("r202639-e", {"circuit-41": "SUBPANEL"}), capture_data("r202639-d"))
    topo = site.topology()
    [gap] = _gaps(topo, "subpanel_circuit_unlinked")
    assert (gap.serial, gap.circuit_id) == (E, "circuit-41")
    assert gap.suggestions == [f"--circuit-role {E}:circuit-41=subpanel:{D}"]
    # Never applied automatically: both panels' lugs stay grid.
    assert len(_preferred(topo, "grid_import")) == 2


def test_subpanel_no_suggestion_with_two_candidates() -> None:
    site = Site(
        _edit("r202639-e", {"circuit-41": "SUBPANEL"}),
        capture_data("r202639-d"),
        capture_data("r202639-c"),
    )
    [gap] = _gaps(site.topology(), "subpanel_circuit_unlinked")
    assert gap.suggestions == []
    assert gap.hint == f"--circuit-role {E}:circuit-41=subpanel:<serial>"


def test_subpanel_without_other_panel_is_no_gap() -> None:
    topo = Site(_edit("r202639-e", {"circuit-41": "SUBPANEL"})).topology()
    assert not _gaps(topo, "subpanel_circuit_unlinked")


def test_subpanel_suggests_panel_linked_under_it() -> None:
    site = Site(_edit("r202639-e", {"circuit-41": "SUBPANEL"}), capture_data("r202639-d"))
    site.tree(D).panel.via_device_id = site.tree(E).panel.id
    topo = site.topology()
    [gap] = _gaps(topo, "subpanel_circuit_unlinked")
    assert gap.suggestions == [f"--circuit-role {E}:circuit-41=subpanel:{D}"]
    assert len(_preferred(topo, "grid_import")) == 2


def test_subpanel_linked_panel_preferred_over_unfed_panel() -> None:
    site = Site(
        _edit("r202639-e", {"circuit-41": "SUBPANEL"}),
        capture_data("r202639-d"),
        capture_data("r202639-c"),
    )
    site.tree(D).panel.via_device_id = site.tree(E).panel.id
    [gap] = _gaps(site.topology(), "subpanel_circuit_unlinked")
    assert gap.suggestions == [f"--circuit-role {E}:circuit-41=subpanel:{D}"]


def test_subpanel_mutual_suggestions_withheld() -> None:
    site = Site(
        _edit("r202639-e", {"circuit-41": "SUBPANEL"}),
        _edit("r202639-d", {"circuit-1": "SUBPANEL"}),
    )
    gaps = _gaps(site.topology(), "subpanel_circuit_unlinked")
    assert sorted(g.serial for g in gaps) == sorted([E, D])
    assert all(g.suggestions == [] for g in gaps)
    assert all(g.hint.endswith("=subpanel:<serial>") for g in gaps)


def test_plain_energy_does_not_warn_about_fed_lugs_kept_as_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = Site(_edit("r202639-e", {"circuit-41": "SUBPANEL"}), capture_data("r202639-d"))
    topo = site.topology(f"{E}:circuit-41=subpanel:{D}")
    fed_lugs = site.eid(site.tree(D).lugs_up, "_meter_imported-energy")
    printed: list[str] = []
    monkeypatch.setattr(energy, "print_warn", printed.append)
    monkeypatch.setattr(energy.console, "print", lambda *a, **k: printed.append(str(a[0])))
    prefs = {"device_consumption": [{"stat_consumption": fed_lugs}]}
    energy._warn_skipped_present(prefs, topo)
    assert not any(fed_lugs in line for line in printed)


def test_subpanel_circuit_role_makes_fed_panel_a_consumer() -> None:
    site = Site(_edit("r202639-e", {"circuit-41": "SUBPANEL"}), capture_data("r202639-d"))
    topo = site.topology(f"{E}:circuit-41=subpanel:{D}")
    feeder = site.circuit(E, "circuit-41")
    feeder_energy = site.eid(feeder, "_meter_exported-energy")
    fed_lugs = site.eid(site.tree(D).lugs_up, "_meter_imported-energy")
    assert _preferred(topo, "grid_import") == {
        site.eid(site.tree(E).lugs_up, "_meter_imported-energy")
    }
    assert fed_lugs in _skipped(topo, "grid_import")
    entry = next(
        a for a in topo.role_assignments
        if a.preferred and a.role == "device_consumption" and a.entity_id == fed_lugs
    )
    assert entry.parent_entity_id == feeder_energy
    assert feeder_energy in _preferred(topo, "device_consumption")
    fed_circuit = site.tree(D).circuits[0]
    fed_entry = next(
        a for a in topo.role_assignments
        if a.entity_id == site.eid(fed_circuit, "_meter_exported-energy")
    )
    assert fed_entry.parent_entity_id == fed_lugs
    assert _role_of(topo, feeder).role == "subpanel_feed"
    assert not _gaps(topo, "subpanel_circuit_unlinked")
    assert [g.serial for g in _gaps(topo, "service_entrance_unknown")] == [E]
    assert {t.serial: t.is_lead_panel for t in topo.panels} == {E: True, D: False}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (f"{E}:circuit-41=subpanel:nt-0000-zzzzz", "no span_ebus panel with serial"),
        (f"{E}:circuit-41=subpanel:{E}", "a panel cannot feed itself"),
        (f"{E}:circuit-41=grid", "takes only subpanel"),
        (f"{E}:feedthrough=subpanel:{D}", "no feedthrough role"),
    ],
)
def test_subpanel_role_errors(spec: str, message: str) -> None:
    site = Site(capture_data("r202639-e"), capture_data("r202639-d"))
    with pytest.raises(CircuitRoleError, match=message):
        resolve_ebus_circuit_roles([spec], site.trees)


def test_subpanel_role_cycle_rejected() -> None:
    site = Site(capture_data("r202639-e"), capture_data("r202639-d"))
    with pytest.raises(CircuitRoleError, match="cycle"):
        resolve_ebus_circuit_roles(
            [f"{E}:circuit-41=subpanel:{D}", f"{D}:circuit-1=subpanel:{E}"], site.trees
        )


# ---------------------------------------------------------------------------
# MAIN 32 with a PV device
# ---------------------------------------------------------------------------


def test_main32_pv_device_unchanged() -> None:
    site = Site(capture_data("main32_r202639"))
    topo = site.topology()
    panel = topo.panels[0]
    pv_circuit = site.circuit(panel.serial, "8a562bd10fb843842670080af2bbb983")
    assert not [e for e in site.entities if e.unique_id.endswith("_connection_feeds-role")]
    assert (panel.solar_position, panel.solar_feed_circuit_id) == (
        "IN_PANEL", "8a562bd10fb843842670080af2bbb983"
    )
    assert panel.solar_role_circuits == [] and panel.feeds_roles == {}
    assert _preferred(topo, "solar") == {site.eid(pv_circuit, "_meter_imported-energy")}
    role = _role_of(topo, pv_circuit)
    assert (role.role, role.skip_consumption) == ("pv_feed", False)
    assert site.eid(pv_circuit, "_meter_exported-energy") in _preferred(
        topo, "device_consumption"
    )
