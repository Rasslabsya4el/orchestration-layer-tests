"""#399 unit 1: the merge lock serves waiters in arrival order (FIFO ticket)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from timing_bound import bound

from tools.agentops.merge import MergeLock

REPO = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(REPO)}

_HOLD = r"""
import sys, time
from pathlib import Path
from tools.agentops.merge import MergeLock
lock = MergeLock(Path(sys.argv[1])); lock.acquire(max_age_seconds=60)
print("held", flush=True)
time.sleep(float(sys.argv[2]))
lock.release()
"""

_WAIT = r"""
import sys, time
from pathlib import Path
from tools.agentops.merge import MergeLock
lock = MergeLock(Path(sys.argv[1])); lock.acquire(max_age_seconds=60)
with open(sys.argv[2], "a") as f:
    f.write(sys.argv[3] + "\n")
time.sleep(0.1)
lock.release()
"""


def _spawn(script, *args, stdout=None):
    return subprocess.Popen([sys.executable, "-c", script, *map(str, args)], cwd=REPO, env=ENV,
                            stdout=stdout, stderr=subprocess.PIPE, text=True)


def _queued(lock_path: Path) -> int:
    q = lock_path.with_name(lock_path.name + ".queue")
    return len(list(q.iterdir())) if q.exists() else 0


def test_waiters_get_the_lock_in_arrival_order_20_rounds(tmp_path: Path):
    a_first = 0
    for i in range(20):
        lock_path = tmp_path / f"r{i}.lock"
        log = tmp_path / f"r{i}.log"
        h = _spawn(_HOLD, lock_path, 1.6, stdout=subprocess.PIPE)
        assert h.stdout.readline().strip() == "held"
        a = _spawn(_WAIT, lock_path, log, "A")
        deadline = time.time() + 20
        while _queued(lock_path) < 1 and time.time() < deadline:  # the holder's ticket is gone once it holds
            time.sleep(0.02)
        time.sleep(0.3)
        b = _spawn(_WAIT, lock_path, log, "B")
        for p in (h, a, b):
            _, err = p.communicate(timeout=60)
            assert p.returncode == 0, err
        order = log.read_text().split()
        assert sorted(order) == ["A", "B"]
        a_first += order[0] == "A"
    assert a_first == 20


def test_a_dead_waiters_ticket_does_not_block_the_next_waiter(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    q = tmp_path / "merge.lock.queue"
    q.mkdir()
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    (q / f"{1:020d}-{p.pid}-0").write_text("")  # oldest ticket, its process is gone
    t0 = time.monotonic()
    lock = MergeLock(lock_path)
    lock.acquire(max_age_seconds=30)
    lock.release()
    elapsed = time.monotonic() - t0
    bound_s = bound(3.0, 20.0)  # max(floor, factor x CPU spin) (#826)
    assert elapsed < bound_s, (elapsed, bound_s)
    assert list(q.iterdir()) == []
