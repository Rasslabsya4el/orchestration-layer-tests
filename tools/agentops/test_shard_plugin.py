"""pytest plugin (``-p tools.agentops.test_shard_plugin``): runs one part of the suite (#232, D119).

When the env var ``TEST_SHARD`` is ``i/n`` (1-based, ``i <= n``), the collected items are sorted by nodeid and only the
ones at positions ``p`` with ``p % n == i - 1`` are kept; the others are reported through ``pytest_deselected``. Every
process that collects (the one process without xdist, each xdist worker) takes the same part, because the split
depends on the sorted ids only. Unset keeps everything. ``1/1`` keeps everything except the PC-only files: the split
is a no-op at ``n == 1``, the PC-only exclusion is not. A value that is not ``i/n`` with 1 <= i <= n (an empty value
included) raises ``pytest.UsageError``.

The files listed in ``tests/pc_only.txt`` (Windows-only tests, #232) run on the PC by the merge, not on GitHub. When
``TEST_SHARD`` is set, ``pytest_ignore_collect`` leaves them out before they are imported (a file that raises at import
would otherwise error the whole part); ``pytest_collection_modifyitems`` drops them from the collected items as well
(a file named on the command line is collected, and dropped there). A missing list drops nothing; unset keeps all.

Stdlib and pytest only; ``pytest_collection_modifyitems`` runs after the other collection hooks (trylast), so a ``-m``
deselection or a plan's reorder is applied before the split.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from .pc_only import drop_pc_only, load_pc_only

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


def pytest_ignore_collect(collection_path, config):
    """True for a path listed in ``tests/pc_only.txt`` while ``TEST_SHARD`` is set: it is never imported (#232)."""
    if os.environ.get(ENV_SHARD) is None:
        return None
    names = load_pc_only(config.rootpath)
    if not names:
        return None
    root = Path(config.rootpath).resolve()
    try:
        relative = Path(collection_path).resolve().relative_to(root)
    except ValueError:
        return None
    return True if relative.as_posix() in names else None


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    value = os.environ.get(ENV_SHARD)
    if value is None:
        return
    i, n = parse_shard(value)
    # the files that run on the PC only are dropped from every part first, 1/1 included; then the rest is cut by position
    kept, dropped = drop_pc_only(items, load_pc_only(getattr(config, "rootpath", Path.cwd())))
    dropped = list(dropped)
    if n > 1:
        kept, cut = shard_items(kept, i, n)
        dropped += cut
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
