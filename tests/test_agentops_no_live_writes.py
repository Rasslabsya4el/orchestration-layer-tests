"""#417: a merge call without its own paths writes under the session tmp dir, never the live
`var/agentops/` (merge.log, merge_suite/, results/)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401
from tools.agentops import detach, merge


def test_paths_are_redirected_in_process_and_child(_agentops_merge_paths_in_tmp: Path):
    base = _agentops_merge_paths_in_tmp
    assert merge._merge_log_path() == base / "merge.log"
    assert merge._suite_output_dir() == base / "merge_suite"
    assert detach.result_path_for("merge", "b").parent == base / "results"
    code = ("from tools.agentops import merge; print(merge._merge_log_path())")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(merge.ROOT), env=os.environ.copy(),
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert Path(out) == base / "merge.log"


def _assert_no_live_writes(writes: list[str]) -> None:
    assert writes == [], "an append wrote this PC's live state: " + "; ".join(writes)


def test_merge_log_append_leaves_live_var_untouched(live_guard, _agentops_merge_paths_in_tmp: Path):
    """An append in a test never writes the live `var/`. It is judged by the write-opens under the real
    `var/` made during the append (no read of the live log: the live-state guard fails such a read, #459)."""
    mark = f"origin/no-live-writes-417-{uuid.uuid4().hex}"
    with live_guard.record_writes() as live_writes:
        merge._append_merge_log(mark, merge.MergeResult(ok=False, reason="red-suite"))
    _assert_no_live_writes(live_writes)
    assert mark in (_agentops_merge_paths_in_tmp / "merge.log").read_text(encoding="utf-8")


def test_planted_append_under_a_stand_in_live_root_fails_the_check(live_guard, tmp_path: Path):
    """The outside case: an append that writes under a stand-in live root (registered for this test only)
    is recorded, and the same check that guards the real log fails on it."""
    live = tmp_path / "stand_in_live_var"
    with live_guard.record_writes(live) as writes:
        merge._append_merge_log("origin/planted-459", merge.MergeResult(ok=False, reason="red-suite"),
                                log_path=live / "merge.log")
    assert writes, "the recorder missed a planted append under the stand-in live root"
    with pytest.raises(AssertionError, match="merge.log"):
        _assert_no_live_writes(writes)


def test_the_default_roots_of_the_recorder_are_the_live_var_roots(live_guard, tmp_path: Path, monkeypatch):
    """`record_writes()` with no root watches `VAR_ROOTS`, read when the block opens: moved to a stand-in, a write
    under it is recorded and a write elsewhere is not (#459: the real test relies on this wiring)."""
    live = tmp_path / "live_var"
    live.mkdir()
    monkeypatch.setattr(live_guard, "VAR_ROOTS", live_guard._both(live))
    with live_guard.record_writes() as writes:
        (live / "a.log").write_text("x", encoding="utf-8")
        (tmp_path / "elsewhere.log").write_text("x", encoding="utf-8")
    assert len(writes) == 1 and writes[0].endswith("a.log")
