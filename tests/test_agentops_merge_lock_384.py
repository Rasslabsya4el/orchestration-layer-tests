"""#384: the merge lock never removes a holder file it cannot parse while that file is young.

Two merges of one ORCH session ran their suites at once in the one suite worktree: a waiter read
a just-created, still empty lock file (or one in Windows pending-delete) as a dead holder and
removed it. Rows 1-3 of the issue's acceptance table.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.agentops.merge import UNPARSABLE_GRACE_SECONDS, MergeLock, MergeLockTimeout

REPO = Path(__file__).resolve().parents[1]


def test_row1_a_young_empty_holder_file_is_waited_on_not_removed(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    lock_path.write_bytes(b"")  # created, not yet written: mtime now
    lock = MergeLock(lock_path)
    t0 = time.monotonic()
    with pytest.raises(MergeLockTimeout):
        lock.acquire(max_age_seconds=3)
    assert time.monotonic() - t0 >= 2.5
    assert lock_path.exists() and lock_path.read_bytes() == b""


def test_row2_an_empty_holder_file_past_the_grace_is_taken_with_a_stale_line(tmp_path: Path, capsys):
    lock_path = tmp_path / "merge.lock"
    lock_path.write_bytes(b"")
    old = time.time() - 11
    os.utime(lock_path, (old, old))
    assert UNPARSABLE_GRACE_SECONDS == 10
    lock = MergeLock(lock_path)
    lock.acquire(max_age_seconds=30)
    try:
        assert f'"pid": {os.getpid()}' in lock_path.read_text(encoding="utf-8")
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert "stale" in out and "11 s" in out, out


_ROUND_SCRIPT = r"""
import sys, time
from pathlib import Path
from tools.agentops.merge import MergeLock
lock_path, log_path, ident, rounds = Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
lines = []
for _ in range(rounds):
    lock = MergeLock(lock_path)
    lock.acquire(max_age_seconds=25)  # worst holder 1.36 s over 20 runs under a 12-process burner (2026-10-01) x 18
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"enter {ident} {time.time():.6f}\n"); f.flush()
    time.sleep(0.2)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"exit {ident} {time.time():.6f}\n"); f.flush()
    lock.release()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"freed {ident} {time.time():.6f}\n")
"""


def test_row3_four_processes_twenty_rounds_never_overlap(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [subprocess.Popen([sys.executable, "-c", _ROUND_SCRIPT, str(lock_path),
                               str(tmp_path / f"rounds{i}.log"), str(i), "20"], cwd=REPO, env=env,
                              stderr=subprocess.PIPE, text=True) for i in range(4)]
    for p in procs:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
    open_at: dict[str, float] = {}
    intervals: list[tuple[float, float]] = []
    entered_at: dict[str, float] = {}
    holds: list[float] = []
    for line in "".join((tmp_path / f"rounds{i}.log").read_text(encoding="utf-8")
                        for i in range(4)).splitlines():
        kind, ident, t = line.split()
        if kind == "enter":
            open_at[ident] = float(t)
        elif kind == "exit":
            entered = open_at.pop(ident)
            intervals.append((entered, float(t)))
            entered_at[ident] = entered
        else:  # freed: the lock is released
            holds.append(float(t) - entered_at[ident])
    assert len(intervals) == 80
    intervals.sort()
    overlaps = sum(1 for a, b in zip(intervals, intervals[1:]) if b[0] < a[1])
    min_gap = min(b[0] - a[1] for a, b in zip(intervals, intervals[1:]))
    print(f"row 3: 80 intervals, {overlaps} overlapping, min gap {min_gap * 1000:.1f} ms, "
          f"worst holder (enter -> released) {max(holds):.2f} s")
    assert overlaps == 0
