"""#175: the busy-PC start hold keeps its count across a dispatcher restart.

Dispatcher A and B share one `admission_hold_file`; the wall clock (`timestamp`) is one injected value, each
dispatcher's own monotonic `clock` starts from an unrelated number. No real start, no real clock.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.agentops.dispatch as dispatch_mod
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue

CAP_S = 1800
BASE = datetime(2026, 10, 18, 11, 6, 0, tzinfo=timezone.utc)


class Wall:
    """One wall clock (UTC seconds since BASE) and the monotonic clock of each dispatcher read from it."""

    def __init__(self) -> None:
        self.s = 0.0

    def timestamp(self) -> datetime:
        return BASE + timedelta(seconds=self.s)

    def clock(self, offset: float):
        return lambda: self.s + offset


class _Held:
    go = False
    reason = "load"

    def line(self) -> str:
        return "PC busy 10.4 cores"


class PcBusy:
    """Holds until `waited_s` reaches the cap, then goes by the cap; records every `waited_s` it is given."""

    def __init__(self) -> None:
        self.waited: list[float] = []

    def __call__(self, previous_wait, waited_s: float = 0.0, hold_cap_s=None):
        self.waited.append(waited_s)
        if hold_cap_s is not None and waited_s >= hold_cap_s:
            return SimpleNamespace(go=True, reason="hold cap", line=lambda: "go: hold cap")
        return _Held()


class Gate:
    """A core update that never finishes within the tick (defers the start); `open()` ends the thread."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def __call__(self):
        self.event.wait(10)
        return "core update (auto): updated: 1 file(s) changed; core_version -> abc"


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
        real = dispatch_mod._load_settings
        monkeypatch.setattr(dispatch_mod, "_load_settings",
                            lambda: {**real(), "start_admission_hold_cap_s": CAP_S})
        self.tmp = tmp_path
        self.file = tmp_path / "runs" / "admission_hold.json"
        self.wall = Wall()
        self.gates: list[Gate] = []

    def make(self, offset: float, *, core_update=None, name: str = "d") -> tuple[Dispatcher, PcBusy, list[str], FakeClaude]:
        claude = FakeClaude()
        log: list[str] = []
        d = Dispatcher(claude=claude, gh=FakeGh(candidates=[_issue(1)]), max_hours=4.0,
                       events_file=self.tmp / f"events_{name}.jsonl",
                       state_file=self.tmp / f"state_{name}" / "orch_session.json",
                       projects_dir=self.tmp / "projects", finishing_dir=self.tmp / "finishing",
                       context_line=1_000_000, clock=self.wall.clock(offset), timestamp=self.wall.timestamp,
                       core_update=core_update, core_update_wait_s=0.05, core_root=self.tmp,
                       merge_lock_path=self.tmp / "var" / "locks" / "merge.lock", orch_offer_max=99,
                       admission_hold_file=self.file, log=log.append)
        pc = PcBusy()
        d.admission = pc
        return d, pc, log, claude

    def at(self, seconds: float) -> None:
        self.wall.s = seconds


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    for g in r.gates:
        g.event.set()


def test_row1_a_restart_keeps_the_count_and_the_cap_fires_on_wall_time(rig):
    a, pc_a, _, _ = rig.make(offset=0, name="a")
    rig.at(0)
    assert a._admission_go((1,)) is False
    rig.at(300)
    assert a._admission_go((1,)) is False
    assert pc_a.waited == [0.0, 300.0]

    b, pc_b, log_b, _ = rig.make(offset=987_654, name="b")  # unrelated monotonic start
    rig.at(1500)
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [1500.0]

    rig.at(1800)
    assert b._admission_go((1,)) is True
    assert pc_b.waited == [1500.0, 1800.0]
    assert any("start by hold cap" in m for m in log_b)


def test_row2_a_last_wait_older_than_the_cap_gives_a_count_of_zero(rig):
    a, _, _, _ = rig.make(offset=0, name="a")
    rig.at(0)
    assert a._admission_go((1,)) is False
    rig.at(CAP_S + 1)  # the file's last_wait_at (wall 0) is 1801 s old when B starts
    b, pc_b, _, _ = rig.make(offset=5000, name="b")
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [0.0]


def test_row3_a_go_clears_the_count_in_the_file(rig):
    a, _, _, _ = rig.make(offset=0, name="a")
    rig.at(0)
    a._admission_go((1,))
    saved = json.loads(rig.file.read_text(encoding="utf-8"))
    assert saved["wait_since"] is not None and saved["last_wait_at"] is not None

    a._admission_go((1,), priority=True)  # any go restarts the count
    saved = json.loads(rig.file.read_text(encoding="utf-8"))
    assert saved["wait_since"] is None and saved["last_wait_at"] is None and saved["released"] is False

    rig.at(60)
    b, pc_b, _, _ = rig.make(offset=1, name="b")
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [0.0]


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"wait_since": 5, "last_wait_at": "x", "released": true}', "",
])
def test_row3_a_corrupt_file_gives_zero_and_no_latch_and_one_log_line(rig, content):
    rig.file.parent.mkdir(parents=True)
    rig.file.write_text(content, encoding="utf-8")
    rig.at(600)
    b, pc_b, log_b, _ = rig.make(offset=42, name="b")
    assert b._admission_released is False
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [0.0]
    assert len([m for m in log_b if "admission hold" in m]) == 1


def test_row3_a_file_of_bytes_that_are_not_utf8_gives_zero_and_no_latch(rig):
    rig.file.parent.mkdir(parents=True)
    rig.file.write_bytes(bytes([255, 254, 0, 123]))
    rig.at(600)
    b, pc_b, log_b, _ = rig.make(offset=42, name="b")
    assert b._admission_released is False
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [0.0]
    assert len([m for m in log_b if "admission hold" in m]) == 1


def test_row3_a_time_in_the_future_gives_zero_and_no_latch(rig):
    future = (BASE + timedelta(seconds=900)).isoformat()
    rig.file.parent.mkdir(parents=True)
    rig.file.write_text(json.dumps({"wait_since": future, "last_wait_at": future, "released": True}),
                        encoding="utf-8")
    rig.at(600)
    b, pc_b, log_b, _ = rig.make(offset=42, name="b")
    assert b._admission_released is False
    assert b._admission_go((1,)) is False
    assert pc_b.waited == [0.0]
    assert any("admission hold" in m and "future" in m for m in log_b)


def test_a_missing_file_is_silent_and_a_write_error_never_raises(rig):
    b, _, log_b, _ = rig.make(offset=0, name="b")
    assert not [m for m in log_b if "admission hold" in m]
    rig.file.parent.mkdir(parents=True)
    rig.file.with_suffix(".tmp").mkdir()  # the temp path is a folder: the write fails
    rig.at(0)
    assert b._admission_go((1,)) is False  # no exception


def test_row4_the_cap_latch_survives_a_restart_and_clears_when_an_orch_starts(rig):
    gate = Gate()
    rig.gates.append(gate)
    a, pc_a, _, claude_a = rig.make(offset=0, core_update=gate, name="a")
    for minute in range(30):
        rig.at(minute * 60)
        a.tick()
    rig.at(CAP_S)
    a.tick()  # the cap fires; the running core update defers the start
    assert claude_a.started == [] and a._admission_released is True
    assert json.loads(rig.file.read_text(encoding="utf-8"))["released"] is True
    # A exits here, before any ORCH started

    b, pc_b, log_b, claude_b = rig.make(offset=555_555, name="b")
    assert b._admission_released is True
    rig.at(CAP_S + 60)
    b.tick()
    assert len(claude_b.started) == 1
    assert pc_b.waited == []  # no admission call, no new hold
    assert not [m for m in log_b if "start held" in m]
    assert json.loads(rig.file.read_text(encoding="utf-8"))["released"] is False
