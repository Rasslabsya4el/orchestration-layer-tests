"""#34: the dispatcher's tick does not stand still while the auto core update waits for the merge line.

Rows 1-3 of the issue, a fake merge-lock holder (a real `MergeLock` held by the test, its progress file
touched) under a `core_update` that commits through `merge_then_checkout_locked` like `_commit_and_push`.
Row 4 is the issue's own acceptance command.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from tools.agentops.dispatch import Dispatcher, TICK_SECONDS
from tools.agentops.merge import MergeLock, merge_then_checkout_locked

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue
from tests._area_rule import _no_area_rule  # noqa: F401

WAIT_BUDGET_S = 0.5  # the tick's bounded wait for the core update, short for the test


class Holder:
    """Another process's merge: holds `var/locks/merge.lock`, names a progress file it keeps touching."""

    def __init__(self, lock_path: Path) -> None:
        self.lock = MergeLock(lock_path)
        self.progress = lock_path.parent / "suite.out"

    def take(self) -> None:
        self.lock.acquire()
        self.progress.write_text("suite running\n", encoding="utf-8")
        self.lock.set_progress(self.progress)

    def touch(self) -> None:
        os.utime(self.progress, None)

    def release(self) -> None:
        self.lock.release()


def _build(tmp_path: Path, core_update):
    lock_path = tmp_path / "var" / "locks" / "merge.lock"
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   finishing_dir=tmp_path / "finishing", context_line=1_000_000, clock=FakeClock(),
                   core_update=core_update, merge_lock_path=lock_path, core_update_wait_s=WAIT_BUDGET_S)
    log: list[str] = []
    d.log = log.append
    return claude, gh, d, log, lock_path


def _committing_update(lock_path: Path, commits: list[str]):
    """The shape of `_commit_and_push`: the commit happens only under the merge line."""

    def core_update() -> str:
        with merge_then_checkout_locked(lock_path):
            commits.append("Core update: 5ebb24f0")
        return "committed"

    return core_update


def _merge_line_lines(log: list[str]) -> list[str]:
    return [m for m in log if "merge line" in m]


@pytest.fixture
def stepped(monkeypatch):
    """Records that the duties after the start (re-pool, owned pids) ran in the tick."""
    ran: list[str] = []
    from tools.agentops import dispatch as mod

    real_track, real_pids = mod.Dispatcher._track_ready_waits, mod.Dispatcher._maybe_update_owned_pids

    def track(self, report):
        ran.append("track")
        return real_track(self, report)

    def pids(self):
        ran.append("pids")
        return real_pids(self)

    monkeypatch.setattr(mod.Dispatcher, "_track_ready_waits", track)
    monkeypatch.setattr(mod.Dispatcher, "_maybe_update_owned_pids", pids)
    return ran


def test_row1_tick_returns_while_the_merge_line_is_held_start_deferred_one_line(tmp_path: Path, stepped):
    commits: list[str] = []
    lock_path = tmp_path / "var" / "locks" / "merge.lock"
    claude, gh, d, log, _ = _build(tmp_path, _committing_update(lock_path, commits))
    holder = Holder(lock_path)
    holder.take()
    try:
        began = time.monotonic()
        report = d.tick()
        holder.touch()
        took = time.monotonic() - began
        d.tick()  # the line is still held: no second line
        d.tick()
    finally:
        holder.release()
        _drain(d)

    assert took < TICK_SECONDS
    assert claude.started == [] and report.started is None
    lines = _merge_line_lines(log)
    assert len(lines) == 1 and f"pid {os.getpid()}" in lines[0]
    assert stepped.count("track") == 3 and stepped.count("pids") == 3  # the rest of the tick ran


def test_row2_when_the_line_frees_the_core_update_commits_and_orch_starts_within_2_ticks(tmp_path: Path, stepped):
    commits: list[str] = []
    lock_path = tmp_path / "var" / "locks" / "merge.lock"
    claude, gh, d, log, _ = _build(tmp_path, _committing_update(lock_path, commits))
    holder = Holder(lock_path)
    holder.take()
    try:
        d.tick()
        assert claude.started == [] and commits == []
    finally:
        holder.release()

    ticks = 0
    while not claude.started and ticks < 2:
        d.tick()
        ticks += 1

    assert len(claude.started) == 1 and ticks <= 2
    assert commits == ["Core update: 5ebb24f0"]  # committed once, before the start
    assert len(_merge_line_lines(log)) == 1


def test_row3_a_core_update_needing_no_commit_starts_orch_on_the_same_tick(tmp_path: Path, stepped):
    calls: list[int] = []

    def no_commit() -> str:
        calls.append(1)
        return "core update (auto): updated: 0 file(s) changed"

    claude, gh, d, log, lock_path = _build(tmp_path, no_commit)
    holder = Holder(lock_path)
    holder.take()  # the line is held, but this update never asks for it
    try:
        report = d.tick()
    finally:
        holder.release()

    assert calls == [1] and len(claude.started) == 1 and report.started is not None
    assert _merge_line_lines(log) == []


def test_a_failed_core_update_is_logged_and_never_blocks_the_start(tmp_path: Path):
    def boom() -> str:
        raise RuntimeError("home unreadable")

    claude, gh, d, log, _ = _build(tmp_path, boom)

    d.tick()

    assert len(claude.started) == 1
    assert any("core update before orch start failed" in m and "home unreadable" in m for m in log)


def test_a_deferral_with_no_merge_line_holder_says_the_holder_is_unknown(tmp_path: Path, stepped):
    gate = threading.Event()

    def blocking() -> str:
        gate.wait(10)
        return "committed"

    claude, gh, d, log, _ = _build(tmp_path, blocking)  # no lock file: no holder
    try:
        d.tick()
        d.tick()  # still blocked: no second line
    finally:
        gate.set()
        _drain(d)

    assert claude.started == []
    lines = _merge_line_lines(log)
    assert len(lines) == 1 and "merge line" in lines[0] and "unknown" in lines[0]


def test_a_finished_failed_core_update_no_start_used_is_logged_once_and_cleared(tmp_path: Path, stepped):
    gate = threading.Event()

    def failing() -> str:
        gate.wait(10)
        raise RuntimeError("home unreadable")

    claude, gh, d, log, _ = _build(tmp_path, failing)
    d.tick()  # the update waits on the gate: the start is deferred
    gate.set()
    job = d._core_update_job
    assert job is not None and job.done.wait(10)

    gh._candidates = []  # nothing eligible on the next tick: no start reaches the update
    d.tick()

    assert claude.started == []
    errors = [m for m in log if "core update before orch start failed (start not taken this tick)" in m]
    assert len(errors) == 1 and "home unreadable" in errors[0]
    assert d._core_update_job is None
    d.tick()
    assert len([m for m in log if "core update before orch start failed" in m]) == 1


def _drain(d) -> None:
    """Lets the update thread of a test end before the temp dir goes."""
    job = getattr(d, "_core_update_job", None)
    if job is not None:
        job.done.wait(10)
