# hass-atlas

A command-line tool for auditing and configuring Home Assistant energy dashboards, area assignments, and device topology. While deeply aware of [SPAN Panel](https://www.span.io/) hierarchy (via the `span_ebus` integration), hass-atlas works across all energy integrations — Tesla Powerwall, Enphase, SolarEdge, and more.

> **CAUTION: This is a power tool.**
>
> hass-atlas modifies your Home Assistant configuration directly via the WebSocket API — including the Energy Dashboard, device registry, entity registry, and area registry. Incorrect changes can break your energy tracking history or require manual cleanup.
>
> Before using hass-atlas to make changes:
>
> 1. **Create a full Home Assistant backup** and store it on another system (not just on the HA host). Go to *Settings > System > Backups > Create backup*.
> 2. **Always run with `--dry-run` first** and carefully review the proposed changes before applying them.
> 3. **Read-only commands are safe.** `audit`, `energy-topology`, and `energy-audit` (without `--prune`) only read data and never modify anything.

**Key capabilities:**

- **Auto-discover** your Home Assistant instance on the local network via mDNS
- **Audit** device hierarchy, area assignments, and energy dashboard configuration
- **Configure** the Energy Dashboard using topology-aware rules that prevent double-counting
- **Detect overlaps** between multiple energy integrations (SPAN + Powerwall + Enphase)
- **Manage areas** by assigning circuit devices to Home Assistant areas
- **Normalize** entity IDs to match current device names
- **Link sub-panels** for daisy-chain SPAN panel configurations

## Quick Start

### Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/) for dependency management
- A Home Assistant instance with a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile)

### Installation

```bash
git clone <repo-url> && cd span-hass-tools

# Option 1: pip (into a venv)
python -m venv .venv && source .venv/bin/activate
pip install .

# Option 2: pipx (isolated install, no venv needed)
pipx install .

# Option 3: Poetry (for development)
poetry install
```

### Set up your token

Export your HA access token (or add it to your shell profile):

```bash
export HASS_API_TOKEN="your-long-lived-access-token"
```

### First run

```bash
# Auto-discovers HA on your network and shows device hierarchy (read-only)
hass-atlas audit

# See energy topology analysis (read-only)
hass-atlas energy-topology

# Preview what energy would configure (dry-run, no changes made)
hass-atlas --dry-run energy --topology

# Apply after reviewing (creates a backup first!)
hass-atlas energy --topology
```

## Recommended Workflow

1. **`hass-atlas audit`** — See your SPAN device tree, check for missing areas and energy gaps. Read-only.
2. **`hass-atlas energy-topology`** — Understand your electrical topology and what hass-atlas recommends. Read-only.
3. **`hass-atlas --dry-run energy --topology`** — Preview the exact Energy Dashboard changes.
4. **`hass-atlas --dry-run areas`** — Preview area assignments for circuit devices.
5. **Review the dry-run output carefully.** If it looks right, run without `--dry-run`.

## Commands

### Global Options

Every command accepts these options:

| Option | Env Var | Description |
|--------|---------|-------------|
| `--url URL` | `HA_URL` | Home Assistant URL. Auto-discovered via mDNS if omitted. |
| `--token TOKEN` | `HASS_API_TOKEN` | Long-lived access token. **Required.** |
| `--dry-run` | — | Preview changes without applying them. **Always use this first.** |

### Command Summary

| Command | Reads | Writes | Description |
|---------|:-----:|:------:|-------------|
| `audit` | Yes | No | Device tree + diagnostics |
| `energy-topology` | Yes | No | Energy system analysis |
| `energy-audit` | Yes | `--prune` | Find stale dashboard references |
| `energy` | Yes | Yes | Auto-configure Energy Dashboard |
| `areas` | Yes | Yes | Assign devices to areas |
| `normalize` | Yes | Yes | Rename entity IDs |
| `link-panels` | Yes | Yes | Link sub-panels in device registry |

### `audit` — Device & Energy Dashboard Health Check

```bash
hass-atlas audit [--format tree|table|json]
```

Displays the SPAN device tree and reports misconfigurations:

- **Missing areas** — circuits with no area assigned
- **Energy gaps** — enabled energy sensors not in the Energy Dashboard
- **Disabled entities** — energy-relevant entities that are disabled

Output formats: `tree` (default, Rich-formatted hierarchy), `table`, or `json`.

### `energy` — Auto-Configure the Energy Dashboard

```bash
hass-atlas energy [--topology]
```

**Without `--topology`:** Adds missing SPAN energy entities to the dashboard. Additive-only — never removes existing entries.

**With `--topology`:** The main feature. Reads physical topology from SPAN entity states, detects other energy integrations, and builds an intelligent configuration:

1. **Discovers topology** — battery position (UPSTREAM/IN_PANEL), solar position, vendor metadata, feed circuits
2. **Scans integrations** — finds all HA integrations providing energy entities (Tesla, Enphase, etc.)
3. **Classifies circuits** — identifies each circuit's role (load, PV feed, battery feed, EV feed)
4. **Resolves overlaps** — decides which integration should meter each energy flow
5. **Configures dashboard** — sets grid source, solar source, battery source, and device consumption with Sankey hierarchy

Use `--dry-run` to preview changes before applying.

### `energy-topology` — Display-Only Topology Report

```bash
hass-atlas energy-topology
```

Shows the full energy system analysis without modifying anything:

- Panel hierarchy (single or daisy-chained)
- Battery and solar positions with vendor/model info
- Other detected energy integrations
- Circuit role classifications
- Recommended Energy Dashboard assignments with conflict explanations

### `energy-audit` — Find Stale Dashboard References

```bash
hass-atlas energy-audit [--prune]
```

Scans the Energy Dashboard configuration for entity references that no longer exist (deleted integrations, renamed entities, etc.). With `--prune`, removes them.

### `areas` — Assign Devices to Areas

```bash
hass-atlas areas [--mapping PATH] [--create-missing]
```

Assigns SPAN circuit devices to Home Assistant areas.

- **Default**: uses the circuit's device name as the area name
- **`--mapping`**: JSON file mapping device names to area names (use `null` to skip a device)
- **`--create-missing`**: creates areas that don't exist yet

Example mapping file:

```json
{
  "Kitchen": "Kitchen",
  "Garage-Outlets": "Garage",
  "Future-240VAC-1": null
}
```

### `normalize` — Fix Entity IDs

```bash
hass-atlas normalize
```

Renames entity IDs to match current device names. Useful when devices were first created before names were assigned (e.g., `circuit_050299_power` → `server_rack_1_spare_power`). Also updates Energy Dashboard references to use the new IDs.

### `link-panels` — Configure Panel Daisy-Chains

```bash
hass-atlas link-panels child_serial:parent_serial [...]
```

Links sub-panels to parent panels in the HA device registry. This enables the Sankey chart to show energy flowing through the panel hierarchy.

Example for a three-panel daisy-chain:

```bash
hass-atlas link-panels \
  nt-2024-d3e4f:nt-2024-a1b2c \
  nt-2024-g5h6j:nt-2024-d3e4f
```

## How Topology-Aware Configuration Works

### The Problem

Home Assistant's Energy Dashboard requires manual configuration. When you have multiple energy integrations (SPAN panels, Tesla Powerwall, Enphase solar), it's easy to double-count energy or point at the wrong sensors. The configuration depends on your *physical* electrical topology — which devices are upstream vs. downstream of each other.

### What hass-atlas Does

SPAN panels expose rich metadata about connected sub-systems: battery vendor and model, solar inverter product, their physical position relative to the panel (UPSTREAM, IN_PANEL, DOWNSTREAM), and which circuit feeds each sub-device. hass-atlas reads this metadata and cross-references it with other HA integrations to make the right configuration choices.

### Position-Dependent Rules

The physical position of a sub-device determines which integration should meter it:

**Battery UPSTREAM** (e.g., Tesla Powerwall between grid and SPAN):
- Grid source: use Powerwall integration (SPAN sees post-battery power, not true grid)
- Battery source: use Powerwall integration
- SPAN upstream energy is *not* grid — it's panel bus power

**Battery IN_PANEL** (battery connected to a SPAN circuit):
- Grid source: use SPAN upstream lug (this *is* true grid)
- Battery source: use SPAN feed circuit (charge = circuit consumption, discharge = circuit return)
- Battery feed circuit excluded from device consumption

**Solar IN_PANEL** (PV inverter connected to a SPAN circuit):
- Solar source: use SPAN feed circuit's return energy (not the Enphase integration)
- This maintains measurement consistency — all energy on the panel bus is measured by the same CTs

**Solar UPSTREAM** (PV connected before the SPAN panel):
- Solar source: use dedicated integration (Enphase, SolarEdge, etc.)

### Measurement Consistency

When a sub-device is IN_PANEL, its energy flows through the panel's CTs. Using SPAN for *that* flow and SPAN for all other circuits keeps the energy balance internally consistent. Mixing measurement systems (e.g., Enphase for solar + SPAN for consumption) creates calibration mismatches and makes double-counting hard to detect.

### CT Noise Suppression

Pure-load circuits (dishwasher, lights, etc.) should never show "return energy" — they don't generate power. But current transformers accumulate small measurement noise that appears as non-zero return values over time. hass-atlas suppresses return energy on all pure-load circuits to prevent these false positives from appearing in the Energy Dashboard.

### Sankey Hierarchy

For multi-panel setups, hass-atlas configures `included_in_stat` relationships so the Energy Dashboard Sankey chart shows energy flowing through the panel hierarchy:

```
Grid → Home → Lead Panel → Sub-Panel A → Circuits...
                         → Sub-Panel B → Circuits...
```

Each circuit's consumption is nested under its parent panel, and each sub-panel is nested under its parent, creating a complete visualization of energy flow through your home.

### Vendor Detection

hass-atlas matches SPAN's vendor metadata against known HA integration platforms:

| Vendor | HA Platforms |
|--------|-------------|
| Tesla | `powerwall`, `tesla_fleet` |
| Enphase | `enphase_envoy` |
| SolarEdge | `solaredge` |
| Generac | `generac` |
| Sonnen | `sonnen` |

## Architecture

### WebSocket API

hass-atlas uses the Home Assistant [WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) exclusively. The REST API does not expose device/entity registries or energy dashboard preferences — WebSocket is required.

Commands used:
- `config/device_registry/list` and `update` — device hierarchy and area assignment
- `config/entity_registry/list` and `update` — entity metadata and renaming
- `config/area_registry/list` and `create` — area management
- `energy/get_prefs` and `save_prefs` — Energy Dashboard configuration
- `get_states` — live entity state values (for topology discovery)

Frame size is set to 16 MB (`max_size=16*1024*1024`) because HA registries can exceed the default 1 MB WebSocket frame limit.

### mDNS Discovery

When `--url` is not specified, hass-atlas discovers Home Assistant on the local network by browsing for `_home-assistant._tcp.local.` mDNS services. If exactly one instance is found, it's used automatically.

### Package Structure

```
src/hass_atlas/
  cli.py          — Click CLI group + global options
  context.py      — Shared Context class, pass_ctx decorator
  ha_client.py    — Async WebSocket client (auth, frame size)
  models.py       — HADevice, HAEntity, HAArea, SpanDeviceTree
  registry.py     — Fetch + parse registries, build device trees
  discovery.py    — mDNS discovery of HA instances
  topology.py     — Core topology analysis engine
  energy.py       — Energy Dashboard configuration + topology apply
  audit.py        — Device tree audit + diagnostics
  areas.py        — Area assignment planning + execution
  normalize.py    — Entity ID normalization
  panels.py       — Sub-panel linking
  output.py       — Rich-based formatters (tree, table, topology)
```

## Development

```bash
poetry install                          # install dependencies
poetry run pytest tests/ -v             # run tests
poetry run ruff check src/ tests/       # lint
poetry run mypy src/                    # type check
```

### Testing

Tests use mock WebSocket fixtures — no live HA instance needed. Test coverage includes:

- Registry parsing and tree building
- Energy config construction and merging
- Topology analysis with 30+ scenarios (multi-panel, multi-vendor)
- Entity ID slugification and normalization
- WebSocket client auth and command handling
- Area assignment planning

## SPAN Energy Direction Convention

Understanding SPAN's energy direction is critical for correct configuration. All values are from the **panel's perspective**:

| Entity | Direction | Meaning |
|--------|-----------|---------|
| Circuit `exported-energy` | Panel → Circuit | **Consumption** (energy delivered to load) |
| Circuit `imported-energy` | Circuit → Panel | **Return/Generation** (backfeed from PV/battery) |
| Circuit `active-power` (negative) | — | **Consuming** power |
| Circuit `active-power` (positive) | — | **Generating** power (PV backfeed) |
| Upstream `imported-energy` | Grid → Panel | **Grid consumption** |
| Upstream `exported-energy` | Panel → Grid | **Grid export** (solar surplus) |

Note: The `span_ebus` integration negates circuit `active-power` so that positive values represent consumption, matching Home Assistant's convention for `device_consumption` stat_rate in the Energy Dashboard "Now" tab.

## License

[MIT](LICENSE)
