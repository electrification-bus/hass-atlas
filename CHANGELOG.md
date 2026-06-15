# Changelog

All notable changes to `hass-atlas` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The 0.1.0 line is the initial alpha. All work to date is grouped here pending the first tagged release; once `v0.1.0` is tagged, this section will move under `[0.1.0]` with the tag date.

### Added

- Initial release of the `hass-atlas` CLI. Reads Home Assistant's device, entity, and area registries over the WebSocket API and configures the Energy Dashboard, area assignments, and panel hierarchy. SPAN-aware but works across all energy integrations.
- **mDNS auto-discovery** of Home Assistant via `_home-assistant._tcp.local.`. Falls back to `--url` / `HA_URL` env var when not on the same network.
- **`audit`** — Renders the SPAN device tree (Rich tree, table, or JSON) and reports missing areas, disabled energy entities, and energy entities not in the Energy Dashboard.
- **`energy`** — Auto-configures the Energy Dashboard. The additive default adds missing SPAN energy entities; `--topology` enables the topology-aware engine that picks the right grid / battery / solar source based on physical placement.
- **`energy-topology`** — Read-only topology analysis. Shows panel hierarchy, BESS / PV position with vendor metadata, other energy integrations on the instance, circuit role classifications, and recommended dashboard assignments with conflict reasoning.
- **`energy-audit`** — Finds Energy Dashboard references that point at entities no longer in the registry. `--prune` removes them.
- **`areas`** — Assigns SPAN circuit devices to HA areas. Defaults to circuit-name = area-name; accepts a JSON mapping file for overrides. Optional `--create-missing` creates areas that don't yet exist.
- **`normalize`** — Renames entity IDs to match current device names. Useful when devices were created with placeholder names then renamed later (e.g. `circuit_050299_power` → `server_rack_1_spare_power`). Also rewrites Energy Dashboard references.
- **`link-panels`** — Sets `via_device` in the HA device registry for daisy-chained SPAN sub-panels. Required for the multi-panel Sankey hierarchy.
- **`water`** — Configures the Energy Dashboard water tab. Auto-discovers `device_class=water` + `state_class=total_increasing` sensors, or accepts an explicit list. Additive-only.
- **Topology-aware Energy Dashboard configuration.** Reads physical placement of BESS / PV / EVSE from the SPAN data model, classifies circuits (load, PV feed, BESS feed, EV feed), detects which other HA integrations (Powerwall, Enphase, etc.) meter the same flows, and resolves overlaps in favor of measurement consistency. CT-noise suppression on pure-load circuits prevents false-positive return-energy entries.
- **Sankey hierarchy via `included_in_stat`.** Each circuit's consumption nests under its panel; each sub-panel nests under its parent panel. The Energy Dashboard "Now" tab renders the full daisy chain (Grid → Home → lead → sub-panel → circuits).
- **Compatibility with span-hass 0.2.x** (the tree-data-model line). hass-atlas reads BESS / PV position from the `connection` capability's `feeds-device-id` / `fed-by-device-id` properties instead of the retired `bess/relative-position` and `bess/feed` entities. When commissioning data isn't yet published on the wire (a known span-side gap for some installs), position degrades gracefully to `None` rather than guessing wrong.
- **Tree-v1 test fixtures.** `tests/fixtures/tree/{nt-0000-abc12.json, nt-0000-def34.json}` are sanitized wire snapshots captured directly from the SPAN firmware on the parent/child Homie 5 publication. `tests/_fixture_loader.py` translates them to HA-shaped registry records so test scenarios exercise the real shape rather than hand-rolled mocks.
- **`energy-audit --remap`** — post-upgrade cleanup for Energy Dashboard references that became stale across a span-hass major version (e.g. 0.1.x → 0.2.x). For each dead reference, tries the `_2`-suffix sibling first (HA's collision-rename pattern when a unique-ID change leaves an orphan slug behind), then falls back to a panel-device → Lugs-device remapping (the old `sensor.span_panel_nt_*_upstream_imported_energy` → `sensor.c*_*lugs_imported_energy` form). Refs with no live equivalent are dropped. Pairs with `--dry-run` for preview. The pre-existing `--prune` flag (drop without remap) remains.

### Changed

- **Tree shape updated for span-hass 0.2.x.** `SpanDeviceTree` now carries `lugs_up`, `lugs_dn`, and `mid` (BESS grandchild) fields. The retired `site_metering` field is gone — site-level power-flow values now live as a capability on the panel root rather than as a separate HA device. Registry-walk classification handles MID as a BESS grandchild via a second pass over `via_device_id`. Audit tree output reflects the new shape.
- **Upstream-lug energy lookups** moved from `tree.panel` + composite suffix (`lugs-upstream_imported-energy`) to `tree.lugs_up` + bare property suffix (`imported-energy`). Matches the entity unique-IDs span-hass 0.2.x publishes on the new dedicated Lugs device.

### Known issues

- **BESS UPSTREAM detection depends on SPAN publishing the lugs `connection` capability.** When SPAN firmware does not publish `<panel>-lugs-up/connection/fed-by-device-id`, hass-atlas cannot detect that a BESS is upstream of the panel and `extract_span_topology` reports `position=None`. Topology-aware configuration falls back to using SPAN upstream lugs as the (preferred) grid source in that case — which is correct only when no upstream BESS exists. Once SPAN starts publishing the lugs connection capability, detection fires automatically with no hass-atlas-side change.
- **Multi-panel UPSTREAM (sub-panel fed by a parent SPAN panel)** is not derivable from the wire today — SPAN does not yet write `connection/fed-by-device-id = <parent-panel>` on the sub-panel's upstream lugs. Use `link-panels` to express the relationship in HA's device registry directly; hass-atlas's Sankey hierarchy honors that linkage.
- **Several scenario tests are marked `xfail` pending rewrite against the tree-v1 fixtures.** The test infrastructure is correct (103 tests pass against the new fixtures); 19 specific scenario tests still assert on flat-model entity layouts (Kitchen / Garage circuits, `sensor.span_site_imported_energy`, retired `bess/feed`-style entities) and need per-test migration. Tracked locally in beads as SHT-0fz.

[Unreleased]: https://github.com/electrification-bus/hass-atlas/commits/main
