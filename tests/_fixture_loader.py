"""Translate tree-v1 fixture JSON into HA-style device/entity/state records.

The tree-v1 fixtures (``tests/fixtures/tree/``) are sanitized wire snapshots
captured from the SPAN device firmware on the new parent/child Homie 5 data
model.  They are the same shape that span-hass 0.2.0 receives over MQTT.

For hass-atlas tests we don't run a real HA — we feed HA-shaped registry
records (``HADevice``, ``HAEntity``) into ``build_span_trees`` and friends.
This module bridges the two: read a tree-v1 JSON, emit the HA records and
state map that ``span_ebus`` would have created from the same wire data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hass_atlas.models import HADevice, HAEntity

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tree"

# Homie device-type → HA device model string (matches what span-hass creates).
_MODEL_BY_DEVICE_TYPE: dict[str, str] = {
    "energy.ebus.device.distribution-enclosure": "SPAN Panel",
    "energy.ebus.device.lugs": "Lugs",
    "energy.ebus.device.bess": "Battery Storage",
    "energy.ebus.device.mid": "Microgrid Interconnect Device",
    "energy.ebus.device.pv": "Solar PV",
    "energy.ebus.device.evse": "EV Charger",
    "energy.ebus.device.circuit": "Circuit",
}

# Capability-property suffixes we treat as power/energy-flow sensors so tests
# that grep by device_class get realistic-looking entities.
_DEVICE_CLASS_BY_PROPERTY: dict[str, str] = {
    "imported-energy": "energy",
    "exported-energy": "energy",
    "active-power": "power",
    "soc": "battery",
    "soe": "energy",
}
_STATE_CLASS_BY_PROPERTY: dict[str, str] = {
    "imported-energy": "total_increasing",
    "exported-energy": "total_increasing",
    "active-power": "measurement",
    "soc": "measurement",
}


# Properties span-hass creates disabled by default (``enabled_default=False``).
_DISABLED_BY_DEFAULT: set[tuple[str, str]] = {
    ("meter", "shared-with-device-ids"),
    ("switch", "shared-with-device-ids"),
}

# Panel ``power-flows`` properties span-hass negates into Home Assistant's frame.
_NEGATED: set[tuple[str, str]] = {
    ("power-flows", "pv"),
    ("power-flows", "battery"),
    ("power-flows", "grid"),
}


def _normalize_capture(data: dict[str, Any]) -> dict[str, Any]:
    """Shape a capture (``parent`` in ``$description``, ``numeric_properties``) like nt-0000-*.

    Adds ``metadata.serial`` (the root device id), ``is_root`` and ``parent_id``,
    and merges ``numeric_properties`` into ``properties``.
    """
    devices = data["devices"]
    if data.get("metadata", {}).get("serial"):
        return data
    root = next(
        k for k, v in devices.items()
        if v["description"]["type"] == "energy.ebus.device.distribution-enclosure"
        and not v["description"].get("parent")
    )
    out: dict[str, Any] = {"metadata": {**data.get("metadata", {}), "serial": root}, "devices": {}}
    for k, v in devices.items():
        out["devices"][k] = {
            **v,
            "properties": {**v.get("properties", {}), **v.get("numeric_properties", {})},
            "parent_id": v["description"].get("parent"),
            "is_root": k == root,
        }
    return out


def _is_off_panel_circuit(fdev: dict[str, Any]) -> bool:
    """span-hass ``util.is_off_panel_circuit``: a circuit that declares no ``info/spaces``."""
    desc = fdev["description"]
    if desc["type"] != "energy.ebus.device.circuit":
        return False
    info = desc.get("nodes", {}).get("info", {})
    return "spaces" not in info.get("properties", {})


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _short_serial(panel_serial: str) -> str:
    """Return the trailing slug of an ``nt-YYYY-xxxxx`` serial for display use."""
    return panel_serial.rsplit("-", 1)[-1]


def _display_name(panel_serial: str, homie_id: str, fdev: dict[str, Any]) -> str:
    """Mirror span-hass's display-name conventions for tests."""
    dtype = fdev["description"]["type"]
    if fdev.get("is_root"):
        return f"SPAN Panel {panel_serial}"
    short = _short_serial(panel_serial)
    type_short = dtype.rsplit(".", 1)[-1]
    if type_short == "lugs":
        direction = str(fdev.get("properties", {}).get("info/direction", "")).lower()
        if direction == "upstream" or homie_id.endswith("-lugs-up"):
            return f"{short} Upstream Lugs"
        if direction == "downstream" or homie_id.endswith("-lugs-dn"):
            return f"{short} Downstream Lugs"
        return f"{short} Lugs"
    if type_short == "circuit":
        name = fdev.get("properties", {}).get("info/name")
        if not name and _is_off_panel_circuit(fdev):
            return f"{short} Remote CT"
        return name if name else homie_id[:8]
    if type_short == "mid":
        return f"{short} Microgrid Interconnect Device"
    if type_short == "bess":
        return f"{short} Battery Storage"
    if type_short == "pv":
        return f"{short} Solar PV"
    if type_short == "evse":
        return f"{short} EV Charger"
    return f"{short} {type_short}"


def load_tree_v1(
    path: Path | str,
) -> tuple[list[HADevice], list[HAEntity], dict[str, dict[str, Any]]]:
    """Load a tree-v1 fixture JSON and return HA-shaped records.

    Returns ``(devices, entities, states)`` where:

    - ``devices`` is one ``HADevice`` per fixture device with the model and
      identifier shape span-hass 0.2.0 creates.
    - ``entities`` covers every ``(capability, property)`` tuple present in
      each device's published-properties dict.  unique_id format:
      ``{panel-serial}_{homie-id}_{capability}_{property}``.
    - ``states`` maps entity_id → ``{"state": <str>, "attributes": {}}``.
    """
    data = _normalize_capture(json.loads(Path(path).read_text()))
    panel_serial: str = data["metadata"]["serial"]
    fixture_devices: dict[str, dict[str, Any]] = data["devices"]

    devices: list[HADevice] = []
    by_homie: dict[str, HADevice] = {}
    for homie_id, fdev in fixture_devices.items():
        if fdev.get("is_root"):
            ident = ("span_ebus", panel_serial)
        else:
            ident = ("span_ebus", f"{panel_serial}_{homie_id}")
        ha_dev = HADevice(
            id=f"dev-{homie_id}",
            name=_display_name(panel_serial, homie_id, fdev),
            model=_MODEL_BY_DEVICE_TYPE.get(fdev["description"]["type"]),
            identifiers=[ident],
            via_device_id=None,  # resolved below
        )
        devices.append(ha_dev)
        by_homie[homie_id] = ha_dev

    # Resolve via_device_id once every HADevice exists.
    for homie_id, fdev in fixture_devices.items():
        parent_homie = fdev.get("parent_id")
        if parent_homie and parent_homie in by_homie and not fdev.get("is_root"):
            by_homie[homie_id].via_device_id = by_homie[parent_homie].id

    entities: list[HAEntity] = []
    states: dict[str, dict[str, Any]] = {}
    for homie_id, fdev in fixture_devices.items():
        ha_dev = by_homie[homie_id]
        name_slug = _slug(ha_dev.name or homie_id)
        published = fdev.get("properties", {})
        # Enumerate every (capability, property) declared in the descriptor —
        # span-hass creates entities for these regardless of whether a value
        # has been published yet (state defaults to "unknown" until then).
        nodes = fdev.get("description", {}).get("nodes", {})
        for capability, ndecl in nodes.items():
            for property_id in ndecl.get("properties", {}):
                cap_prop = f"{capability}/{property_id}"
                unique_id = f"{panel_serial}_{homie_id}_{capability}_{property_id}"
                # Match span-hass's entity_id format: {device-slug}_{property-slug}
                # (capability is in the unique_id but not entity_id; collisions
                # across capabilities resolve via the _2 suffix counter).
                base_eid = f"sensor.{name_slug}_{_slug(property_id)}"
                entity_id = base_eid
                n = 2
                while entity_id in states:
                    entity_id = f"{base_eid}_{n}"
                    n += 1
                ent = HAEntity(
                    entity_id=entity_id,
                    unique_id=unique_id,
                    platform="span_ebus",
                    device_id=ha_dev.id,
                    device_class=_DEVICE_CLASS_BY_PROPERTY.get(property_id),
                    state_class=_STATE_CLASS_BY_PROPERTY.get(property_id),
                    original_name=property_id.replace("-", " ").title(),
                    has_entity_name=True,
                    disabled_by=(
                        "integration"
                        if (capability, property_id) in _DISABLED_BY_DEFAULT else None
                    ),
                )
                entities.append(ent)
                ha_dev.entities.append(ent)
                value = published.get(cap_prop)
                if value is not None and (capability, property_id) in _NEGATED:
                    value = str(-float(value))
                states[entity_id] = {
                    "state": "unknown" if value is None else str(value),
                    "attributes": {},
                }

    return devices, entities, states


def load_abc12() -> tuple[list[HADevice], list[HAEntity], dict[str, dict[str, Any]]]:
    """Load the lead-panel fixture (16 devices: panel + lugs + BESS + MID + 11 circuits)."""
    return load_tree_v1(FIXTURES_DIR / "nt-0000-abc12.json")


def load_capture(handle: str) -> tuple[list[HADevice], list[HAEntity], dict[str, dict[str, Any]]]:
    """Load a masked r202639 capture: ``r202639-a`` to ``-e`` or ``main32_r202639``."""
    return load_tree_v1(FIXTURES_DIR / f"{handle}-tree-v1.json")


def load_def34() -> tuple[list[HADevice], list[HAEntity], dict[str, dict[str, Any]]]:
    """Load the sub-panel fixture (25 devices: panel + lugs + BESS + MID + PV + 19 circuits)."""
    return load_tree_v1(FIXTURES_DIR / "nt-0000-def34.json")
