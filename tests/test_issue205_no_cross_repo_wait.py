"""#205: a merge never waits on another repo. A live Priority record of another repo on the machine (a Priority merge
that waits or runs there) does not hold this repo's merge before its suite starts: the suite starts at once, there is
no wait and no ``holding suite start`` line in the merge log. Seam: ``merge.run_merge`` (the public merge entry).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_issue95_target_check_eject import SUITE, _branch, _world
from tools.agentops import core_targets, merge, merge_priority
from tools.agentops.machine_mode import machine_dir
from tools.agentops.merge import SuiteRun

CLEAN = "# core\nplain\nmore\n"


def _env(tmp_path, monkeypatch):
    for name, value in (("SUITE_OUTPUT_DIR", tmp_path / "suite_out"), ("MERGE_LOG_PATH", tmp_path / "merge.log")):
        monkeypatch.setattr(merge, name, value)
    monkeypatch.setenv(merge.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])


def _other_repo_record(state: str) -> None:
    """A live record of repo A on the machine, as another installation's Priority merge leaves it (this pid is alive)."""
    d = machine_dir() / "priority-merges"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acme_repo-a-live.json").write_text(
        json.dumps({"repo": "acme/repo-a", "pid": os.getpid(), "issue": 31, "state": state}), encoding="utf-8")


@pytest.mark.parametrize("state", ["waiting", "running"])
def test_a_live_priority_record_of_another_repo_does_not_hold_this_merge(tmp_path, monkeypatch, state):
    _env(tmp_path, monkeypatch)
    bare, checkout, lockfile = _world(tmp_path)
    b1 = _branch(tmp_path, bare, "f1", CLEAN)
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {})  # the holder is an ordinary merge
    _other_repo_record(state)
    sleeps: list[float] = []
    monkeypatch.setattr(merge.time, "sleep", lambda s: sleeps.append(s))
    started: list[int] = []
    monkeypatch.setattr(merge, "_run_merge_suite",
                        lambda *a, **k: (started.append(1), SuiteRun(True, "", 1))[1])

    result = merge.run_merge(b1, SUITE, cwd=checkout, lock_path=lockfile, lock_timeout=60, issues=(5,))

    log = (tmp_path / "merge.log").read_text(encoding="utf-8")
    assert result.ok and started == [1]
    assert sleeps == []
    assert "holding suite start" not in log
