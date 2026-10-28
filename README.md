# hass-atlas

A command-line tool for auditing and configuring Home Assistant energy dashboards, area assignments, and device topology. Designed as a companion to [span-hass](https://github.com/electrification-bus/span-hass) (the `span_ebus` HA integration for SPAN smart panels), but works across all energy integrations — Tesla Powerwall, Enphase, SolarEdge, and more.

> **WARNING: barely tested outside MAIN 32 and `span_ebus`. Use at your own risk.**
>
> - **Panel models other than MAIN 32** (MAIN_16, MLO_24, MAIN_40, MLO_48): hass-atlas's handling of these models is tested only against recorded panel data and edited copies of it.
> - **The [SpanPanel/span](https://github.com/SpanPanel/span) integration (`span_panel`):** support is new. `energy` has saved the Energy Dashboard for one live panel, a MAIN 32, in a test Home Assistant; everything else is tested against recorded data. The other commands that write to Home Assistant refuse to run with it.
> - The energy assignments hass-atlas proposes for these setups may be wrong. Check every one before relying on it, and keep a backup of Home Assistant before changing anything.
>
> This software is provided as is, without warranty of any kind (see [License](#license)).

**Compatibility:** Targets [`span-hass` v0.5.0](https://github.com/electrification-bus/span-hass/releases/tag/v0.5.0) (latest release, MAIN 32 only) and [`span-hass` v0.6.0b1](https://github.com/electrification-bus/span-hass/releases/tag/v0.6.0b1) (pre-release, beta support for other panel models). For panels still on `span-hass` 0.1.x, pin hass-atlas to the pre-`SHT-br5` commit on `main`.

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
- **Configure water** sensors in the Energy Dashboard water tab

## Quick Start

### Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/) for dependency management
- A Home Assistant instance with a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile)

### Installation

```bash
git clone https://github.com/electrification-bus/hass-atlas.git && cd hass-atlas

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
| `--proxy URL` | `HASS_ATLAS_PROXY` | HTTP or SOCKS proxy for the WebSocket connection. See [Proxies](#proxies). |

### Proxies

hass-atlas can reach Home Assistant through an HTTP, HTTPS or SOCKS proxy, for example an `ssh -D` tunnel:

```bash
pip install '.[socks]'        # SOCKS needs python-socks; or: poetry install -E socks
hass-atlas --url http://ha.example:8123 --proxy socks5h://localhost:1080 audit
```

Without `--proxy`, hass-atlas uses the proxy configured in the environment or the operating system the way the [websockets library](https://websockets.readthedocs.io/en/stable/topics/proxies.html) does, and falls back to `ALL_PROXY`. For an `http://` Home Assistant URL the order is `ws_proxy`, then a SOCKS proxy (`socks_proxy` or the operating system's SOCKS setting), then `https_proxy`, then `http_proxy`. For an `https://` URL it is `wss_proxy`, then the SOCKS proxy, then `https_proxy`; `http_proxy` is not used. Hosts in `NO_PROXY` are reached directly. Use `socks5h://` to have the proxy resolve the Home Assistant host name.

mDNS discovery cannot run through a proxy: with `--proxy`, `--url` is required, and with a proxy set in a `*_proxy` environment variable, discovery warns that it only searches the local network. Operating system proxy settings do not trigger the warning, and neither does `NO_PROXY=*`.

### Command Summary

| Command | Reads | Writes | Description |
|---------|:-----:|:------:|-------------|
| `audit` | Yes | No | Device tree + diagnostics |
| `cascade` | Yes | No | Panel-cascade tree from publisher topology |
| `energy-topology` | Yes | No | Energy system analysis |
| `energy-audit` | Yes | `--prune` / `--remap` | Find / fix stale dashboard references |
| `export` | Yes | No | Write floors, labels, areas, devices and entities to YAML files |
| `apply` | Yes | Yes | Make floors, labels, areas, devices and entities match those YAML files |
| `energy` | Yes | Yes | Auto-configure Energy Dashboard |
| `areas` | Yes | Yes | Assign devices to areas |
| `normalize` | Yes | Yes | Rename entity IDs |
| `link-panels` | Yes | Yes | Link sub-panels in device registry (mostly redundant on span-hass 0.2.x — auto-derived from publisher) |
| `water` | Yes | Yes | Configure Energy Dashboard water tab |

### `audit` — Device & Energy Dashboard Health Check

```bash
hass-atlas audit [--format tree|table|json]
```

Displays the SPAN device tree and reports misconfigurations:

- **Missing areas** — circuits with no area assigned
- **Energy gaps** — enabled energy sensors not in the Energy Dashboard
- **Disabled entities** — energy-relevant entities that are disabled
- **Gaps**: what hass-atlas could not determine (see [Gaps on span_ebus](#gaps-on-span_ebus))

Remote CTs are listed apart from the circuits.

Output formats: `tree` (default, Rich-formatted hierarchy), `table`, or `json`.

### `energy` — Auto-Configure the Energy Dashboard

```bash
hass-atlas energy [--topology] [--circuit-role [SERIAL:]CT-ID=ROLE ...]
```

**Without `--topology`:** Adds the same assignments `--topology` proposes, but additive-only: never removes existing entries. It lists the existing entries `--topology` would remove.

**With `--topology`:** The main feature. Reads physical topology from SPAN entity states, detects other energy integrations, and builds an intelligent configuration:

1. **Discovers topology** — battery position (UPSTREAM/IN_PANEL), solar position, vendor metadata, feed circuits
2. **Scans integrations** — finds all HA integrations providing energy entities (Tesla, Enphase, etc.)
3. **Classifies circuits** — identifies each circuit's role (load, PV feed, battery feed, EV feed)
4. **Resolves overlaps** — decides which integration should meter each energy flow
5. **Configures dashboard** — sets grid source, solar source, battery source, and device consumption with Sankey hierarchy

Use `--dry-run` to preview changes before applying.

Grid sources are written in the format the Home Assistant version accepts: one source per grid connection (`stat_energy_from` / `stat_energy_to`) from 2026.3, a single `flow_from` / `flow_to` source before it. An existing grid source keeps its cost settings.

### Gaps on span_ebus

`audit`, `energy-topology`, and `energy` report a **Gaps** list:

- **Remote CTs.** A circuit device with no breaker space is a meter outside the panel. It is left out of device consumption and area assignment until its role is stated with `--circuit-role [<serial>:]<ct-id>=grid|load|solar|battery|ignore`. Its imported energy is the flow toward the panel (grid import, solar production, battery discharge, or a load's consumption) and its exported energy the flow away. The gap compares its power with the panel's Grid Power and the upstream lugs, and suggests `grid` when they match. On span_ebus a remote CT takes any role but `subpanel`, and a breaker circuit takes only `subpanel:<serial>`.
- **Shared meters.** Circuits that report one meter are counted once, under the lowest circuit id. The group comes from the Meter Shared With sensor when it is enabled (span-hass disables it by default), else from a shared breaker space with the same readings, else from identical readings.
- **Service entrance unknown.** The upstream lugs, used as the grid, do not say what feeds them.
- **Battery upstream without an integration.** The upstream lugs are fed by the battery and no vendor battery integration is installed, so grid figures include battery flow and no battery source can be configured.
- **Subpanel circuit unlinked.** A circuit's Feeds Role is SUBPANEL, no `--circuit-role <serial>:<circuit-id>=subpanel:<serial>` names the panel it feeds, and another span_ebus panel could be that panel: one that no subpanel role names and that either has no known feeder or is linked under this panel with `link-panels`. A single panel linked under this panel is suggested; otherwise a single candidate is. Two panels suggested as each other's feeder get no suggestion. With the role stated, the fed panel's upstream lugs are a consumer under that circuit instead of a second grid source; `link-panels` alone leaves them a grid source.
- **Feeds Role not handled.** A circuit's Feeds Role is GENERATOR or MIXED, or STORAGE on a circuit other than the battery's feed circuit. It is counted as a load.
- **Feeds Role disabled.** The panel publishes Feeds Role, but its sensors are disabled in Home Assistant.

### `energy-topology` — Display-Only Topology Report

```bash
hass-atlas energy-topology [--circuit-role [SERIAL:]CT-ID=ROLE ...]
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

### `export` - Write Configuration to YAML Files

```bash
hass-atlas export [--out DIR] [--only floors,labels,areas,devices,entities]
hass-atlas --dry-run export [--out DIR]
```

Reads the floor, label, area, device and entity registries and writes one YAML file per domain to `DIR` (default `./atlas`), plus a manifest, `atlas.yaml`, holding the file format, the hass-atlas and Home Assistant versions, the exported domains and `entities: all`. It sends only read commands (`list`, and `config/entity_registry/get_entries` for entity aliases and device classes), never a write.

```
atlas/
  atlas.yaml     format, hass_atlas, ha_version, domains, entities
  floors.yaml    floor_id, name, level, icon, aliases
  labels.yaml    label_id, name, color, icon, description
  areas.yaml     area_id, name, floor_id, icon, labels, aliases, temperature_entity_id, humidity_entity_id
  devices.yaml   match, name_by_user, area_id, labels, disabled
  entities.yaml  match, entity_id, name, icon, area_id, device_class, labels, categories, aliases, hidden, disabled, options
```

`devices.yaml` holds only the devices with a user value: a name given by the user, an area, labels, or disabled by the user. Device ids are random per installation, so each device is named by a `match` block instead: its identifiers, or its connections when it has no identifiers, plus `manufacturer` and `model` when another device shares one of those pairs (`null` when the device has none). A device whose `match` block fits another live device too, whether or not that one is exported, cannot be told apart, so export leaves it out and prints a warning.

```yaml
- match:
    connections:
    - [mac, '02:00:5e:10:00:01']
  area_id: den
- match:
    identifiers:
    - [acme_hub, hub-7f3a]
    manufacturer: Acme
    model: Relay R2
  name_by_user: Pump relay
  labels:
  - critical_load
  - solar
- match:
    identifiers:
    - [acme_hub, sensor-0042]
  disabled: true
```

`entities.yaml` holds every entity (`entities: all` in the manifest), so a rebuilt Home Assistant gets every entity_id back, including those that match the id Home Assistant would generate today. Entity ids are kept in the files, but registry ids are not, so each entity is named by a `match` block: its `platform` and `unique_id`. A `mobile_app` unique_id embeds the app's webhook id, which is a credential, and a config-entry helper's unique_id holds its config entry id, which changes on a rebuild, as does the config subentry id that some conversation agents use as their unique_id; such an entity (any whose unique_id contains its config entry or subentry id) is matched by `platform` and `entity_id` instead, and its unique_id is never written. An entity of that kind whose entity_id was built from its unique_id is left out with a warning. Besides `entity_id`, only values a user set are written: `name`, `icon` and `device_class` (Home Assistant keeps its own in separate `original_*` fields), `area_id`, `labels`, `aliases` when they differ from a new entity's `[null]` (`null` in the list stands for the entity's own name), `hidden: true` and `disabled: true` for a hide or disable by a user, and the option keys a user sets: `sensor` `display_precision` and `unit_of_measurement`, and the `weather` units. Option keys Home Assistant writes itself, such as `suggested_display_precision`, are not written.

```yaml
- match: {platform: acme_hub, unique_id: hub-7f3a-r2-power}
  entity_id: sensor.pump_power
  options:
    sensor: {display_precision: 1}
- match: {platform: acme_hub, unique_id: hub-7f3a-r2-switch}
  entity_id: switch.pump_relay
  name: Pump
  icon: mdi:pump
  area_id: garage_2
  labels:
  - critical_load
  - solar
- match: {platform: acme_hub, unique_id: sensor-0042-contact}
  entity_id: binary_sensor.door_contact
  device_class: window
  hidden: true
- match: {platform: integration, entity_id: sensor.house_energy_in}
  icon: mdi:transmission-tower
- match: {platform: mobile_app, entity_id: sensor.pixel_battery_level}
  area_id: den
- match: {platform: zwave_js, unique_id: 4011223344-12-49-0-Humidity}
  entity_id: sensor.workshop_humidity
  aliases:
  - null
  - shop humidity
```

The files are meant to be committed and diffed:

- Running export twice against an unchanged Home Assistant gives identical bytes. Floors and areas keep the order Home Assistant lists them in, labels are sorted by `label_id`, devices and entities by their `match` block, and `labels`, identifier and connection lists and floor and area `aliases` are sorted. Entity `aliases` keep their order, which Home Assistant keeps.
- Only fields that hold a value are written; a domain with no items is written as `[]`.
- Timestamps (`created_at`, `modified_at`) and area pictures are not written. A picture is an `/api/image/serve/<random id>/...` path that does not survive a rebuild. Device and entity registry ids, config entry ids and the `via_device_id` and `parent_device_id` links are not written either, since they change on a rebuild. Neither are entity `categories`: category ids are random per installation, and apply does not create categories. A hand-written file can still set them by id.
- Export writes each file to a hidden `.hass-atlas-tmp-*` file in `DIR` and renames them over the old files only once all are written, so an export that fails with an error leaves the previous files in place. If the process is killed while renaming, some files can be new and some old; run export again. Staged files left by a killed run are removed by the next export; to keep them out of a commit made in between, add `.hass-atlas-tmp-*` to the repository's `.gitignore`.
- `DIR` can be `.`, a relative path or a symlink to a directory; a symlink is written through and kept. Other files already in `DIR` are kept. A `DIR` holding files other than hidden ones (such as `.DS_Store`) but no `atlas.yaml` is refused.

`--only` exports the listed domains and keeps the domains that the existing `atlas.yaml` lists. It is refused when that earlier export came from a different Home Assistant or hass-atlas version, since the manifest records one version for every file. With `--dry-run`, export prints a unified diff of each file against `DIR` and writes nothing.

### `apply` - Make Home Assistant Match the YAML Files

```bash
hass-atlas --dry-run apply DIR [--only floors,labels,areas,devices,entities] [-v]
hass-atlas apply DIR [--only ...] [--yes] [--snapshot-dir SNAPSHOT] [-v]
hass-atlas apply DIR --check
```

Reads an export directory and creates the floors, labels and areas missing from Home Assistant, renames entities to the entity_id in the files, and updates the fields that differ on them, on devices and on entities. It never creates or deletes a device or an entity, and never deletes anything else. A field absent from a file is left alone, and `null` clears it, so a hand-written file can hold only the fields it manages. Items in Home Assistant that are not in the files are left alone.

Run it with `--dry-run` first. The plan has one line per change, grouped by stage:

```
$ hass-atlas --dry-run apply atlas --only floors,areas
Plan for http://ha.local:8123  (HA 2026.10.4, atlas format 1)
Stage 1  floors  (3 in file, 2 live)
  + floor   basement          create as "basement", rename -> "Cellar"
  = floor   2 unchanged
Stage 2  areas  (5 in file, 4 live)
  ~ area    den               floor_id: null -> upstairs
  ! area    cafe              refused: floor_id attic: no floor attic in the file or in HA
  = area    3 unchanged
Stage 5  area sensors  (1 in file)
  = area    1 unchanged
Summary: 1 create, 1 update, 1 refused, 6 unchanged. Dry run: nothing written.
```

`+` is a create, `~` an update, `>` an entity rename, `=` unchanged (counted, or listed with `-v`; an item matched by name is always listed), `!` refused, a conflict or skipped, `x` a failed write, and `?` an unmatched device or entity (counted by the domain of a device's first identifier or connection, or by an entity's platform, or listed with `-v`) or a write whose result is unknown.

- **Stages.** Labels and floors are written first (stage 1), then areas (stage 2), then devices (stage 3), then entities (stage 4), then the areas' temperature and humidity sensors (stage 5), which Home Assistant checks against the entities' states. Before a stage writes, apply reads its registries again; a change whose old value has changed in Home Assistant since the plan is skipped as a conflict. After a stage writes, apply reads them again and compares them with the files; any difference stops the run. A failed write stops the run after its stage. A write that times out or loses the connection stops the run at once and is shown and journaled as unknown, since Home Assistant may have applied it. Running apply again picks up from where Home Assistant is.
- **Matching.** A file item matches the Home Assistant item with the same id, or else the one with the same name, ignoring case and spaces. An item is refused when its name matches an item that another file item already matches, or more than one item.
- **Ids.** Home Assistant makes a new item's id from its name. When the name would give a different id from the file's (an area `garage_2` named `Workshop`), apply creates the item under a temporary name that gives the file's id, then renames it. If Home Assistant still assigns another id, apply uses that id in every later reference and says so on the line.
- **Devices.** A file device matches the device in Home Assistant whose identifiers are the same set, or whose connections are, when the file gives connections; a MAC address may be written in any form Home Assistant accepts. When several devices have that set, `manufacturer` and `model` choose among them, and `null` chooses a device without one. A device still matched by several, or matched by two file items, is refused. A device that matches none is unmatched, which is normal while integrations are still being added again; running apply later picks it up. `disabled: true` disables the device as a user would, and `false` or `null` enables it if a user disabled it. A device that Home Assistant disabled itself (`disabled_by` is `config_entry`, `integration` or `device`) is refused when the file sets `disabled`: apply sets and clears only a user disable. Part of a plan:

  ```
  Stage 3  devices  (5 in file, 8 live)
    ~ device  Smart plug (mac 02:00:5e:10:00:01) area_id: den -> kitchen
                                                 disabled_by: null -> user
    ~ device  Fridge relay (acme_hub hub-7f3a, Relay R4) name_by_user: null -> "Fridge relay"
                                                         labels: [] -> [solar, critical_load]
    ! device  Weather bridge (acme_cloud acct-91) refused: disabled_by is config_entry in HA; apply sets or clears only user
    ? device  1 unmatched:      zwave_js 1  (-v lists them)
    = device  1 unchanged
  ```
- **Entities.** A file entity matches the entity in Home Assistant with the same platform and unique_id (and the domain of its `entity_id`), or, when the file matches it by `entity_id`, the entity with that entity_id, which must be of the same platform. An entity that matches none is unmatched, like a device. Renames run first in stage 4: when an entity's `entity_id` differs, apply renames it, and when the new id is held by another entity that the plan also renames away (two entities whose ids were swapped on a rebuild), each moves to `<id>_atlas_tmp` first and then to its id. A rename onto an id held by anything else, an entity the plan does not rename or a state with no registry entry, is refused, and so is a rename onto the id such an entity keeps; the entity's other fields are still applied. When a planned rename does not run, the entity's other fields are skipped, and an entity already at its temporary id whose target is still held moves back to its old id. Before each write, apply checks that the entity at the entity_id is the one it planned for, by its registry id. Then the other fields: `disabled: true` disables an entity as a user would, also one that Home Assistant disabled itself, and `disabled: false` or `null` enables it only if a user disabled it and its device is enabled. `hidden` sets and clears only a user hide, so it is refused on an entity that Home Assistant hid. Such a refusal refuses only that field; the entity's other fields are still applied. `options` keys are written into the live options of their domain, keeping the keys Home Assistant wrote there, and a key set to `null` is removed; `options: null` removes every user key. `aliases: null` sets the default `[null]`. An entity without a name of its own (neither `name` nor one from its integration) cannot be given an area; apply refuses it, as Home Assistant does. When entities were enabled, apply prints one line saying how many load after Home Assistant restarts and how many when their integration reloads. Part of a plan for a rebuilt Home Assistant:

  ```
  Stage 4  entities  (15 in file, 15 live)
    > entity  sensor.pump_power_2 -> sensor.pump_power  (through sensor.pump_power_atlas_tmp)
    > entity  sensor.pump_power -> sensor.pump_power_2  (through sensor.pump_power_2_atlas_tmp)
    > entity  sensor.relay_power -> sensor.kitchen_power
    ~ entity  sensor.pump_power options.sensor.display_precision: null -> 1
    ~ entity  binary_sensor.door_contact device_class: null -> window
                                         hidden_by: null -> user
    = entity  3 unchanged
  ```
- **References.** An area's `floor_id` and `labels`, and a device's or entity's `area_id` and `labels`, must name a floor, area or label in the files or in Home Assistant, and a category that an entity's `categories` set must exist in Home Assistant; otherwise the item is refused (an entity's rename still runs), or for a category only its `categories`. When Home Assistant gave an area or label another id than the file's, a device's or entity's references use the new id, and an area's sensors use the id an entity has once the renames have run, also with `--only` leaving out entities. A sensor that names a file entity matching no entity in Home Assistant is refused. A temperature or humidity sensor is checked only when it changes, and must be a `sensor` entity with that device class; otherwise neither sensor is set, and the area is created or updated without them. A label's `icon` must have the form `prefix:name` and its `color` must be `#RRGGBB` or a theme color such as `red`; otherwise the label is refused.
- **Confirmation.** Apply prints the plan and asks once before writing. `--yes` skips the question; without a terminal, apply writes only with `--yes`.
- **Snapshot.** Before its first write, apply creates `~/.local/state/hass-atlas/snapshots/<host>/<UTC time>/` (mode 0700; `--snapshot-dir` sets another, empty, directory) holding `before/`, an export of the registries the plan writes, as they were; `plan.json`, the plan; and `journal.jsonl`, one line per write with its payload, old values and result, synced to disk before the next write. `hass-atlas apply SNAPSHOT/before` puts back the fields that held a value; it does not clear fields that were empty or delete items the apply created. If the snapshot cannot be written, nothing is written to Home Assistant.
- **`--only`** applies the listed domains; the others are read to check references but never written.
- **`--check`** only plans, and exits 0 when nothing would change, 2 when changes are pending and 3 when items are refused or unmatched. Apply warns when the files came from a different Home Assistant major or minor version.

Exit codes: 0 done; 1 an error, a declined confirmation, a failed write or a failed check after a stage; 2 changes pending (`--check`); 3 done, but items were refused, skipped, in conflict or unmatched.

### `normalize` — Fix Entity IDs

```bash
hass-atlas normalize
```

Renames entity IDs to match current device names. Useful when devices were first created before names were assigned (e.g., `circuit_050299_power` → `server_rack_1_spare_power`). Also updates Energy Dashboard references to use the new IDs.

### `water` — Configure Energy Dashboard Water Tab

```bash
hass-atlas water [ENTITY_IDS...]
```

Adds water consumption sensors to the Energy Dashboard water tab. Works with any integration that provides water sensors (ESPHome pulse counters, smart water meters, etc.).

**Auto-discovery mode** (no arguments): Finds all HA sensors with `device_class=water` and `state_class=total_increasing`, then adds them to the water tab.

**Explicit mode** (with entity IDs): Adds only the specified sensors.

```bash
# Auto-discover all water sensors
hass-atlas --dry-run water

# Add specific sensors only
hass-atlas water sensor.water_meters_water_house_cold \
               sensor.water_meters_water_house_hot \
               sensor.water_meters_water_irrigation
```

Additive-only — existing water sources are never removed. Duplicate entries are skipped.

**HA schema note:** Water sources live under the `device_consumption_water` key in energy prefs, using `stat_consumption` as the entity reference (same pattern as `device_consumption` for electricity, not `stat_energy_from` like grid/solar sources).

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

## span_panel Integration

When Home Assistant has the [SpanPanel/span](https://github.com/SpanPanel/span) integration (`span_panel`) and not `span_ebus`, `audit`, `energy`, `energy-topology`, and `energy-audit` (without `--remap`) read each panel through the `span_panel/panel_topology` WebSocket command. That command is admin-only, so the token must belong to a Home Assistant administrator. The other commands that write (`areas`, `normalize`, `link-panels`, `cascade`, `energy-audit --remap`) exit with a message that span_panel support is not yet available.

hass-atlas maps the grid to each panel's main meter, solar to generation circuits, and device consumption to each circuit's consumed energy, counting a shared meter once and leaving out remote CTs. `audit`, `energy`, and `energy-topology` report a **Gaps** list naming what hass-atlas could not determine, such as what a remote CT measures, which circuit feeds the battery, or which circuit feeds another panel, with suggested circuits where readings allow. Suggestions are never applied. State a circuit's role with the repeatable `--circuit-role` option:

```bash
hass-atlas energy-topology \
  --circuit-role xg-1155-18fkf:remote-ct-3=grid \
  --circuit-role circuit-7=battery \
  --circuit-role xg-7814-15i6h:circuit-3=subpanel:xg-1334-066e8
```

The value is `[<serial>:]<circuit-id>=<role>`, with role `grid`, `load`, `solar`, `battery`, `subpanel:<serial>`, or `ignore`. The serial prefix is required only when the circuit id exists on more than one panel. A panel fed from another panel's feed-through lugs is stated as `<lead-serial>:feedthrough=subpanel:<serial>`. `audit --format json` includes the gaps.

`energy` saves what `energy-topology` shows, including the default it uses for each open gap: for example, a grid taken from a panel's main meter when the service entrance is unknown, or a grid source per panel when the feed between panels is unknown. Run `hass-atlas --dry-run energy --topology` first, answer the gaps with `--circuit-role`, and save only when the proposed changes are right.

## How Topology-Aware Configuration Works

### The Problem

Home Assistant's Energy Dashboard requires manual configuration. When you have multiple energy integrations (SPAN panels, Tesla Powerwall, Enphase solar), it's easy to double-count energy or point at the wrong sensors. The configuration depends on your *physical* electrical topology — which devices are upstream vs. downstream of each other.

### What hass-atlas Does

SPAN panels expose rich metadata about connected sub-systems: battery vendor and model, solar inverter product, their physical position relative to the panel (UPSTREAM, IN_PANEL, DOWNSTREAM), and which circuit feeds each sub-device. hass-atlas reads this metadata and cross-references it with other HA integrations to make the right configuration choices.

On `span-hass` 0.2.x, position is derived from the `connection` capability — each circuit's `connection/feeds-device-id` and each upstream lug's `connection/fed-by-device-id` are matched against the BESS / PV Homie device-ids. On `span-hass` 0.6.0b1, a circuit's Feeds Role (`connection/feeds-role`) also counts: a SOLAR circuit is a solar source and not a consumer, its "Energy Returned" (`imported-energy`) the production and its Power the stat_rate, and a STORAGE circuit is the battery feed when no circuit's `feeds-device-id` names the BESS. When commissioning data hasn't been published yet for a given DER, hass-atlas reports `position=None` and degrades gracefully (preferring SPAN upstream lugs as the grid source) rather than guessing wrong. See `CHANGELOG.md` §Known issues for the SPAN-side publish gaps that affect this.

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
- `config/entity_registry/list`, `get_entries` and `update` - entity metadata and renaming (`get_entries` gives `export` and `apply` the aliases and device classes that `list` leaves out)
- `config/category_registry/list` - categories that entities name, for `apply`
- `config/area_registry/list`, `create` and `update` — area management
- `config/floor_registry/list`, `create` and `update`, and the same for `config/label_registry` - floors and labels, for `export` and `apply`
- `energy/get_prefs` and `save_prefs` — Energy Dashboard configuration
- `get_states` - live entity state values (for topology discovery, and for the area sensor and entity rename checks in `apply`)

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
  water.py        — Energy Dashboard water tab configuration
  audit.py        — Device tree audit + diagnostics
  areas.py        — Area assignment planning + execution
  normalize.py    — Entity ID normalization
  panels.py       — Sub-panel linking
  span_panel.py   - span_panel backend: panel_topology, roles, gaps
  atlas/          - export and apply: domains/ (one module per registry), yamlio.py (atlas files), planner.py and plan.py (matching, plan lines), refs.py (file ids to live ids), executor.py (all apply writes), snapshot.py, export_cmd.py, apply_cmd.py
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

Tests run against sanitized tree-v1 wire snapshots loaded via `tests/_fixture_loader.py` (no live HA instance needed). The two reference panels under `tests/fixtures/tree/` are an in-panel-BESS lead panel and a sub-panel with PV, both with populated connection records — together they exercise the topology engine's IN_PANEL / UPSTREAM detection paths. Test coverage includes:

- Registry parsing and tree building (SPAN Panel + Lugs + BESS + MID + PV + circuits)
- Energy config construction and merging
- Topology analysis with multi-panel, multi-vendor scenarios
- Entity ID slugification and normalization
- WebSocket client auth and command handling
- Area assignment planning

Several legacy scenario tests are currently marked `xfail` pending rewrite against the tree-v1 fixtures — they assert on flat-model entity layouts (e.g., Kitchen / Garage circuits, `sensor.span_site_imported_energy`) that don't exist in the new shape. See `CHANGELOG.md` §Known issues.

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
