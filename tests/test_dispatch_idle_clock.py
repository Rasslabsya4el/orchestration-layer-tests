"""#766: the idle clock. A live session the listing shows idle, holding claims and running no subagent,
is never left alone longer than 10 min: not finishing -> one wake at 10 min naming what it holds, ended
10 min after that wake if still idle; finishing -> ended at 10 min. Fake clock (and fake wall clock), a
fake `claude.exe` and a fake `gh`: no real process. Rows 1-5 of the issue's table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _events, _issue, _subagent
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
STEP = 30  # one dispatcher tick, seconds


class Rig:
    def __init__(self, tmp_path: Path, *, finishing: bool, start: datetime = NOW, issues=(1, 2, 3)) -> None:
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.ts = FakeTimestamp(start)
        self.claude = FakeClaude()
        self.gh = FakeGh(candidates=[_issue(n, n) for n in issues])
        self.messages: list[str] = []
        self.d = _dispatcher(tmp_path, claude=self.claude, gh=self.gh, clock=self.clock, timestamp=self.ts)
        self.d.log = self.messages.append
        self.d.boot_time = lambda: None  # #782: the fake start dates before the real boot of this PC
        self.d.tick()  # starts the session, offered every issue
        self.sid = self.claude.list_sessions()[0]["id"]
        self.uuid = self.claude.list_sessions()[0]["sessionId"]
        for n in issues:
            self.gh.claim(n)
        self.d.tick()  # it holds them, working / busy
        self.gh._candidates = []
        if finishing:
            mark_finishing(self.sid, finishing_dir=tmp_path / "finishing")
        self.ended_after: float | None = None  # seconds of idleness (from `go_idle`) when it ended
        self._idle_from = 0.0

    def go_idle(self) -> None:
        self.claude.set_state(self.sid, state="working", status="idle")
        self._idle_from = self.clock.value

    def go_busy(self) -> None:
        self.claude.set_state(self.sid, state="working", status="busy")

    def step(self, seconds: float = STEP):
        self.clock.advance(seconds)
        self.ts.advance(seconds=seconds)
        report = self.d.tick()
        if report.ended and self.ended_after is None:
            self.ended_after = self.clock.value - self._idle_from
        return report

    def run(self, minutes: float) -> None:
        for _ in range(int(minutes * 60 / STEP)):
            self.step()

    def idle_wakes(self) -> list[dict]:
        return [e for e in _events(self.tmp_path) if e["kind"] == "orch_idle_wake"]

    def idle_wake_messages(self) -> list[str]:
        return [m for _, m in self.claude.waked if "ended your turn" in m]


def test_row1_finishing_idle_with_3_claims_is_ended_at_10_min_not_woken(tmp_path: Path):
    rig = Rig(tmp_path, finishing=True)
    rig.go_idle()

    rig.run(9.5)
    assert rig.ended_after is None and rig.claude.stopped == []
    rig.run(1)

    assert rig.idle_wakes() == [] and rig.idle_wake_messages() == []
    assert rig.claude.stopped == [rig.sid]
    assert 600 <= rig.ended_after <= 600 + STEP
    ends = {e["issue"]: e["outcome"] for e in _events(tmp_path) if e["kind"] == "orch_end"}
    assert ends == {1: "no_checkpoint", 2: "no_checkpoint", 3: "no_checkpoint"}
    assert sorted(rig.gh.removed_assignees) == [1, 2, 3]
    assert all(not s.assignees for s in rig.gh.issues.values())  # back in the pool


def test_row2_not_finishing_one_wake_at_10_min_ended_at_20_min(tmp_path: Path):
    rig = Rig(tmp_path, finishing=False)
    rig.gh.comment_at[2] = NOW + timedelta(minutes=1)
    rig.go_idle()

    rig.run(9.5)
    assert rig.idle_wake_messages() == []
    rig.run(1)  # 10 min
    messages = rig.idle_wake_messages()
    assert len(messages) == 1 and len(rig.idle_wakes()) == 1
    assert all(f"#{n} (last comment" in messages[0] for n in (1, 2, 3))
    assert rig.claude.stopped == []

    rig.run(9)  # 19 min
    assert rig.claude.stopped == []
    rig.run(1.5)  # 20.5 min

    assert rig.claude.stopped == [rig.sid]
    assert 1200 <= rig.ended_after <= 1200 + STEP
    assert len(rig.idle_wake_messages()) == 1  # 0 wakes after the first
    outcomes = {e["issue"]: e["outcome"] for e in _events(tmp_path) if e["kind"] == "orch_end"}
    assert outcomes == {1: "no_checkpoint", 2: "no_checkpoint", 3: "no_checkpoint"}
    assert sorted(rig.gh.removed_assignees) == [1, 2, 3]


def test_row3_a_running_subagent_means_no_wake_and_no_end(tmp_path: Path):
    for finishing in (False, True):
        rig = Rig(tmp_path / str(finishing), finishing=finishing)
        _subagent(rig.tmp_path / "projects", rig.uuid, "aaa")
        rig.go_idle()

        rig.run(30)

        assert rig.idle_wakes() == [] and rig.idle_wake_messages() == []
        assert rig.claude.stopped == [] and rig.ended_after is None


def test_row4_an_answer_to_the_wake_restarts_the_idle_clock(tmp_path: Path):
    rig = Rig(tmp_path, finishing=False)
    rig.go_idle()
    rig.run(10.5)
    assert len(rig.idle_wake_messages()) == 1

    rig.go_busy()  # it answered the wake
    rig.step()
    rig.go_idle()  # ... and ended its turn again
    rig.run(9)
    assert rig.claude.stopped == [] and len(rig.idle_wake_messages()) == 1  # not ended, not yet woken again

    rig.run(2)  # 10 min after the second idle began: the clock started over, so a new wake, not an end
    assert rig.claude.stopped == [] and len(rig.idle_wake_messages()) == 2

    rig.run(10)
    assert rig.claude.stopped == [rig.sid]


def test_idle_turn_end_is_logged_as_idle_a_permission_wait_as_waiting_on_the_owner(tmp_path: Path):
    rig = Rig(tmp_path / "idle", finishing=False)
    rig.go_idle()
    rig.run(2)
    assert any("is idle while holding #1, #2, #3" in m for m in rig.messages)
    assert not any("waiting on the owner" in m for m in rig.messages)

    waiting = Rig(tmp_path / "wait", finishing=True)
    waiting.go_idle()
    waiting.claude._sessions[waiting.sid]["waitingFor"] = "permission prompt"
    waiting.run(30)
    assert sum("waiting on the owner" in m for m in waiting.messages) == 1
    assert waiting.claude.stopped == [] and waiting.ended_after is None  # a human's wait, not a clock's


def test_row5_replay_of_10_10_turn_ended_14_47_41_finishing_issues_back_by_14_58(tmp_path: Path):
    local = datetime(2026, 10, 10, 14, 47, 41).astimezone()
    rig = Rig(tmp_path, finishing=True, start=local, issues=(746, 747, 748))
    # the session's last record: its turn ended at 14:47:41 (the transcript the brain read)
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    line = {"type": "assistant", "timestamp": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    (projects / f"{rig.uuid}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    rig.go_idle()
    rig.step(46)  # the dispatcher's first poll that sees it idle: 14:48:27 (dispatch.out :6947)

    while rig.claude.stopped == [] and rig.ts.value < local + timedelta(minutes=40):
        rig.step()

    assert rig.claude.stopped == [rig.sid]
    assert rig.ts.value <= local.replace(hour=14, minute=58, second=0), rig.ts.value  # not 15:50
    ends = {e["issue"]: e["outcome"] for e in _events(tmp_path) if e["kind"] == "orch_end"}
    assert ends == {746: "no_checkpoint", 747: "no_checkpoint", 748: "no_checkpoint"}
    assert all(not s.assignees for s in rig.gh.issues.values())
