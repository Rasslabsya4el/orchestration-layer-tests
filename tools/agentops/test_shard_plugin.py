"""pytest plugin (``-p tools.agentops.test_shard_plugin``): runs one part of the suite (#232, D119).

When the env var ``TEST_SHARD`` is ``i/n`` (1-based, ``i <= n``), the collected items are sorted by nodeid and only the
ones at positions ``p`` with ``p % n == i - 1`` are kept; the others are reported through ``pytest_deselected``. Every
process that collects (the one process without xdist, each xdist worker) takes the same part, because the split
depends on the sorted ids only. Unset, or ``1/1``, keeps everything. A value that is not ``i/n`` with 1 <= i <= n
(an empty value included) raises ``pytest.UsageError``.

Stdlib and pytest only; it runs after the other collection hooks (trylast), so a ``-m`` deselection or a plan's
reorder is applied before the split.
"""

from __future__ import annotations

import os
import re

import pytest

ENV_SHARD = "TEST_SHARD"
_SHAPE = re.compile(r"(\d+)/(\d+)")


def parse_shard(value: str) -> tuple[int, int]:
    """``i/n`` as ``(i, n)``; raises ``pytest.UsageError`` for anything else."""
    match = _SHAPE.fullmatch(value)
    if not match:
        raise pytest.UsageError(f"{ENV_SHARD} must be i/n (1-based, i <= n), got {value!r}")
    i, n = int(match.group(1)), int(match.group(2))
    if n < 1 or not 1 <= i <= n:
        raise pytest.UsageError(f"{ENV_SHARD} must be i/n with 1 <= i <= n, got {value!r}")
    return i, n


def shard_items(items: list, i: int, n: int) -> tuple[list, list]:
    """(kept, dropped): the items at sorted positions p with p % n == i - 1, kept in their incoming order."""
    position = {item.nodeid: p for p, item in enumerate(sorted(items, key=lambda item: item.nodeid))}
    kept = [item for item in items if position[item.nodeid] % n == i - 1]
    dropped = [item for item in items if position[item.nodeid] % n != i - 1]
    return kept, dropped


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    value = os.environ.get(ENV_SHARD)
    if value is None:
        return
    i, n = parse_shard(value)
    if n == 1:
        return
    kept, dropped = shard_items(items, i, n)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
