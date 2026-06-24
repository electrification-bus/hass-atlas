"""Panel-cascade command — read and render the multi-panel topology.

SPAN panels publish their upstream feed via the ``connection`` capability on
their upstream-lugs Homie child device.  ``fed-by-device-id`` /
``fed-by-device-type`` together let a subscriber walk the cascade upward.

This command consumes that publication through HA (the lugs ``connection``
properties surface as sensor entities in span-hass 0.2.x) and renders the
cascade as a tree, alongside any divergence with HA's device-registry
``via_device`` hierarchy.

The walk-upward termination rule (from span-hass's reference implementation
at ``custom_components/span_ebus/__init__.py::_resolve_upstream_panel``):

- ``fed-by-device-type = "energy.ebus.device.distribution-enclosure"`` —
  another panel; keep walking.
- ``fed-by-device-type = "energy.ebus.device.bess"`` — BESS at the top;
  stop here.
- triplet not published (None) — utility feed at the top; stop here.
- anything else — stop defensively (don't guess).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import click

from hass_atlas.context import Context, pass_ctx, run_async
from hass_atlas.models import HADevice, SpanDeviceTree
from hass_atlas.output import console, print_info, print_warn
from hass_atlas.registry import (
    enrich_entities_from_states,
    fetch_entity_states,
    fetch_registries,
)
from hass_atlas.topology import _find_sub_entity, _state_value
from rich.tree import Tree

DEVICE_TYPE_PANEL = "energy.ebus.device.distribution-enclosure"
DEVICE_TYPE_BESS = "energy.ebus.device.bess"


@dataclass
class UpstreamPointer:
    """What a panel's upstream-lugs reports about what's feeding it."""

    fed_by_device_id: str | None  # e.g. "nt-2143-c1akc" (a panel) or BESS device-id, or None
    fed_by_device_type: str | None  # "energy.ebus.device.distribution-enclosure", "...bess", or None
    fed_by_device_status: str | None  # "OK" / "LOST" / "DEGRADED" / None


@dataclass
class CascadeNode:
    """One node in the rendered panel cascade.

    Each node represents either a panel (when ``kind == "panel"``) or the
    non-panel source at the top of a cascade (BESS / utility / unknown).
    """

    kind: str  # "panel", "bess", "utility", "unknown"
    label: str  # panel serial, or BESS device-id, or "utility feed"
    upstream: UpstreamPointer | None = None  # only set when kind == "panel"
    children: list[CascadeNode] = field(default_factory=list)


def read_upstream_pointer(tree: SpanDeviceTree, states: dict[str, dict[str, Any]]) -> UpstreamPointer:
    """Read the ``connection/fed-by-*`` triplet from a panel's upstream-lugs.

    Returns a pointer with None fields when no upstream-lugs device exists
    or when the triplet isn't published yet (utility-feed case).
    """
    if tree.lugs_up is None:
        return UpstreamPointer(None, None, None)
    return UpstreamPointer(
        fed_by_device_id=_state_value(
            states, _find_sub_entity(tree.lugs_up, "_connection_fed-by-device-id")
        ),
        fed_by_device_type=_state_value(
            states, _find_sub_entity(tree.lugs_up, "_connection_fed-by-device-type")
        ),
        fed_by_device_status=_state_value(
            states, _find_sub_entity(tree.lugs_up, "_connection_fed-by-device-status")
        ),
    )


def build_cascade(
    trees: list[SpanDeviceTree],
    states: dict[str, dict[str, Any]],
) -> list[CascadeNode]:
    """Walk the cascade graph upward from each panel; return roots.

    A node's ``children`` are panels that report this node as their upstream.
    The list of returned nodes is the forest of cascade roots — typically
    one per BESS or utility feed in the install.
    """
    # First pass: build one CascadeNode per panel.
    panels: dict[str, CascadeNode] = {}
    for tree in trees:
        if not tree.serial:
            continue
        pointer: UpstreamPointer | None = read_upstream_pointer(tree, states)
        panels[tree.serial] = CascadeNode(
            kind="panel",
            label=tree.serial,
            upstream=pointer,
        )

    # Second pass: attach each panel under its upstream.  When the upstream
    # is another panel, hang it there directly.  When it's BESS / utility /
    # unknown, synthesize a non-panel root node (deduping by device-id so
    # multiple panels fed by the same BESS share one root).
    roots: dict[str, CascadeNode] = {}  # label → root node

    def _get_root(kind: str, label: str) -> CascadeNode:
        key = f"{kind}:{label}"
        if key not in roots:
            roots[key] = CascadeNode(kind=kind, label=label)
        return roots[key]

    for serial, node in panels.items():
        pointer = node.upstream
        if pointer is None:
            _get_root("unknown", serial).children.append(node)
            continue
        if pointer.fed_by_device_type == DEVICE_TYPE_PANEL:
            parent_serial = pointer.fed_by_device_id
            if parent_serial and parent_serial in panels:
                panels[parent_serial].children.append(node)
                continue
            # Upstream panel not in our tree set — synthesize a stub
            _get_root("panel-external", parent_serial or "(unknown)").children.append(node)
        elif pointer.fed_by_device_type == DEVICE_TYPE_BESS:
            _get_root("bess", pointer.fed_by_device_id or "(unknown)").children.append(node)
        elif pointer.fed_by_device_id is None and pointer.fed_by_device_type is None:
            _get_root("utility", "utility feed").children.append(node)
        else:
            # Defensive: don't guess
            label = f"{pointer.fed_by_device_type}:{pointer.fed_by_device_id}"
            _get_root("unknown", label).children.append(node)

    # Only return nodes that are actually roots (panel nodes that became
    # someone-else's-child are filtered out by virtue of `roots` being keyed
    # on the non-panel sources).  Add any panel-roots whose upstream couldn't
    # be resolved to a parent node in our set.
    forest: list[CascadeNode] = list(roots.values())

    # A panel that points at another panel WE DO have appears under that
    # other panel's children — it shouldn't be in roots.  But a panel that
    # has no upstream and isn't in any roots[*].children needs to surface.
    placed: set[str] = set()

    def _collect(n: CascadeNode) -> None:
        for c in n.children:
            if c.kind == "panel":
                placed.add(c.label)
            _collect(c)

    for r in forest:
        _collect(r)

    for serial, node in panels.items():
        if serial not in placed and node not in forest:
            forest.append(node)

    return forest


def render_cascade(forest: list[CascadeNode], registry_view: dict[str, HADevice] | None = None) -> None:
    """Render the cascade forest as a Rich tree, optionally cross-referenced
    against HA's device-registry view (panel → via_device_id chain)."""
    for root in forest:
        if root.kind == "bess":
            label = f"[bold yellow]BESS[/bold yellow] {root.label}"
        elif root.kind == "utility":
            label = "[bold yellow]Utility feed[/bold yellow]"
        elif root.kind == "panel-external":
            label = f"[dim yellow]Panel (not in this install)[/dim yellow] {root.label}"
        elif root.kind == "unknown":
            label = f"[bold red]Unknown upstream[/bold red] {root.label}"
        elif root.kind == "panel":
            label = f"[bold magenta]Panel[/bold magenta] {root.label}"
        else:
            label = root.label
        t = Tree(label)
        _add_cascade_branch(t, root)
        console.print(t)


def _add_cascade_branch(parent: Tree, node: CascadeNode) -> None:
    for child in node.children:
        if child.kind != "panel":
            continue
        status = child.upstream.fed_by_device_status if child.upstream else None
        # span-hass exposes fed-by-device-status as a PROBLEM-class binary_sensor,
        # so the HA-side state is "on" (problem present) / "off" (no problem).
        # Some other consumers may see the underlying enum value (LOST/DEGRADED).
        status_tag = ""
        if status in ("on", "LOST", "DEGRADED"):
            label_text = "DEGRADED" if status == "on" else status
            status_tag = f" [bold red]({label_text})[/bold red]"
        label = f"[magenta]{child.label}[/magenta]{status_tag}"
        branch = parent.add(label)
        _add_cascade_branch(branch, child)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("cascade")
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Cross-reference against HA device-registry via_device chain; "
    "flag panels where the registry diverges from the publisher cascade.",
)
@pass_ctx
def cascade(ctx: Context, check: bool) -> None:
    """Render the panel cascade as published over MQTT (via lugs `connection`)."""
    run_async(_cascade(ctx, check))


async def _cascade(ctx: Context, check: bool) -> None:
    async with ctx.client() as client:
        devices, entities, _ = await fetch_registries(client)
        states = await fetch_entity_states(client)
    enrich_entities_from_states(entities, states)

    from hass_atlas.registry import build_span_trees

    trees = build_span_trees(devices, entities)
    if not trees:
        print_warn("No SPAN panels found in HA")
        return

    forest = build_cascade(trees, states)
    print_info(f"Panel cascade for {sum(1 for t in trees if t.serial)} panel(s):")
    console.print()
    render_cascade(forest)

    if check:
        console.print()
        _print_registry_divergence(trees, states)


def _print_registry_divergence(
    trees: list[SpanDeviceTree], states: dict[str, dict[str, Any]]
) -> None:
    """Flag panels whose HA via_device_id disagrees with their publisher upstream.

    In span-hass 0.2.x, the panel-root ``via_device`` is auto-derived from
    the same publisher pointer this command reads.  Divergence means stale
    state — typically resolvable by reloading the span_ebus config entry.
    """
    panel_by_devid: dict[str, SpanDeviceTree] = {
        t.panel.id: t for t in trees if t.serial
    }

    divergent: list[tuple[str, str | None, str | None]] = []
    for tree in trees:
        if not tree.serial:
            continue
        ha_parent_serial = (
            panel_by_devid[tree.panel.via_device_id].serial
            if tree.panel.via_device_id in panel_by_devid
            else None
        )
        pointer = read_upstream_pointer(tree, states)
        pub_parent_serial = (
            pointer.fed_by_device_id
            if pointer.fed_by_device_type == DEVICE_TYPE_PANEL
            else None
        )
        if ha_parent_serial != pub_parent_serial:
            divergent.append((tree.serial, ha_parent_serial, pub_parent_serial))

    if not divergent:
        print_info("HA via_device matches publisher cascade for all panels.")
        return

    print_warn(f"{len(divergent)} panel(s) diverge between HA and publisher cascade:")
    for serial, ha, pub in divergent:
        console.print(
            f"  [magenta]{serial}[/magenta]: "
            f"HA via_device → [dim]{ha or '(none)'}[/dim], "
            f"publisher says → [dim]{pub or '(none/non-panel)'}[/dim]"
        )
    print_info(
        "Reload the span_ebus config entry for divergent panels — "
        "span-hass auto-derives via_device on each init→ready cycle."
    )
