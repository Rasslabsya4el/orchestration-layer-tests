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

``TEST_IDS_FILE`` (#247) names a file of node ids, one per line (blank lines skipped). When it is set and the file
exists, only the items whose nodeid is in it are kept, and the rest are deselected, before the shard split: a part then
splits the listed ids only. An entry without ``::`` is a test file's path (the selection sends one for every added or changed
test file, #256) and keeps every test of that file; it counts as collected when any test of the file was. A missing file,
or an unset variable, filters nothing.

``TEST_PC_ONLY=1`` (#247) is the Windows run: ``pytest_ignore_collect`` no longer ignores the PC-only files, and the
collected items are cut to those of the PC-only files, with no sharding (``TEST_SHARD`` is not read then). The ids
filter still applies first, so the PC-only run takes only the PC-only ids among the listed ones.

``TEST_SKIP_FILE`` (#260) names a file of node ids, one per line: the items with those ids are deselected, after the ids
filter. The workflow sets it in the Linux and the Windows job, for the tests that read a file the public test copy does
not take, in any test file (the merge runs them on the PC; #346). A missing file raises ``pytest.UsageError``; an id not collected is ignored.

A run is green only when the ids sent were all collected (#247 unit 4): ``pytest_record_collected_ids`` (tryfirst) keeps every
collected nodeid before a ``-m "not slow"`` deselection, and the trylast hook raises ``pytest.UsageError`` (exit 4, not
the exit 5 that the workflow passes) when an id this job was sent is not among them. A Linux part is not asked for the
PC-only files' ids, the ``TEST_PC_ONLY=1`` run is asked for those alone; a part whose slice is empty after the split is
no error, the ids matched. No ``TEST_IDS_FILE`` checks nothing.
Under xdist (#271) a worker that raised would end before running an item and the controller would die in
``assert not crashitem`` (``INTERNALERROR``, no ``FAILED`` line): the worker runs nothing and hands the message to the
controller (``workeroutput``), which prints it and ends the run with exit 4.

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
ENV_IDS = "TEST_IDS_FILE"
ENV_PC_ONLY = "TEST_PC_ONLY"
ENV_SKIP = "TEST_SKIP_FILE"
_WORKER_KEY = "shard_ids_not_collected"
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


def load_ids(path: str | None) -> set[str] | None:
    """The non-empty stripped lines of the ids file (#247); None when ``TEST_IDS_FILE`` is unset (no filtering).
    A set path whose file does not exist is an error: running the whole suite there would report green wrongly."""
    if not path:
        return None
    if not os.path.isfile(path):
        raise pytest.UsageError(f"{ENV_IDS} names a file that does not exist: {path!r}")
    text = Path(path).read_text(encoding="utf-8")
    found = {line.strip() for line in text.splitlines() if line.strip()}
    if not found:
        raise pytest.UsageError(f"{ENV_IDS} names a file with no ids: {path!r}")  # 0 tests would read as green
    return found


def load_skip(path: str | None) -> set[str]:
    """The non-empty stripped lines of the skip file (#260); empty when ``TEST_SKIP_FILE`` is unset. A set path whose
    file does not exist is an error: running the tests it should skip would turn the run red for a wrong reason."""
    if not path:
        return set()
    if not os.path.isfile(path):
        raise pytest.UsageError(f"{ENV_SKIP} names a file that does not exist: {path!r}")
    return {line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}


def pytest_ignore_collect(collection_path, config):
    """True for a path listed in ``tests/pc_only.txt`` while ``TEST_SHARD`` is set: it is never imported (#232).

    ``TEST_PC_ONLY=1`` (#247) turns that off: the PC-only run imports those files.
    """
    if os.environ.get(ENV_SHARD) is None or os.environ.get(ENV_PC_ONLY) == "1":
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


def missing_ids(wanted: set[str], collected: set[str], names: set[str], pc_only: bool, sharded: bool) -> tuple[list[str], int]:
    """(missing, expected count): the sorted ids this job was sent that were not collected; the PC-only ones only in the PC-only run, none of them
    on a sharded (Linux) part, all of them otherwise."""
    def in_pc_only_file(nodeid: str) -> bool:
        return nodeid.split("::", 1)[0].replace("\\", "/") in names
    if pc_only:
        expected = {nodeid for nodeid in wanted if in_pc_only_file(nodeid)}
    elif sharded:
        expected = {nodeid for nodeid in wanted if not in_pc_only_file(nodeid)}
    else:
        expected = set(wanted)
    files = {_file_of(nodeid) for nodeid in collected}
    return sorted(entry for entry in expected if entry not in collected and not (is_bare(entry) and entry in files)), len(expected)


def is_bare(entry: str) -> bool:
    """An id without ``::`` is a test file's path: the selection sends the path of every added or changed test file (#256)."""
    return "::" not in entry


def _file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].replace("\\", "/")


def is_wanted(nodeid: str, wanted: set[str]) -> bool:
    """The id was sent as itself, or its file was sent as a bare path (#256)."""
    return nodeid in wanted or _file_of(nodeid) in wanted


@pytest.hookimpl(tryfirst=True, specname="pytest_collection_modifyitems")
def pytest_record_collected_ids(config, items):
    """Keep every collected nodeid before any deselection (``-m "not slow"``): a slow id exists, it is only not run."""
    config._shard_collected_ids = {item.nodeid for item in items}


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """xdist controller: keep the not-collected message a worker handed over (#271)."""
    message = (getattr(node, "workeroutput", None) or {}).get(_WORKER_KEY)
    if message:
        node.config._shard_missing_message = message


def pytest_terminal_summary(terminalreporter):
    message = getattr(terminalreporter.config, "_shard_missing_message", None)
    if message:
        terminalreporter.write_sep("=", "ERROR: " + message, red=True)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """xdist controller: a run whose workers reported ids not collected ends with the usage-error code (#271)."""
    if getattr(session.config, "_shard_missing_message", None):
        session.exitstatus = pytest.ExitCode.USAGE_ERROR


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    value = os.environ.get(ENV_SHARD)
    pc_only = os.environ.get(ENV_PC_ONLY) == "1"
    wanted = load_ids(os.environ.get(ENV_IDS))
    skip = load_skip(os.environ.get(ENV_SKIP))
    if value is None and not pc_only and wanted is None and not skip:
        return
    names = load_pc_only(getattr(config, "rootpath", Path.cwd()))
    collected = getattr(config, "_shard_collected_ids", None)
    if wanted is not None and collected is not None:  # every id sent must have been collected (#247 unit 4)
        missing, expected = missing_ids(wanted - skip, collected, names, pc_only, value is not None)
        if missing:
            message = f"ids sent but not collected: {len(missing)} of {expected}: " + ", ".join(missing[:10])
            workeroutput = getattr(config, "workeroutput", None)
            if workeroutput is None:
                raise pytest.UsageError(message)
            # an xdist worker that raises ends before running an item and the controller dies in `assert not crashitem`
            # (#271): the worker runs nothing and hands the message to the controller, which ends the run red with it
            workeroutput[_WORKER_KEY] = message
            items[:] = []
            return
    kept, dropped = list(items), []
    if wanted is not None:  # the ids file first, so a part splits the listed ids only (#247)
        dropped += [item for item in kept if not is_wanted(item.nodeid, wanted)]
        kept = [item for item in kept if is_wanted(item.nodeid, wanted)]
    if skip:  # #260: tests the merge runs on the PC
        dropped += [item for item in kept if item.nodeid in skip]
        kept = [item for item in kept if item.nodeid not in skip]
    if pc_only:  # the Windows run: the PC-only files' items, no sharding (#247)
        others, kept = drop_pc_only(kept, names)
        dropped += others
    elif value is not None:
        i, n = parse_shard(value)
        # the files that run on the PC only are dropped from every part first, 1/1 included; then the rest is cut by position
        kept, pc_items = drop_pc_only(kept, names)
        dropped += pc_items
        if n > 1:
            kept, cut = shard_items(kept, i, n)
            dropped += cut
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
