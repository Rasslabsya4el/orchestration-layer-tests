"""#615 unit 1: the merge lock's waiting line (tickets carry info, a holder claims and posts)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.agentops.merge import MergeLock, MergeLockTimeout

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

# argv: lock, ticket_info json ("" = none), ceiling, log
_WAIT = r"""
import sys, time, json
from pathlib import Path
from tools.agentops.merge import MergeLock, MergeLockTimeout
lock = MergeLock(Path(sys.argv[1]))
info = json.loads(sys.argv[2]) if sys.argv[2] else None
try:
    res = lock.acquire(max_age_seconds=float(sys.argv[3]), ticket_info=info)
except MergeLockTimeout:
    print("timeout", flush=True)
    sys.exit(0)
if res is None:
    print("held", flush=True)
    with open(sys.argv[4], "a") as f:
        f.write(sys.argv[5] + "\n")
    time.sleep(0.2)
    lock.release()
else:
    print("result " + json.dumps(res), flush=True)
"""


def _spawn(script, *args, stdout=subprocess.PIPE):
    return subprocess.Popen([sys.executable, "-c", script, *map(str, args)], cwd=REPO, env=ENV,
                            stdout=stdout, stderr=subprocess.PIPE, text=True)


def _queue(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".queue")


def _batch(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".batch")


def _wait_queued(lock_path: Path, n: int, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = _queue(lock_path)
        if q.exists() and len(list(q.iterdir())) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"fewer than {n} tickets queued")


def _waiter(lock_path, info, ceiling=60, log="x.log", name="w"):
    return _spawn(_WAIT, lock_path, json.dumps(info) if info is not None else "", ceiling, log, name)


def _holder(tmp_path: Path) -> MergeLock:
    lock = MergeLock(tmp_path / "merge.lock")
    assert lock.acquire(max_age_seconds=60) is None
    return lock


def _spawn_merges(lock_path: Path, names, log):
    procs = []
    for i, b in enumerate(names):
        procs.append(_waiter(lock_path, {"kind": "merge", "branch": b}, log=log, name=b))
        _wait_queued(lock_path, i + 1)
    return procs


def _finish(procs):
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outs.append(out)
    return outs


def test_waiting_line_is_in_arrival_order(tmp_path: Path):
    lock = _holder(tmp_path)
    procs = _spawn_merges(lock.path, ["b1", "b2", "b3"], tmp_path / "l.log")
    try:
        assert [w.info["branch"] for w in lock.waiting_line()] == ["b1", "b2", "b3"]
    finally:
        lock.release()
        _finish(procs)


def test_barrier_ticket_stops_the_line(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    p1 = _waiter(lock.path, {"kind": "merge", "branch": "b1"}, log=log, name="b1")
    _wait_queued(lock.path, 1)
    barrier = _waiter(lock.path, None, log=log, name="bar")
    _wait_queued(lock.path, 2)
    p2 = _waiter(lock.path, {"kind": "merge", "branch": "b2"}, log=log, name="b2")
    _wait_queued(lock.path, 3)
    try:
        assert [w.info["branch"] for w in lock.waiting_line()] == ["b1"]
    finally:
        lock.release()
        _finish([p1, barrier, p2])


def test_posted_results_reach_every_waiter_and_the_lock_is_never_theirs(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    procs = _spawn_merges(lock.path, ["b1", "b2", "b3"], log)
    line = lock.claim(lock.waiting_line())
    assert len(line) == 3
    for w in line:
        lock.post_result(w, {"rc": 0, "sha": "abc"})
    lock.release()
    outs = _finish(procs)
    for out in outs:
        assert out.strip() == 'result {"rc": 0, "sha": "abc"}'
        assert "held" not in out
    assert not log.exists()
    assert list(_batch(lock.path).glob("*.claim")) == []
    assert list(_batch(lock.path).glob("*.result")) == []


def test_unclaimed_waiters_take_the_lock_in_order(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    procs = _spawn_merges(lock.path, ["b1", "b2"], log)
    line = lock.claim(lock.waiting_line())
    assert len(line) == 2
    lock.unclaim(line)
    lock.release()
    outs = _finish(procs)
    assert all(o.strip() == "held" for o in outs)
    assert log.read_text().split() == ["b1", "b2"]


def test_a_claim_naming_a_dead_pid_is_dropped(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    procs = _spawn_merges(lock.path, ["b1"], log)
    w = lock.waiting_line()[0]
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    claim = _batch(lock.path) / f"{w.ticket}.claim"
    claim.write_text(json.dumps({"pid": dead.pid, "token": "x", "started": "2020-01-01 00:00:00"}))
    lock.release()
    outs = _finish(procs)
    assert outs[0].strip() == "held"
    assert not claim.exists()


def _first_waiting(lock, claim: bool = False):
    # bounded wait for the line to show a waiter (a bare `[0]` raced the machine's load)
    deadline = time.time() + 120
    while True:
        line = lock.waiting_line()
        if line:
            return lock.claim(line)[0] if claim else line[0]
        assert time.time() < deadline, "no waiter in the line within 120 s"
        time.sleep(0.02)


def test_claimed_waiter_outlives_the_age_ceiling_unclaimed_does_not(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    # #630: the holder's age counts only once the waiter is in line. Its start-up under a full
    # suite's load can take longer than the ceiling, so the holder heartbeats until the ticket
    # shows. `started` is whole seconds (`util.now`), so the age read just after a heartbeat is
    # already 0-1 s: the ceiling is 2 s, not 1, and the age passes it during the 3 s sleep below.
    claimed = _waiter(lock.path, {"kind": "merge", "branch": "b1"}, ceiling=2, log=log, name="b1")
    deadline = time.time() + 120
    while time.time() < deadline:
        lock.heartbeat()
        q = _queue(lock.path)
        if q.exists() and list(q.iterdir()):
            break
        time.sleep(0.02)
    w0 = _first_waiting(lock, claim=True)
    claim_file = _batch(lock.path) / f"{w0.ticket}.claim"
    deadline = time.time() + 120
    while not claim_file.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert claim_file.exists()
    time.sleep(3.0)
    w = _first_waiting(lock)
    lock.post_result(w, {"rc": 0})
    out, err = claimed.communicate(timeout=120)
    assert claimed.returncode == 0, err
    assert out.strip() == 'result {"rc": 0}'
    # the same ceiling without a claim: today's timeout (age counts from the holder's start)
    plain = _waiter(lock.path, {"kind": "merge", "branch": "b2"}, ceiling=2, log=log, name="b2")
    out, err = plain.communicate(timeout=120)
    assert plain.returncode == 0, err
    assert out.strip() == "timeout"
    lock.release()


def test_heartbeat_moves_started_forward(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        before = json.loads(lock.path.read_text())
        deadline = time.time() + 120
        while True:
            time.sleep(0.2)
            lock.heartbeat()
            after = json.loads(lock.path.read_text())
            if after["started"] > before["started"] or time.time() > deadline:
                break
        assert after["started"] > before["started"]
        assert (after["pid"], after["token"]) == (before["pid"], before["token"])
        lock.assert_held()
    finally:
        lock.release()


def test_release_without_posting_strands_no_claim(tmp_path: Path):
    lock = _holder(tmp_path)
    log = tmp_path / "l.log"
    procs = _spawn_merges(lock.path, ["b1", "b2"], log)
    assert len(lock.claim(lock.waiting_line())) == 2
    assert len(list(_batch(lock.path).glob("*.claim"))) == 2
    lock.release()
    outs = _finish(procs)  # the waiters, freed of their claims, take the lock themselves
    assert all(o.strip() == "held" for o in outs)
    assert list(_batch(lock.path).glob("*.claim")) == []


# --- #658: a commit that needs no suite is passed over by the holder's waiting line ---

_DEAD_PID = 2_000_000_000


def _put(lock_path: Path, seq: int, info, pid: int | None = None) -> str:
    q = _queue(lock_path)
    q.mkdir(parents=True, exist_ok=True)
    name = f"{seq:020d}-{os.getpid() if pid is None else pid}-{seq}"
    (q / name).write_text(json.dumps(info), encoding="utf-8")
    return name


def _merge(b):
    return {"kind": "merge", "branch": b}


_DOCS_COMMIT = {"kind": "commit", "needs_suite": False}
_CODE_COMMIT = {"kind": "commit", "needs_suite": True}


def _line(lock: MergeLock) -> list[str]:
    return [w.info["branch"] for w in lock.waiting_line()]


def test_row1_docs_only_commit_is_passed_over_and_stays_queued(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock.path, 1, _merge("B0"))
        commit = _put(lock.path, 2, _DOCS_COMMIT)
        _put(lock.path, 3, _merge("B"))
        assert _line(lock) == ["B0", "B"]
        assert (_queue(lock.path) / commit).exists()
        claimed = lock.claim(lock.waiting_line())
        assert commit not in [w.ticket for w in claimed]
        assert not (_batch(lock.path) / f"{commit}.claim").exists()
    finally:
        lock.release()


def test_row2_code_commit_is_a_wall(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock.path, 1, _CODE_COMMIT)
        _put(lock.path, 2, _merge("B"))
        assert _line(lock) == []
    finally:
        lock.release()


def test_row3_empty_ticket_is_a_wall(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock.path, 1, {})
        _put(lock.path, 2, _merge("B"))
        assert _line(lock) == []
    finally:
        lock.release()


def test_row4_the_1455_line_forms_one_batch(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        seq = iter(range(1, 100))
        for _ in range(2):
            _put(lock.path, next(seq), _CODE_COMMIT, pid=_DEAD_PID)
        _put(lock.path, next(seq), _merge("orch-636"))
        c1 = _put(lock.path, next(seq), _DOCS_COMMIT)
        expected = ["orch-636"]
        for b in ["wt-621-g", "orch-650", "orch-601-u2", "orch-630"]:
            _put(lock.path, next(seq), _merge(b)); expected.append(b)
        _put(lock.path, next(seq), _DOCS_COMMIT, pid=_DEAD_PID)
        c2 = _put(lock.path, next(seq), _DOCS_COMMIT)
        for b in ["orch-654", "orch-644", "orch-650", "orch-655"]:
            _put(lock.path, next(seq), _merge(b)); expected.append(b)
        _put(lock.path, next(seq), _CODE_COMMIT, pid=_DEAD_PID)
        c3 = _put(lock.path, next(seq), _DOCS_COMMIT)
        for b in ["orch-628", "orch-633"]:
            _put(lock.path, next(seq), _merge(b)); expected.append(b)
        assert len(expected) == 11  # 10 branches + the duplicate orch-650 ticket
        assert _line(lock) == expected
        queued = sorted(p.name for p in _queue(lock.path).iterdir())
        assert [n for n in queued if n in (c1, c2, c3)] == [c1, c2, c3]
        lock.claim(lock.waiting_line())
        assert not any((_batch(lock.path) / f"{c}.claim").exists() for c in (c1, c2, c3))
    finally:
        lock.release()


@pytest.mark.parametrize("path,needs", [("docs/STATE.md", False), ("src/pkg/x.py", True)])
def test_run_commit_ticket_carries_kind_and_needs_suite(tmp_path: Path, monkeypatch, path, needs):
    from tools.agentops import merge

    seen = {}

    class Stop(Exception):
        pass

    def fake_acquire(self, max_age_seconds=None, *, ticket_info=None):
        seen["info"] = ticket_info
        raise Stop

    monkeypatch.setattr(merge.MergeLock, "acquire", fake_acquire)
    monkeypatch.setattr(merge, "_core_commit_refusal", lambda cwd, paths: "")  # tmp_path is no git repo (#1116)
    with pytest.raises(Stop):
        merge.run_commit([path], "m", cwd=tmp_path, lock_path=tmp_path / "l.lock",
                         suite_cmd="true", suite_env={}, suite_shared=False,
                         suite_output_dir=tmp_path / "o", merge_log_path=tmp_path / "m.log")
    info = seen["info"]
    assert info["kind"] == "commit" and info["needs_suite"] == needs
    assert info["message"] == "m" and info["issues"] == []


def test_line_poll_log_names_the_barrier(tmp_path: Path):
    from tools.agentops.merge import _append_line_poll_log

    lock = _holder(tmp_path)
    try:
        _put(lock.path, 1, _merge("B0"))
        t = _put(lock.path, 2, {})
        _put(lock.path, 3, _merge("B"))
        polled = lock.waiting_line()
        assert [w.info["branch"] for w in polled] == ["B0"]
        log = tmp_path / "merge.log"
        _append_line_poll_log("A", lock.line_stop, ["X"], 1, log)
        text = log.read_text(encoding="utf-8")
        assert f"stopped at {t} {{}}" in text and "filter dropped 1: X" in text
    finally:
        lock.release()
