"""#779 unit 10: the keeper pass runs on its own cadence inside the dispatcher process, independent of the 30 s tick
and of the slow `claude agents --json --all` listing. Fake process table, fake mode, short intervals: no real
process is touched."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher
from tests.test_hold import FakeTable
from tools.agentops import hold

WAIT = 5.0


def wait_for(pred, limit: float = WAIT) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "repo"
    (root / "var").mkdir(parents=True)
    t = FakeTable()
    t.add(1, cmd="desktop app")
    t.add(10, 1, cmd="claude bg ORCH")
    t.add(11, 10, cmd="pytest")
    t.add(20, 1, cmd="python -m tools.agentops dispatch")
    sessions = [{"pid": 10, "kind": "background", "cwd": str(root)}]
    mode = ["play"]
    keepers: list[hold.Keeper] = []

    def make(lister=None, **kw) -> hold.Keeper:
        k = hold.Keeper(root, interval=0.02, mode_reader=lambda: mode[0], table=t,
                        lister=lister or (lambda: sessions), sessions_refresh=0.05, **kw)
        keepers.append(k)
        return k

    yield root, t, mode, make
    for k in keepers:
        k.stop()


def test_spawn_into_frozen_tree_is_suspended_by_next_keeper_pass_with_no_tick(world):
    root, t, mode, make = world
    k = make()
    k.start()
    assert wait_for(lambda: {10, 11} <= set(t.suspended))
    t.add(12, 11, cmd="late child")  # appears in the frozen tree; no dispatcher tick exists in this test
    assert wait_for(lambda: 12 in t.suspended)
    time.sleep(0.2)  # many more passes
    assert sorted(t.suspended) == [10, 11, 12]  # each suspended exactly once
    for never in (1, 20):
        assert never not in t.suspended


def test_normal_resumes_what_the_record_holds_and_only_once(world):
    root, t, mode, make = world
    k = make()
    k.start()
    assert wait_for(lambda: {10, 11} <= set(t.suspended))
    mode[0] = "normal"
    assert wait_for(lambda: {10, 11} <= set(t.resumed))
    time.sleep(0.2)
    assert sorted(t.resumed) == [10, 11]
    assert hold.read_record(root) == []


def test_slow_session_listing_does_not_block_passes(world):
    root, t, mode, make = world
    (root / "var" / "agentops" / "hold_roots").mkdir(parents=True)
    (root / "var" / "agentops" / "hold_roots" / "suite.json").write_text(json.dumps([10]), encoding="utf-8")
    release = threading.Event()
    listing_started = threading.Event()

    def stuck_lister():
        listing_started.set()
        release.wait(30)
        return []

    k = make(lister=stuck_lister)
    k.start()
    assert listing_started.wait(WAIT)  # the first pass woke the refresher; it is now stuck inside the listing
    assert wait_for(lambda: {10, 11} <= set(t.suspended))  # the process-table part never waited for it
    t.add(12, 11, cmd="late child")
    assert wait_for(lambda: 12 in t.suspended)  # ... and passes keep running while the listing is stuck
    release.set()


def test_slow_tick_does_not_delay_the_keeper(world, tmp_path, monkeypatch):
    root, t, mode, make = world
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=[]))
    d.log = lambda m: None
    tick_entered, tick_release = threading.Event(), threading.Event()

    def slow_tick():
        tick_entered.set()
        tick_release.wait(30)

    d.tick = slow_tick
    d.keeper = make()
    d.keeper.start()
    loop = threading.Thread(target=lambda: d.run(ticks=1), daemon=True)
    loop.start()
    try:
        assert tick_entered.wait(WAIT)  # the dispatcher is inside a tick that does not return
        assert wait_for(lambda: {10, 11} <= set(t.suspended))
        t.add(12, 11, cmd="late child")
        assert wait_for(lambda: 12 in t.suspended)
    finally:
        tick_release.set()
        loop.join(WAIT)


def test_tick_skips_its_own_pass_while_the_keeper_thread_lives(world, tmp_path, monkeypatch):
    root, t, mode, make = world
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
    calls: list[str] = []
    monkeypatch.setattr(hold, "keeper_pass", lambda m, root=None, **kw: calls.append(m) or {})
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=[]))
    d.log = lambda m: None
    assert d._machine_mode_and_keep() == "normal" and calls == ["normal"]  # no keeper: the tick keeps working
    d.keeper = make()
    d.keeper.start()
    assert d._machine_mode_and_keep() == "normal" and calls == ["normal"]  # a live keeper does the passes
    d.keeper.stop()
    assert not d.keeper.alive()
    assert d._machine_mode_and_keep() == "normal" and calls == ["normal", "normal"]  # a dead one: the tick again


def test_no_double_suspend_with_concurrent_passes(world):
    root, t, mode, make = world
    k = make()
    k.start()
    sessions = [{"pid": 10, "kind": "background", "cwd": str(root)}]
    threads = [threading.Thread(target=lambda: [hold.keeper_pass("play", root, table=t, sessions=lambda: sessions)
                                                for _ in range(20)]) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(WAIT)
    assert wait_for(lambda: {10, 11} <= set(t.suspended))
    assert sorted(t.suspended) == [10, 11]
    assert {e["pid"] for e in hold.read_record(root)} == {10, 11}


def test_unreadable_mode_runs_no_pass_and_the_thread_survives(world):
    root, t, mode, make = world
    logs: list[str] = []
    k = make(log=logs.append)

    def bad():
        raise OSError("no file")

    k._mode_reader = bad
    k.start()
    assert wait_for(lambda: any("unreadable" in m for m in logs))
    time.sleep(0.1)
    assert t.suspended == [] and k.alive()
    assert sum("unreadable" in m for m in logs) == 1  # logged once, not every pass


def test_stop_ends_both_threads(world):
    root, t, mode, make = world
    k = make()
    k.start()
    assert k.alive()
    k.stop()
    assert not k.alive()
    assert k.sessions._thread is not None and not k.sessions._thread.is_alive()


def test_root_lock_excludes_another_handle(tmp_path):
    """The lock is a file lock too: another process (the `machine normal` command) cannot pass while a pass runs."""
    with hold._root_lock(tmp_path):
        with open(tmp_path / "var" / "agentops" / "hold.lock", "a+b") as other:
            with pytest.raises(OSError):
                hold._lock_byte(other)
    with open(tmp_path / "var" / "agentops" / "hold.lock", "a+b") as other:
        hold._lock_byte(other)  # free again
