"""Check Energy Dashboard prefs against Home Assistant's own energy/save_prefs schema.

``required_key_errors`` encodes the keys HA's schema requires for each grid
format, and always runs.  ``validate_with_ha``, ``validate_legacy_flows_with_ha``
and ``check_with_ha`` run the schema of a real Home Assistant install in a
subprocess: the interpreter named by ``HASS_ATLAS_HA_PYTHON``, else the
``span`` sibling checkout's virtualenv.  They return None when no such
interpreter can import the schema.  HA 2026.10 builds the schema with
probatio, whose errors subclass the ``voluptuous`` ones its shim provides.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

_SIBLING_PYTHON = Path(__file__).resolve().parents[2] / "span" / ".venv" / "bin" / "python"

_VALIDATE = """
import json, sys
from homeassistant.const import __version__
from homeassistant.components.energy.data import (
    DEVICE_CONSUMPTION_SCHEMA, ENERGY_SOURCE_SCHEMA,
)
import voluptuous as vol  # after homeassistant, which may substitute its own
results = []
for prefs in json.load(sys.stdin):
    try:
        ENERGY_SOURCE_SCHEMA(prefs.get("energy_sources", []))
        vol.Schema([DEVICE_CONSUMPTION_SCHEMA])(prefs.get("device_consumption", []))
        vol.Schema([DEVICE_CONSUMPTION_SCHEMA])(prefs.get("device_consumption_water", []))
        results.append(None)
    except vol.Invalid as err:
        results.append(str(err))
print(json.dumps({"version": __version__, "results": results}))
"""


@cache
def ha_python() -> str | None:
    """An interpreter that can import Home Assistant's energy schema, or None."""
    candidate = os.environ.get("HASS_ATLAS_HA_PYTHON") or str(_SIBLING_PYTHON)
    if not Path(candidate).exists():
        return None
    probe = subprocess.run(
        [candidate, "-c", "import homeassistant.components.energy.data"],
        capture_output=True,
        timeout=120,
    )
    return candidate if probe.returncode == 0 else None


def validate_with_ha(prefs_list: list[dict[str, Any]]) -> tuple[str, list[str | None]] | None:
    """Validate each prefs dict with HA's schema: ``(ha_version, [error or None])``."""
    python = ha_python()
    if python is None:
        return None
    out = subprocess.run(
        [python, "-c", _VALIDATE],
        input=json.dumps(prefs_list),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    data = json.loads(out.stdout)
    return data["version"], data["results"]


_VALIDATE_LEGACY_FLOWS = """
import json, sys
from homeassistant.components.energy.data import (
    FLOW_FROM_GRID_SOURCE_SCHEMA, FLOW_TO_GRID_SOURCE_SCHEMA,
)
results = []
for prefs in json.load(sys.stdin):
    error = None
    for s in prefs.get("energy_sources", []):
        try:
            for f in s.get("flow_from", []):
                FLOW_FROM_GRID_SOURCE_SCHEMA(f)
            for f in s.get("flow_to", []):
                FLOW_TO_GRID_SOURCE_SCHEMA(f)
        except Exception as err:
            error = f"{type(err).__name__}: {err}"
    results.append(error)
print(json.dumps(results))
"""


def validate_legacy_flows_with_ha(prefs_list: list[dict[str, Any]]) -> list[str | None] | None:
    """Validate each legacy flow with HA's flow schemas, which pre-2026.3 releases share."""
    python = ha_python()
    if python is None:
        return None
    out = subprocess.run(
        [python, "-c", _VALIDATE_LEGACY_FLOWS],
        input=json.dumps(prefs_list),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    results: list[str | None] = json.loads(out.stdout)
    return results


_SERVE = """
import json, sys
from homeassistant.components.energy.data import (
    DEVICE_CONSUMPTION_SCHEMA, ENERGY_SOURCE_SCHEMA,
)
import voluptuous as vol  # after homeassistant, which may substitute its own
devices = vol.Schema([DEVICE_CONSUMPTION_SCHEMA])
for line in sys.stdin:
    message = json.loads(line)
    try:
        result = {
            key: (ENERGY_SOURCE_SCHEMA if key == "energy_sources" else devices)(value)
            for key, value in message.items()
        }
        print(json.dumps({"result": result}), flush=True)
    except vol.Invalid as err:
        print(json.dumps({"error": str(err)}), flush=True)
"""


@cache
def _server() -> subprocess.Popen[str] | None:
    python = ha_python()
    if python is None:
        return None
    process = subprocess.Popen(
        [python, "-c", _SERVE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    atexit.register(process.kill)
    return process


def check_with_ha(lists: dict[str, Any]) -> dict[str, Any] | None:
    """Run HA's schema for each list in *lists*, as ``energy/save_prefs`` does.

    Returns ``{"result": {list: validated list}}`` or ``{"error": message}``,
    or None without a Home Assistant install. One subprocess serves every call.
    """
    process = _server()
    if process is None:
        return None
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(lists) + "\n")
    process.stdin.flush()
    reply: dict[str, Any] = json.loads(process.stdout.readline())
    return reply


def required_key_errors(prefs: dict[str, Any], legacy: bool) -> list[str]:
    """Errors against the keys HA requires: unified (2026.3+) or legacy (earlier)."""
    errors: list[str] = []
    sources = prefs.get("energy_sources", [])
    grids = [s for s in sources if s.get("type") == "grid"]
    for s in grids:
        if "cost_adjustment_day" not in s:
            errors.append(f"grid source without cost_adjustment_day: {s}")
        if legacy:
            if "flow_from" not in s or "flow_to" not in s:
                errors.append(f"legacy grid source without flow_from and flow_to: {s}")
            if "stat_energy_from" in s or "stat_energy_to" in s:
                errors.append(f"legacy grid source with unified keys: {s}")
            # HA's _flow_from_ensure_single_price reads both price keys directly.
            for f in s.get("flow_from", []) or []:
                if not {"entity_energy_price", "number_energy_price"} <= f.keys():
                    errors.append(f"legacy flow_from entry without price keys: {f}")
        else:
            if "flow_from" in s or "flow_to" in s or "power" in s:
                errors.append(f"unified grid source with legacy keys: {s}")
            if not any(s.get(k) for k in ("stat_energy_from", "stat_energy_to", "stat_rate",
                                          "power_config")):
                errors.append(f"unified grid source with no statistic: {s}")
    if legacy and len(grids) > 1:
        errors.append("more than one legacy grid source")
    if not legacy:
        for key in ("stat_energy_from", "stat_energy_to", "stat_rate"):
            values = [s[key] for s in grids if s.get(key)]
            if len(values) != len(set(values)):
                errors.append(f"grid {key} used by more than one grid source")
    for s in sources:
        if s.get("type") == "battery" and not (s.get("stat_energy_from") and s.get("stat_energy_to")):
            errors.append(f"battery source without both statistics: {s}")
        if s.get("type") in ("solar", "gas", "water") and not s.get("stat_energy_from"):
            errors.append(f"{s.get('type')} source without stat_energy_from: {s}")
    for name in ("device_consumption", "device_consumption_water"):
        for d in prefs.get(name, []):
            if not d.get("stat_consumption"):
                errors.append(f"{name} entry without stat_consumption: {d}")
    return errors
