"""#188 unit 2 (rules 1, 3, 4; acceptance rows 1-4): a pending core mark in an `auto` target holds every ORCH start
and every offer of a newly ready issue to a live ORCH, with no time cap; the update is retried on every tick and
lands on the first tick where every live session is finishing and nothing blocks it.

Driven through `Dispatcher.tick`: a fake clock, fake sessions, a fake `core_update` callable (no network). The
fake answers with the strings the real `apply_auto_core_update` returns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.agentops.dispatch as dispatch
from tools.agentops.core_pending import HOLD_REL, clear_mark, read_mark, write_mark
from tools.agentops.core_update import ORCH_STATE_ENTRY
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue

TAKEN = "core update (auto): updated: 3 file(s) changed; core_version -> 5ebb24f0"
MERGE_BUSY = "core update (auto) refused: core update refused: merge running (the merge line is busy)"
ORCH_LIVE = "core update (auto) refused: refusing: an ORCH session is live in this target"
UNCOMMITTED = "core update (auto) refused: core update refused: uncommitted core paths: tools/agentops/x.py"
SHA = "5ebb24f0aa11bb22cc33dd44ee55ff6677889900"
SHA2 = "9c1d2e3faa11bb22cc33dd44ee55ff6677889900"
BRAIN = "Brain: orch layer"
MIN = 60.0


class Update:
    """The `core_update` callable. `answer` is what it returns while the target is blocked; `taken` flips it
    to the taken line. Records how many ORCH starts had happened at each call."""

    def __init__(self, claude: FakeClaude, answer: str = TAKEN) -> None:
        self.claude = claude
        self.answer = answer
        self.starts_at_call: list[int] = []

    @property
    def calls(self) -> int:
        return len(self.starts_at_call)

    def __call__(self):
        self.starts_at_call.append(len(self.claude.started))
        return self.answer


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
        real = dispatch._load_settings
        monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "update_rule": "auto"})
        self.root = tmp_path
        self.claude = FakeClaude()
        self.claude.add_session("brain-1", name=BRAIN, state="working", status="busy", kind="interactive",
                                cwd=str(self.claude.cwd))
        self.gh = FakeGh(candidates=[_issue(1)])
        self.clock = FakeClock()
        self.wall0 = datetime.now(timezone.utc).timestamp()
        self.update = Update(self.claude)
        self.log: list[str] = []
        self.d = self.new_dispatcher()

    def new_dispatcher(self) -> Dispatcher:
        """A dispatcher on the same target, clocks and files: building a second one is a restart."""
        tmp_path = self.root
        d = Dispatcher(claude=self.claude, gh=self.gh, max_hours=40.0, events_file=tmp_path / "events.jsonl",
                       state_file=tmp_path / ORCH_STATE_ENTRY, projects_dir=tmp_path / "projects",
                       finishing_dir=tmp_path / "finishing", context_line=1_000_000, clock=self.clock,
                       timestamp=lambda: datetime.fromtimestamp(self.wall0 + self.clock.value, timezone.utc),
                       core_update=self.update, merge_lock_path=tmp_path / "var" / "locks" / "merge.lock",
                       core_update_wait_s=5.0, core_root=tmp_path, orch_offer_max=99)
        d.admission = lambda previous_wait: SimpleNamespace(go=True, reason="", line=lambda: "go")
        d.log = self.log.append
        return d

    def start_orch(self, *, finishing: bool) -> None:
        """The ordinary first start (no mark yet), then issue 2 turns ready and the mark is written."""
        self.d.tick()
        assert len(self.claude.started) == 1
        self.update.starts_at_call.clear()  # the first start's own before-start update is not under test
        self.d.sessions[0].finishing = finishing
        self.gh._candidates = [_issue(2)]
        write_mark(self.root, SHA)

    def end_session(self) -> None:
        self.claude._sessions.pop(self.d.sessions[0].session)

    def tick_for(self, minutes: float, every: float = 5.0) -> None:
        """Ticks every `every` minutes of fake time for `minutes` minutes."""
        for _ in range(int(minutes / every)):
            self.clock.advance(every * MIN)
            self.d.tick()

    def wakes_naming(self, number: int) -> list[str]:
        return [m for t, m in self.claude.waked if t != BRAIN and f"#{number}" in m]

    def notices(self) -> list[str]:
        return [m for t, m in self.claude.waked if t == BRAIN and "core waits" in m]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_row1_hermes_replay_no_start_over_6h_while_the_update_is_blocked_then_it_lands_first(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = MERGE_BUSY  # the merge line is busy: the update cannot be taken yet

    rig.tick_for(6 * 60)

    assert len(rig.claude.started) == 1  # 0 further starts in 6 h (the old code started one after 60 min)
    assert rig.update.calls >= 70  # retried on every tick
    assert read_mark(rig.root) is not None

    starts_before = len(rig.claude.started)
    calls_before = rig.update.calls
    rig.update.answer = TAKEN  # the merge line is free
    rig.clock.advance(5 * MIN)
    rig.d.tick()

    assert read_mark(rig.root) is None  # taken on the first tick
    assert not (rig.root / HOLD_REL).exists()
    assert rig.update.starts_at_call[calls_before] == starts_before  # the take came before the next start
    rig.tick_for(10)
    assert len(rig.claude.started) == starts_before + 1  # then the ORCH starts, offered issue 2
    assert rig.claude.started[-1][0] == [2]


def test_row1_the_update_is_taken_on_the_first_tick_when_the_merge_lock_is_free(rig):
    rig.start_orch(finishing=True)

    rig.clock.advance(5 * MIN)
    rig.d.tick()

    assert rig.update.calls >= 1
    assert read_mark(rig.root) is None  # taken on the first tick, with a finishing session still live
    assert len(rig.claude.started) == 2  # and the further ORCH starts after it
    assert rig.update.starts_at_call[0] == 1  # the first call came before the second start


def test_row2_a_live_working_orch_gets_no_wake_for_a_new_issue_while_the_mark_stands(rig):
    rig.start_orch(finishing=False)

    rig.tick_for(3 * 60, every=10)

    assert rig.wakes_naming(2) == []
    assert rig.update.calls == 0  # a working ORCH blocks the update; it is not tried
    assert len(rig.claude.started) == 1


def test_row2_after_the_take_the_issue_is_offered(rig):
    rig.start_orch(finishing=False)
    rig.tick_for(60, every=10)
    assert rig.wakes_naming(2) == []

    rig.end_session()  # the working ORCH ends: nothing blocks the update now
    rig.clock.advance(10 * MIN)
    rig.d.tick()  # the end is noted
    rig.clock.advance(10 * MIN)
    rig.d.tick()

    assert read_mark(rig.root) is None
    assert len(rig.claude.started) == 2
    assert 2 in rig.d.sessions[-1].offered


def test_row2_when_the_mark_is_gone_the_live_orch_is_woken_about_the_issue(rig):
    rig.start_orch(finishing=False)
    rig.tick_for(30, every=10)
    assert rig.wakes_naming(2) == []

    clear_mark(rig.root)  # the home push took the update
    rig.clock.advance(10 * MIN)
    rig.d.tick()

    assert len(rig.wakes_naming(2)) == 1


def test_row3_past_60_min_blocked_there_are_no_starts_and_exactly_one_notice(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = ORCH_LIVE

    rig.tick_for(61 + 5)  # past core_hold_max_min

    assert len(rig.claude.started) == 1
    assert len(rig.notices()) == 1
    assert "mark 5ebb24f0" in rig.notices()[0]

    write_mark(rig.root, SHA2)  # a newer push, same hold
    rig.tick_for(2 * 60)

    assert len(rig.claude.started) == 1
    assert len(rig.notices()) == 1  # no second notice for the same hold
    assert read_mark(rig.root)["sha"] == SHA2


def test_row3_the_notice_names_the_blocking_session_and_since_when(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = MERGE_BUSY
    session = rig.d.sessions[0]

    rig.tick_for(66)

    text = rig.notices()[0]
    assert (session.name or session.session) in text
    assert "finishing" in text
    assert "since " in text


def test_row4_a_refusal_for_uncommitted_core_paths_lets_the_start_go_ahead_and_keeps_the_mark(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = UNCOMMITTED

    rig.clock.advance(5 * MIN)
    rig.d.tick()
    rig.d.tick()

    assert len(rig.claude.started) == 2  # today's behaviour: the start goes ahead
    assert read_mark(rig.root)["sha"] == SHA  # the mark stays


def test_a_target_whose_rule_is_not_auto_holds_nothing(rig, monkeypatch):
    real = dispatch._load_settings
    monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "update_rule": "word"})
    rig.start_orch(finishing=False)
    rig.d.sessions[0].finishing = False

    rig.tick_for(30, every=10)

    assert len(rig.wakes_naming(2)) >= 1  # the wake is not held


def test_a_hold_of_640_min_is_written_for_a_human_in_the_notice_and_the_log(rig, monkeypatch):
    real = dispatch._load_settings
    monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "core_hold_max_min": 640})
    rig.start_orch(finishing=True)
    rig.update.answer = MERGE_BUSY

    rig.tick_for(650, every=10)

    assert "held 10 h 40 min" in rig.notices()[0]
    assert "640" not in rig.notices()[0]
    assert any("held 10 h 40 min" in line for line in rig.log)


def test_one_notice_per_hold_the_cap_notice_first_then_a_release_sends_none(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = MERGE_BUSY
    rig.tick_for(66)
    assert len(rig.notices()) == 1

    rig.update.answer = UNCOMMITTED  # the cause changes: the hold is released
    rig.tick_for(10)

    assert len(rig.claude.started) == 2  # the start goes on
    assert any("hold released" in line for line in rig.log)  # the release is still logged
    assert len(rig.notices()) == 1


def test_one_notice_per_hold_the_release_first_sends_its_notice_and_the_cap_none(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = MERGE_BUSY
    rig.tick_for(10)  # the hold is armed
    rig.update.answer = UNCOMMITTED
    rig.tick_for(5)  # released: one notice, the start goes on

    notices = rig.notices()
    assert len(notices) == 1 and "update refused" in notices[0] and "uncommitted core paths" in notices[0]

    write_mark(rig.root, SHA2)  # a newer push: the same hold again
    rig.update.answer = MERGE_BUSY
    rig.tick_for(70)

    assert len(rig.notices()) == 1  # the cap passed with no second notice
    assert any("still pending" in line for line in rig.log)


def test_row4_after_a_release_a_working_orch_is_woken_about_a_new_issue(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = UNCOMMITTED
    rig.tick_for(10)
    assert len(rig.claude.started) == 2  # the further ORCH, working
    rig.gh._candidates = [_issue(3)]

    rig.tick_for(30, every=10)

    assert len(rig.wakes_naming(3)) >= 1
    assert read_mark(rig.root) is not None


def test_a_restart_keeps_the_release_and_the_wakes_go_out(rig):
    rig.start_orch(finishing=True)
    rig.update.answer = UNCOMMITTED
    rig.tick_for(10)
    assert len(rig.claude.started) == 2
    for cur in rig.d.sessions:  # so a restarted dispatcher adopts them
        rig.claude._sessions[cur.session].update(cwd=str(rig.claude.cwd), name=cur.name)
    rig.d = rig.new_dispatcher()
    rig.d.tick()
    rig.gh._candidates = [_issue(3)]

    rig.tick_for(30, every=10)

    assert len(rig.wakes_naming(3)) >= 1  # still released: no hold re-engaged by the restart
    assert len(rig.claude.started) == 2


@pytest.mark.parametrize("answer, goes_on", [(MERGE_BUSY, False), (ORCH_LIVE, False), (UNCOMMITTED, True)])
def test_before_start_with_no_session_a_waiting_refusal_defers_the_start_and_another_lets_it_go(rig, answer, goes_on):
    write_mark(rig.root, SHA)
    rig.update.answer = answer

    assert rig.d._core_update_before_start() is goes_on
    assert read_mark(rig.root) is not None  # the mark stays either way
