"""pytest plugin (``-p tools.agentops.suite_plan_plugin``): writes the suite's plan file (#758).

When the env var ``SUITE_PLAN_FILE`` names a path, the plugin writes once, on the controller process, after the
collection order is final: ``{"seconds": [...]}``, one number per test in the order the tests are handed out, each
the test's stored duration from pytest-testmon's datafile (env ``TESTMON_DATAFILE``, table ``test_execution``).
A test with no stored duration gets the median of the stored ones; with no stored duration at all no file is
written. Without the env var the plugin does nothing.

Order (#755): testmon re-sorts in a trylast ``pytest_collection_modifyitems`` (ascending by duration). This plugin's
hookwrapper on the same hook reorders after every other implementation: DESCENDING by each test's own stored duration
(same source as the plan file), a test with no stored duration first, ties keep the incoming order, no stored duration
at all leaves the order as it is. Each process (controller and every xdist worker) reads the same datafile, so all
reach the same order. Without xdist ``pytest_collection_finish`` runs after it; with xdist the controller does not
collect, and ``pytest_xdist_node_collection_finished`` hands over a worker's final ids, the very list xdist's load
scheduler hands out from (``self.collection``, in index order)."""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
from pathlib import Path

import pytest

ENV_PLAN = "SUITE_PLAN_FILE"
ENV_DURATIONS = "SUITE_DURATIONS_FILE"
_state = {"written": False}


def stored_durations(datafile: str) -> dict[str, float]:
    """``{nodeid: stored duration}`` from testmon's datafile (read-only); {} when it cannot be read."""
    if not datafile or not Path(datafile).exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{Path(datafile).as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT test_name, duration FROM test_execution ORDER BY rowid").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {name: float(d) for name, d in rows if d is not None}


def durations_source() -> str:
    """The datafile durations are read from: SUITE_DURATIONS_FILE if it exists, else TESTMON_DATAFILE."""
    src = os.environ.get(ENV_DURATIONS)
    if not src or not Path(src).exists():
        src = os.environ.get("TESTMON_DATAFILE", ".testmondata")
    return src


def order_longest_first(ids: list[str], stored: dict[str, float]) -> list[str]:
    """Descending by stored duration; an id with none first; ties keep the incoming order (stable sort)."""
    if not any(i in stored for i in ids):
        return list(ids)
    keys = {i: (-stored[i] if i in stored else float("-inf")) for i in ids}
    return sorted(ids, key=keys.__getitem__)


def plan_seconds(ids: list[str], stored: dict[str, float]) -> list[float] | None:
    """One number per id; a missing one is the median of the stored ones; None when none is stored."""
    known = [stored[i] for i in ids if i in stored]
    if not known:
        return None
    median = statistics.median(known)
    return [stored.get(i, median) for i in ids]


def _write(ids: list[str]) -> None:
    out = os.environ.get(ENV_PLAN)
    if not out or _state["written"] or not ids:
        return
    _state["written"] = True
    seconds = plan_seconds(list(ids), stored_durations(durations_source()))
    if seconds is None:
        return
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"seconds": seconds}), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(session, config, items):
    yield
    stored = stored_durations(durations_source())
    if not stored:
        return
    by_id = {item.nodeid: item for item in items}
    if len(by_id) != len(items):  # duplicate ids: no safe key by id
        return
    items[:] = [by_id[i] for i in order_longest_first([item.nodeid for item in items], stored)]


def pytest_collection_finish(session):
    if hasattr(session.config, "workerinput"):
        return
    _write([item.nodeid for item in session.items])


def pytest_xdist_node_collection_finished(node, ids):
    _write(list(ids))
