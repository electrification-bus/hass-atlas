# Contributing to hass-atlas

Thanks for your interest in contributing! `hass-atlas` is a command-line companion to Home Assistant for auditing and configuring the Energy Dashboard, area assignments, and device topology. It's SPAN-aware (works against the [`span-hass`](https://github.com/electrification-bus/span-hass) custom integration) but operates across all energy integrations on the HA instance — Tesla Powerwall, Enphase, SolarEdge, and others.

## How to contribute

### Discussions

Use [Discussions](https://github.com/electrification-bus/hass-atlas/discussions) for:

- Open-ended questions about the tool's design, command surface, or intent ("how should I model X in the topology engine?")
- Energy Dashboard / Sankey configuration questions where the right answer depends on your panel topology, vendor mix, or other integrations
- Multi-panel setups (daisy-chains, sub-panels, mixed SPAN-and-Powerwall topologies) — worth aligning on the shape before scoping changes
- Proposed new commands, output formats, or rules in the topology engine — worth aligning on behavior before writing code
- Questions about the relationship between this tool and the [`span-hass`](https://github.com/electrification-bus/span-hass) HA integration or the [Electrification Bus specification](https://github.com/electrification-bus/specification) (this tool reads the entity surface that span-hass creates from the spec — entity-shape questions often belong upstream)
- Thinking out loud about a proposed change before scoping it

Discussions are open-ended — a good place to align on direction before something becomes a concrete change. Aligned outcomes often turn into one or more Issues or pull requests.

### Issues

Use [Issues](https://github.com/electrification-bus/hass-atlas/issues) for actionable changes:

- Bug reports with reproduction steps (HA version, `span-hass` version if relevant, snapshot of the affected `audit` / `energy-topology` output)
- Concrete feature requests with a clear scope and a Home Assistant use case
- Topology-engine rule gaps where hass-atlas guesses wrong on a real deployment (include a redacted snapshot of the registries)
- Documentation gaps where a specific README, docstring, or in-UI string change is intended
- Discussion outcomes that have alignment and a clear scope

If you're not sure whether something is an Issue or a Discussion, start with a Discussion — we can convert it later.

### Pull requests

Pull requests are welcome.

- For small fixes (typos, docstring tweaks, version bumps, low-risk bug fixes with a test), open a PR directly.
- For substantive changes (new commands, new topology rules, new vendor-platform mappings, changes to existing output formats, changes that write new keys into `energy/save_prefs`), open a Discussion or Issue first so we can align on scope before you invest the effort.
- **This tool is a CLI client of HA, not an HA integration.** Changes to MQTT transport, Homie discovery, or panel-side device-shape semantics belong in [`ebus-sdk`](https://github.com/electrification-bus/python-sdk) or [`span-hass`](https://github.com/electrification-bus/span-hass), not here. Keep this repo focused on the HA-WebSocket-API client concerns: reading registries, deriving topology, building Energy Dashboard config, and applying it.
- **Always offer a `--dry-run` for write paths.** Every command that writes to HA must accept and honor the global `--dry-run` flag, and the dry-run output must clearly show what would change. Users will rely on dry-run to gate destructive operations on their live HA instance.
- **Energy Dashboard config edits must be conservative.** When `apply_topology_prefs` or any similar function modifies `energy/save_prefs`, preserve user-configured entries the engine doesn't manage (e.g. cost adjustments, third-party device-consumption entries, water sources). The merge logic in `energy.py` is the pattern to follow.
- **Spec conformance flows through span-hass.** When hass-atlas reads entity-shape from the SPAN integration, the shape is normative — see the [Electrification Bus specification](https://github.com/electrification-bus/specification) for the wire-level capability/property model. If hass-atlas needs an entity the integration doesn't expose, file the gap upstream rather than working around it.
- **Lint and type-check before sending.** The repo enforces [ruff](https://github.com/astral-sh/ruff) and [mypy](https://mypy-lang.org/) — run `poetry run ruff check src/ tests/` and `poetry run mypy src/` locally before pushing. CI will catch what you miss, but green-first is friendlier.
- **Tests are required.** New behavior needs a test (`poetry run pytest tests/ -v`); new bug fixes need a regression test. The test infrastructure uses sanitized tree-v1 wire snapshots loaded via `tests/_fixture_loader.py` — build new scenarios on top of the `abc12_data` / `def34_data` fixtures rather than hand-rolling flat-model mocks (those are deprecated; see CHANGELOG `[Unreleased]` § Changed).
- **Keep comments to a minimum.** The project style is to write self-explanatory code and reserve comments for non-obvious *why* (a SPAN firmware quirk, a Homie property nuance, an HA WebSocket frame-size constraint, a workaround for a specific HA registry behavior). Don't add comments that just restate the code.
- **Version bumps land in one file.** The project version lives in `pyproject.toml`; bump it on release-worthy changes and add a `CHANGELOG.md` entry describing what changed.
- One commit per logical change is fine; we don't require squash or any particular branch naming.

## Releases

A maintainer bumps the version in `pyproject.toml`, moves the `[Unreleased]` section of `CHANGELOG.md` under a new `[X.Y.Z] — YYYY-MM-DD` heading, and pushes a `vX.Y.Z` tag.

## Code of conduct

Be respectful and constructive. We appreciate everyone who takes the time to file an issue, start a discussion, or send a pull request.

## Maintenance posture

`hass-atlas` is an active alpha tool. Updates and maintenance, including responses to issues filed on GitHub, will take place on an "as time and resources permit" basis. The tool is developed alongside [`span-hass`](https://github.com/electrification-bus/span-hass), [`ebus-sdk`](https://github.com/electrification-bus/python-sdk), and the [Electrification Bus specification](https://github.com/electrification-bus/specification) — see the specification repo's README §Governance for the project's long-term governance context.
