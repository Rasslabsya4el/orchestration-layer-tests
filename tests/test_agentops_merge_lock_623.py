"""#623: a merge waiter counts the holder's silence (suite output mtime), not just its age."""
from __future__ import annotations

import json
import os
import threading
import time
from timing_bound import bound
from pathlib import Path

import pytest

from tools.agentops import merge
from tools.agentops.merge import MergeLock, MergeLockTimeout, _append_lock_timeout_log
from tools.agentops.util import pid_alive


@pytest.fixture(autouse=True)
def _live_by_pid(monkeypatch):
    # the holder here is this very process with a back-dated `started`: judge liveness by pid only
    monkeypatch.setattr(merge, "holder_is_live", lambda pid, started: pid_alive(pid))


def _holder(tmp_path: Path, started_min_ago: float, progress_min_ago: float | None, pid: int | None = None):
    lock_path = tmp_path / "merge.lock"
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - started_min_ago * 60))
    data = {"pid": pid or os.getpid(), "started": started, "token": "other"}
    if progress_min_ago is not None:
        prog = tmp_path / "suite.out"
        prog.write_text("x", encoding="utf-8")
        t = time.time() - progress_min_ago * 60
        os.utime(prog, (t, t))
        data["progress"] = str(prog)
    lock_path.write_text(json.dumps(data), encoding="utf-8")
    return lock_path


def test_row1_a_writing_holder_is_waited_for_and_released(tmp_path: Path):
    lock_path = _holder(tmp_path, 40, 1)
    threading.Timer(1.0, lambda: lock_path.unlink()).start()
    lock = MergeLock(lock_path)
    t0 = time.monotonic()
    lock.acquire(max_age_seconds=30 * 60)
    assert 0.8 <= time.monotonic() - t0 < 10
    lock.release()


def test_row2_a_silent_holder_times_out_naming_pid_held_silent(tmp_path: Path):
    lock_path = _holder(tmp_path, 40, 31)
    with pytest.raises(MergeLockTimeout) as ei:
        MergeLock(lock_path).acquire(max_age_seconds=30 * 60, silent_takeover_seconds=0)  # #62: no takeover here
    msg = str(ei.value)
    assert f"pid {os.getpid()}" in msg
    assert "held for 40 min" in msg and "silent for 31 min" in msg


def test_row3_no_progress_key_times_out_at_started_plus_ceiling(tmp_path: Path):
    lock_path = _holder(tmp_path, 40, None)
    with pytest.raises(MergeLockTimeout) as ei:
        MergeLock(lock_path).acquire(max_age_seconds=30 * 60)
    assert "no progress file" in str(ei.value) and "held for 40 min" in str(ei.value)
    # a young commit holder is still waited on
    lock_path = _holder(tmp_path, 0.02, None)
    with pytest.raises(MergeLockTimeout):
        MergeLock(lock_path).acquire(max_age_seconds=2)


def test_row4_dead_holder_with_fresh_progress_is_taken_over_at_once(tmp_path: Path):
    lock_path = _holder(tmp_path, 5, 0, pid=2_000_000_000)
    lock = MergeLock(lock_path)
    t0 = time.monotonic()
    lock.acquire(max_age_seconds=30 * 60)
    elapsed = time.monotonic() - t0
    bound_s = bound(2.0, 13.0)  # max(floor, factor x CPU spin) (#826)
    assert elapsed < bound_s, (elapsed, bound_s)
    lock.release()


def test_set_progress_keeps_pid_started_token(tmp_path: Path):
    lock = MergeLock(tmp_path / "merge.lock")
    lock.acquire(max_age_seconds=5)
    before = json.loads(lock.path.read_text())
    lock.set_progress(tmp_path / "o.txt")
    after = json.loads(lock.path.read_text())
    assert after["progress"] == str(tmp_path / "o.txt")
    assert {k: after[k] for k in ("pid", "started", "token")} == {k: before[k] for k in ("pid", "started", "token")}
    lock.assert_held()
    lock.release()


def test_row5_merge_log_line_carries_both_durations(tmp_path: Path):
    lock_path = _holder(tmp_path, 40, 31)
    with pytest.raises(MergeLockTimeout) as ei:
        MergeLock(lock_path).acquire(max_age_seconds=30 * 60, silent_takeover_seconds=0)
    log = tmp_path / "merge.log"
    _append_lock_timeout_log("orch-1", ei.value, log_path=log)
    line = log.read_text(encoding="utf-8").strip()
    assert "lock-timeout" in line and "held for 40 min" in line and "silent for 31 min" in line


def test_run_suite_streams_its_output_file_while_running(tmp_path: Path):
    out = tmp_path / "suite.out"
    cmd = f'"{__import__("sys").executable}" -c "import time;print(\'first\',flush=True);time.sleep(1.5);print(\'second\',flush=True)"'
    seen: dict = {}

    def poll():
        deadline = time.time() + 5
        while time.time() < deadline:
            if out.exists() and "first" in out.read_text(encoding="utf-8"):
                seen["early"] = "second" not in out.read_text(encoding="utf-8")
                return
            time.sleep(0.05)

    th = threading.Thread(target=poll)
    th.start()
    ok, output = merge._run_suite(cmd, tmp_path, None, out)
    th.join(10)
    assert ok and seen.get("early") is True
    assert output.split() == ["first", "second"]
    assert out.read_text(encoding="utf-8").split() == ["first", "second"]
