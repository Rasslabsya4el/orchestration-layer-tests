"""#62: a merge line holder silent past the takeover limit loses the line to the next waiter.

Real MergeLock files in tmp_path; the holder is a sleeping child process (a live pid), and no real tree
is killed: `merge._kill_tree` is recorded. The incident:
Other 2026-10-16, pid 9820 held the line 35 min with its suite output silent for 30 min (the repo's load
gate waiting for the machine to cool down), and the next merge only gave up with `lock-timeout`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.agentops import detach, merge, merge_handback
from tools.agentops.merge import MergeLock, MergeLockTimeout
from tools.agentops.util import no_window_flags

MIN = 60.0


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


@pytest.fixture
def line(tmp_path, monkeypatch):
    """A lock held by a fake holder: started 10 min ago, suite output last written ``silent`` seconds ago."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                              creationflags=no_window_flags())
    log = tmp_path / "merge.log"
    monkeypatch.setenv(merge.ENV_MERGE_LOG, str(log))
    kills: list[int] = []
    monkeypatch.setattr(merge, "_kill_tree", lambda pid: kills.append(pid))
    monkeypatch.setattr(merge, "_holder_tree_burning", lambda pid: False)
    # the holder child is younger than the back-dated `started` (a reused-pid reading): judge by pid only
    monkeypatch.setattr(merge, "holder_is_live", lambda pid, started, **kw: True)

    def make(silent: float) -> dict:
        out = tmp_path / "suite.out"
        out.write_text("....\n", encoding="utf-8")
        old = time.time() - silent
        os.utime(out, (old, old))
        result = tmp_path / "results" / "merge-i369.json"
        lock_path = tmp_path / "merge.lock"
        lock_path.write_text(json.dumps({
            "pid": holder.pid, "started": _stamp(time.time() - 10 * MIN), "token": "holder-token",
            "kind": "merge", "branch": "i369", "progress": str(out)}), encoding="utf-8")
        lock_path.with_name("merge.lock.holding").write_text(json.dumps({
            "pid": holder.pid, "ns": time.time_ns(),
            "info": {"kind": "merge", "branch": "i369", "issues": [369], "result": str(result)}}),
            encoding="utf-8")
        return {"lock_path": lock_path, "kills": kills, "log": log, "result": result, "pid": holder.pid}

    yield make
    holder.kill()
    holder.wait()


def test_a_holder_silent_past_the_limit_loses_the_line_to_the_next_waiter(line):
    ctx = line(silent=40 * MIN)
    waiter = MergeLock(ctx["lock_path"])
    assert waiter.acquire(max_age_seconds=3600, silent_takeover_seconds=30 * MIN,
                          ticket_info={"kind": "merge", "branch": "i372"}) is None
    try:
        assert json.loads(ctx["lock_path"].read_text(encoding="utf-8"))["token"] == waiter.token
        assert ctx["kills"] == [ctx["pid"]]
        text = ctx["log"].read_text(encoding="utf-8")
        assert "i372 | silent-holder | took the merge line from pid" in text and "i369" in text, text
        assert "silent for 40 min" in text, text
        marker = Path(str(ctx["result"]) + ".silent-holder")
        assert "silent for 40 min" in marker.read_text(encoding="utf-8")
    finally:
        waiter.release()


def test_a_holder_whose_suite_is_only_collecting_keeps_the_line(line, monkeypatch):
    ctx = line(silent=40 * MIN)
    monkeypatch.setattr(merge, "_holder_tree_burning", lambda pid: True)  # CPU still growing: collecting
    with pytest.raises(MergeLockTimeout):
        MergeLock(ctx["lock_path"]).acquire(max_age_seconds=2, silent_takeover_seconds=30 * MIN,
                                            ticket_info={"kind": "merge", "branch": "i372"})
    assert ctx["kills"] == [] and not ctx["log"].exists()
    assert json.loads(ctx["lock_path"].read_text(encoding="utf-8"))["token"] == "holder-token"


def test_a_quiet_but_live_suite_under_the_limit_keeps_the_line(line):
    ctx = line(silent=10 * MIN)
    with pytest.raises(MergeLockTimeout):
        MergeLock(ctx["lock_path"]).acquire(max_age_seconds=2, silent_takeover_seconds=30 * MIN,
                                            ticket_info={"kind": "merge", "branch": "i372"})
    assert ctx["kills"] == [] and not ctx["log"].exists()
    assert json.loads(ctx["lock_path"].read_text(encoding="utf-8"))["token"] == "holder-token"


def test_a_limit_of_zero_switches_the_takeover_off(line):
    ctx = line(silent=40 * MIN)
    with pytest.raises(MergeLockTimeout):
        MergeLock(ctx["lock_path"]).acquire(max_age_seconds=2, silent_takeover_seconds=0,
                                            ticket_info={"kind": "merge", "branch": "i372"})
    assert ctx["kills"] == []


def test_tree_burning_reads_cpu_growth_over_the_window():
    def cpu_for(growth):
        readings = iter([10.0, 10.0 + growth])
        return lambda pid: (lambda: next(readings))

    assert merge._tree_is_burning(1, cpu_for=cpu_for(0.0), sleep=lambda s: None) is False
    assert merge._tree_is_burning(1, cpu_for=cpu_for(1.0), sleep=lambda s: None) is True


def test_the_marker_gives_the_detached_result_its_reason_and_the_ticket_its_line(tmp_path):
    result = tmp_path / "20261016T161532-merge-i369.json"
    note = "silent for 40 min (merge_lock_silent_takeover_min 30 min): the merge line went to i372"
    Path(str(result) + ".silent-holder").write_text(note, encoding="utf-8")
    assert detach.silent_holder_note(result) == note
    assert detach.silent_holder_note(tmp_path / "none.json") == ""
    record = {"kind": "merge", "rc": 1, "reason": "silent-holder", "issues": [369], "pushed_sha": None,
              "in_line_s": 2400.0, "suite_s": None, "failed": None, "suite_output": None, "output": note}
    body = merge_handback._body(record, str(result), "i369")
    assert "did not land (silent-holder)" in body and note in body
