"""#779 unit 2: the hold engine, over a FAKE process table (no real process is touched, bar one spawned child)."""

import json
import subprocess
import sys
import time

import pytest

from tools.agentops import hold

psutil = pytest.importorskip("psutil", reason="the hold tests need psutil, which is not installed")


class FakeTable:
    def __init__(self):
        self.procs = {}  # pid -> {"ct", "parent", "cmd"}
        self.suspended = []
        self.resumed = []

    def add(self, pid, parent=None, cmd="x", ct=None):
        self.procs[pid] = {"ct": ct if ct is not None else 1000.0 + pid, "parent": parent, "cmd": cmd}

    def create_time(self, pid):
        return self.procs[pid]["ct"] if pid in self.procs else None

    def children(self, pid):
        return [p for p, v in self.procs.items() if v["parent"] == pid]

    def parent(self, pid):
        return self.procs.get(pid, {}).get("parent")

    def cmdline(self, pid):
        return self.procs[pid]["cmd"]

    def all_pids(self):
        return list(self.procs)

    def suspend(self, pid):
        self.suspended.append(pid)

    def resume(self, pid):
        self.resumed.append(pid)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "repo"
    (root / "var").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "hold_protected.json").write_text('[["jobs.py", "daemon"]]', encoding="utf-8")
    t = FakeTable()
    t.add(1, cmd="desktop app")
    t.add(2, 1, cmd="interactive claude")           # interactive session (the caller's ancestor)
    t.add(3, 2, cmd="python -m tools.agentops machine play")  # the caller
    t.add(10, 1, cmd="claude bg ORCH")               # background session
    t.add(11, 10, cmd="pytest")
    t.add(12, 11, cmd="pytest worker")
    t.add(20, 1, cmd="python -m tools.agentops dispatch --loop")  # dispatcher loop
    t.add(21, 1, cmd="python scripts/jobs.py daemon")             # runs daemon loop
    t.add(30, 1, cmd="claude bg elsewhere")          # a background session of another repo
    t.add(31, 30, cmd="pytest")
    t.add(40, 1, cmd="merge suite")                  # merge lock holder
    t.add(41, 40, cmd="suite child")
    sessions = [
        {"pid": 10, "kind": "background", "cwd": str(root)},
        {"pid": 30, "kind": "background", "cwd": str(root.parent / "other")},
        {"pid": 2, "kind": "interactive", "cwd": str(root)},
    ]
    return root, t, sessions


def run(world, mode, **kw):
    root, t, sessions = world
    return hold.keeper_pass(mode, root, table=t, sessions=lambda: sessions, caller=3, **kw)


def test_suspended_set_exact_and_excluded_never_touched(world):
    root, t, _ = world
    (root / "var" / "locks").mkdir()
    (root / "var" / "locks" / "merge.lock").write_text(json.dumps({"pid": 40, "started": "x"}))
    done = run(world, "play")
    assert set(done["suspended"]) == {10, 11, 12, 40, 41}
    assert t.suspended[-1] in (10, 40) and t.suspended.index(12) < t.suspended.index(10)  # children first
    for never in (1, 2, 3, 20, 21, 30, 31):
        assert never not in t.suspended


def test_idempotent_and_record_survives(world):
    root, t, _ = world
    run(world, "play")
    first = list(t.suspended)
    assert run(world, "play")["suspended"] == []
    assert t.suspended == first
    assert {e["pid"] for e in hold.read_record(root)} == {10, 11, 12}


def test_reused_pid_is_skipped(world):
    root, t, _ = world
    run(world, "play")
    t.procs[11]["ct"] += 500  # pid 11 died and the number was reused by someone else
    done = run(world, "normal")
    assert set(done["resumed"]) == {10, 12}
    assert hold.read_record(root) == []


def test_normal_resumes_only_the_record(world):
    root, t, _ = world
    assert run(world, "normal")["resumed"] == []  # nothing recorded: nothing resumed
    run(world, "play")
    done = run(world, "normal")
    assert set(done["resumed"]) == {10, 11, 12}
    assert set(t.resumed) == {10, 11, 12}
    assert run(world, "normal")["resumed"] == []


def test_child_spawned_later_is_suspended_by_next_pass(world):
    root, t, _ = world
    run(world, "play")
    t.add(13, 12, cmd="late child")
    done = run(world, "play")
    assert done["suspended"] == [13]
    assert {e["pid"] for e in hold.read_record(root)} == {10, 11, 12, 13}


def test_bad_mode_raises(world):
    with pytest.raises(ValueError):
        run(world, "sleep")


def test_real_child_freeze_and_thaw(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        table = hold.PsutilTable()
        import psutil
        ct = psutil.Process(child.pid).create_time()
        table.suspend(child.pid)
        deadline = time.time() + 5
        while psutil.Process(child.pid).status() != psutil.STATUS_STOPPED and time.time() < deadline:
            time.sleep(0.1)
        assert psutil.Process(child.pid).status() == psutil.STATUS_STOPPED
        table.resume(child.pid)
        time.sleep(0.2)
        assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
        assert table.create_time(child.pid) == ct
    finally:
        child.kill()
        child.wait()
