"""Shared CLI context — imported by command modules without circular deps."""

from __future__ import annotations

import asyncio
from typing import Any

import click

from ha_atlas.ha_client import HAClient


class Context:
    """Shared CLI context."""

    def __init__(self, url: str, token: str, dry_run: bool) -> None:
        self.url = url
        self.token = token
        self.dry_run = dry_run

    def client(self) -> HAClient:
        return HAClient(self.url, self.token)


pass_ctx = click.make_pass_decorator(Context)


def run_async(coro: Any) -> Any:
    """Run an async function from a Click command."""
    return asyncio.run(coro)
