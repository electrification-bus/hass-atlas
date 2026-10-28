"""Fail a test on which the fake HA's energy schema and HA's own disagreed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.atlas.fake_ha import SCHEMA_MISMATCHES


@pytest.fixture(autouse=True)
def _energy_schema_agrees() -> Iterator[None]:
    SCHEMA_MISMATCHES.clear()
    yield
    assert SCHEMA_MISMATCHES == []
