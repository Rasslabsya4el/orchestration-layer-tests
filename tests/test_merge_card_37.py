"""#37: the Merge card's data path. `merge_shape` (mods/pob-slots/bin/collect.py) passes a commit holder's `kind`,
`message` and `issues` from `merge-status --json` to the card unchanged, and adds the human durations. Fixtures are
plain dicts shaped as the command prints them; 0 PoB, no subprocess."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    """The collector's project root is a folder of this test's own (`MONITOR_ROOT`, read when the module loads):
    its `var/agentops/merge.log` is absent, so no real merge log is read (#367)."""
    monkeypatch.setenv("MONITOR_ROOT", str(tmp_path))
    return tmp_path


def _collect():
    spec = importlib.util.spec_from_file_location("collect_37", COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _commit_running() -> dict:
    # the shape merge_status.collect writes for a commit holder (the message is cut to 60 characters there)
    return {"kind": "commit", "message": "Hermes finances agent joins the home targets", "branches": [],
            "issues": [29], "phase": "suite", "percent": 76, "tests_done": 1221, "tests_total": 1606, "failed": 0,
            "eta_s": 200, "started": "2026-10-16T13:44:36+02:00", "workers": 6, "suite_out": None}


def test_commit_holder_reaches_the_card_with_its_kind_message_and_ticket():
    data = {"at": "2026-10-16T14:00:00+02:00", "running": _commit_running()}
    run = _collect().merge_shape(data)["running"]
    assert run["kind"] == "commit"
    assert run["message"] == "Hermes finances agent joins the home targets"
    assert run["issues"] == [29]
    assert run["branches"] == []
    assert run["eta_h"] and run["elapsed_h"], "the human durations are still added for a commit holder"


def test_commit_holder_without_a_ticket_keeps_an_empty_issue_list():
    running = {**_commit_running(), "message": "Tidy the lock file", "issues": []}
    run = _collect().merge_shape({"at": "2026-10-16T14:00:00+02:00", "running": running})["running"]
    assert run["kind"] == "commit" and run["issues"] == [] and run["message"] == "Tidy the lock file"


def test_old_record_without_kind_or_message_passes_through_unchanged():
    old = {**_commit_running(), "branches": ["13-port"], "issues": [13]}
    del old["kind"]
    del old["message"]
    run = _collect().merge_shape({"at": "2026-10-16T14:00:00+02:00", "running": old})["running"]
    assert run.get("kind") is None and run.get("message") is None
    assert run["branches"] == ["13-port"] and run["issues"] == [13]
