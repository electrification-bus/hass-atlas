"""Shared CLI context — imported by command modules without circular deps."""

from __future__ import annotations

import asyncio
import sys
import urllib.request
from typing import Any

import click

from hass_atlas.discovery import discover_ha
from hass_atlas.ha_client import HAClient, redact_proxy
from hass_atlas.output import print_error, print_info, print_warn


def _environment_proxy() -> str | None:
    """Return a proxy set in a ``*_proxy`` environment variable, if any.

    OS proxy settings (such as macOS system proxies) are ignored, and
    ``NO_PROXY=*`` disables the result. Any other ``NO_PROXY`` cannot be
    applied, since the Home Assistant host is not known before discovery.
    """
    proxies = urllib.request.getproxies_environment()
    if proxies.get("no", "").strip() == "*":
        return None
    for scheme in ("ws", "wss", "socks", "https", "http", "all"):
        if proxies.get(scheme):
            return proxies[scheme]
    return None


def _resolve_url(url: str | None, proxy: str | None = None) -> str:
    """Resolve HA URL: CLI/env override, otherwise mDNS discovery."""
    if url:
        return url

    if proxy:
        # mDNS is link-local multicast; it never crosses a proxy.
        print_error(
            "mDNS discovery cannot run through a proxy. Specify --url or set HA_URL."
        )
        sys.exit(1)
    env_proxy = _environment_proxy()
    if env_proxy:
        print_warn(
            f"A proxy is configured ({redact_proxy(env_proxy)}); mDNS discovery only "
            "finds Home Assistant on the local network. Specify --url to reach it "
            "through the proxy."
        )

    print_info("No --url specified, discovering Home Assistant via mDNS...")
    instances = discover_ha()

    if not instances:
        print_error("No Home Assistant instances found via mDNS. Specify --url or set HA_URL.")
        sys.exit(1)

    if len(instances) == 1:
        ha = instances[0]
        label = ha.location_name or ha.host
        print_info(f"Found: {label} at {ha.url} (v{ha.version or '?'})")
        return ha.url

    # Multiple instances — let user pick
    print_info(f"Found {len(instances)} Home Assistant instances:")
    for i, ha in enumerate(instances, 1):
        label = ha.location_name or ha.host
        click.echo(f"  {i}. {label} — {ha.url} (v{ha.version or '?'})")
    choice: int = click.prompt("Select instance", type=click.IntRange(1, len(instances)))
    return instances[choice - 1].url


class Context:
    """Shared CLI context.

    The URL is resolved on first use, so ``--help`` never runs mDNS discovery.
    """

    def __init__(
        self, url: str | None, token: str, dry_run: bool, proxy: str | None = None
    ) -> None:
        self._url = url
        self.token = token
        self.dry_run = dry_run
        self.proxy = proxy

    @property
    def url(self) -> str:
        if self._url is None:
            self._url = _resolve_url(None, self.proxy)
        return self._url

    def client(self) -> HAClient:
        return HAClient(self.url, self.token, proxy=self.proxy)


pass_ctx = click.make_pass_decorator(Context)


def run_async(coro: Any) -> Any:
    """Run an async function from a Click command."""
    return asyncio.run(coro)
