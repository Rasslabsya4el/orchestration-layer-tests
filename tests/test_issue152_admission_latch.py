"""#152: a start the busy-PC wait already released is not held again by a core update.

Rows 1 and 2 of the issue, driven through `Dispatcher.tick`: the PC stays busy, the hold cap releases the
start at T0+30 min, a core update (still running, or waiting for the merge line) defers it, and the next
tick that finds the core taken starts the ORCH with no new `start held` line and no new admission call.
Also: the latch is cleared by a start and by a tick with no candidates; a plain go is asked again.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.agentops.dispatch as dispatch_mod
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue

TAKEN = "core update (auto): updated: 3 file(s) changed; core_version -> 5ebb24f0"
MINUTE = 60
CAP_S = 1800  # the hold cap the test settings carry (`start_admission_hold_cap_s`)


class Gate:
    """The core update callable: blocks until `open()` (still running, or waiting for the merge line)."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.event.wait(10)  # bounded: a test that forgets to open it still ends
        return TAKEN

    def open(self) -> None:
        self.event.set()


class _Held:
    go = False
    reason = "load"

    def line(self) -> str:
        return "PC busy 10.4 cores"


class PcBusy:
    """The load gate: while the PC is busy it holds until `waited_s` reaches the cap, then goes by the cap.
    With `free` set it goes at once with no reason (a plain go)."""

    def __init__(self) -> None:
        self.calls = 0
        self.free = False

    def __call__(self, previous_wait, waited_s: float = 0.0, hold_cap_s=None):
        self.calls += 1
        if self.free:
            return SimpleNamespace(go=True, reason="", line=lambda: "go")
        if hold_cap_s is not None and waited_s >= hold_cap_s:
            return SimpleNamespace(go=True, reason="hold cap",
                                   line=lambda: f"go: hold cap, waited {int(waited_s // 60)} min")
        return _Held()


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
        real = dispatch_mod._load_settings
        monkeypatch.setattr(dispatch_mod, "_load_settings",
                            lambda: {**real(), "start_admission_hold_cap_s": CAP_S})
        self.claude = FakeClaude()
        self.gh = FakeGh(candidates=[_issue(1)])
        self.clock = FakeClock()
        self.gate = Gate()
        self.pc = PcBusy()
        self.log: list[str] = []
        self.d = Dispatcher(claude=self.claude, gh=self.gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                            state_file=tmp_path / "state" / "orch_session.json", projects_dir=tmp_path / "projects",
                            finishing_dir=tmp_path / "finishing", context_line=1_000_000, clock=self.clock,
                            core_update=self.gate, merge_lock_path=tmp_path / "var" / "locks" / "merge.lock",
                            core_update_wait_s=0.05, core_root=tmp_path, orch_offer_max=99)
        self.d.admission = self.pc
        self.d.log = self.log.append

    def tick_at(self, seconds: float) -> None:
        self.clock.value = seconds
        self.d.tick()

    def started(self) -> int:
        return len(self.claude.started)

    def held_after(self, mark: int) -> list[str]:
        return [m for m in self.log[mark:] if "start held" in m]

    def cap_releases_after(self, mark: int) -> list[str]:
        return [m for m in self.log[mark:] if "start by hold cap" in m]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.gate.open()  # never leave the update thread waiting


def wait_out_the_cap(rig: Rig) -> int:
    """Ticks each minute from T0 while the PC is busy; the tick at T0+30 min is the cap's release, whose
    start the running core update defers. Returns the log index taken just before that release tick."""
    for minute in range(30):
        rig.tick_at(minute * MINUTE)
    assert rig.started() == 0 and rig.pc.calls == 30  # held every minute before the cap
    mark = len(rig.log)
    rig.tick_at(30 * MINUTE)  # release, deferred by the update still running
    assert rig.started() == 0
    assert len(rig.cap_releases_after(mark)) == 1
    assert rig.gate.calls == 1
    return mark


def test_row1_cap_releases_core_update_defers_then_next_tick_starts_with_no_new_hold(rig):
    mark = wait_out_the_cap(rig)
    calls = rig.pc.calls
    rig.tick_at(31 * MINUTE)  # deferred again, the update still running
    rig.tick_at(34 * MINUTE)
    assert rig.started() == 0
    assert rig.pc.calls == calls  # no admission call once released

    rig.gate.open()
    rig.d.core_update_wait_s = 5.0  # the release tick waits for the taken update
    rig.tick_at(35 * MINUTE)  # core update done, PC still busy

    assert rig.started() == 1
    assert rig.held_after(mark) == []  # no `start held` from the release on
    assert rig.pc.calls == calls
    assert rig.d._admission_released is False  # cleared by the start


def test_row2_two_deferrals_in_a_row_still_no_new_hold_and_the_start_comes_on_the_first_free_tick(rig):
    mark = wait_out_the_cap(rig)  # deferral 1: update running
    calls = rig.pc.calls
    rig.tick_at(31 * MINUTE)  # deferral 2: update still not taken (waiting for the merge line)
    assert rig.started() == 0
    assert rig.pc.calls == calls

    rig.gate.open()
    rig.d.core_update_wait_s = 5.0
    rig.tick_at(32 * MINUTE)  # the first tick after the core is taken

    assert rig.started() == 1
    assert rig.gate.calls == 1  # one update job, no second run
    assert rig.held_after(mark) == []
    assert rig.pc.calls == calls


def test_the_latch_is_cleared_by_a_start_so_the_next_start_is_held_again(rig):
    wait_out_the_cap(rig)
    rig.gate.open()
    rig.d.core_update_wait_s = 5.0
    rig.tick_at(31 * MINUTE)
    assert rig.started() == 1
    assert rig.d._admission_released is False

    rig.d.sessions[0].finishing = True  # the ORCH ends; a further start is wanted
    rig.gh._candidates = [_issue(2)]
    calls = rig.pc.calls
    mark = len(rig.log)
    rig.tick_at(32 * MINUTE)

    assert rig.pc.calls == calls + 1  # the admission is asked again
    assert rig.started() == 1
    assert len(rig.held_after(mark)) == 1  # a fresh PC-busy hold


def test_the_latch_is_cleared_by_a_tick_with_no_candidates(rig):
    wait_out_the_cap(rig)
    assert rig.d._admission_released is True

    rig.gh._candidates = []  # no issue ready any more
    rig.tick_at(31 * MINUTE)
    assert rig.d._admission_released is False

    rig.gh._candidates = [_issue(2)]
    calls = rig.pc.calls
    mark = len(rig.log)
    rig.tick_at(32 * MINUTE)

    assert rig.pc.calls == calls + 1  # asked again: a new hold, not the old release
    assert rig.started() == 0
    assert len(rig.held_after(mark)) == 1


def test_a_plain_go_deferred_by_the_core_update_is_asked_again_on_the_next_tick(rig):
    rig.pc.free = True  # a plain go, not a hold-cap go
    rig.tick_at(0)
    assert rig.gate.calls == 1 and rig.started() == 0
    assert rig.d._admission_released is False

    calls = rig.pc.calls
    rig.tick_at(MINUTE)
    assert rig.pc.calls == calls + 1  # no latch from a plain go


def test_a_priority_go_does_not_latch(rig):
    assert rig.d._admission_go((1,), priority=True) is True
    assert rig.d._admission_released is False
