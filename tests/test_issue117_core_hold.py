"""#117: a target holding a pending core mark starts no further ORCH until the core is taken.

Rows 1-4 of the issue, driven through `Dispatcher.tick`: a mark and a finishing session live hold the
start (1 line, 0 starts) while the update is blocked; once the blocker is gone the pending update runs before
the next start (also when the state file still lists the ended session); past `core_hold_max_min` (play time not
counted) the hold goes on and one brain notice per mark is sent (#188 rule 1 reversed the old row 4: no time cap
lets a start go on with the old core).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.agentops.dispatch as dispatch
from tools.agentops import core_settings, machine_mode
from tools.agentops.core_pending import HOLD_REL, MARK_REL, clear_mark, read_mark, write_mark
from tools.agentops.core_update import ORCH_STATE_ENTRY
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue

TAKEN = "core update (auto): updated: 3 file(s) changed; core_version -> 5ebb24f0"
BLOCKED = "core update (auto) refused: core update refused: merge running (the merge line is busy)"
SHA = "5ebb24f0aa11bb22cc33dd44ee55ff6677889900"
BRAIN = "Brain: orch layer"


class Update:
    """The `core_update` callable: counts calls, records how many starts had happened at each call."""

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
        monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "update_rule": "auto"})  # #188
        self.root = tmp_path
        self.claude = FakeClaude()
        self.claude.add_session("brain-1", name=BRAIN, state="working", status="busy", kind="interactive",
                                cwd=str(self.claude.cwd))
        self.gh = FakeGh(candidates=[_issue(1)])
        self.clock = FakeClock()
        self.wall0 = datetime.now(timezone.utc).timestamp()
        self.update = Update(self.claude, answer=BLOCKED)  # #188: the update stays blocked until the session ends
        self.log: list[str] = []
        self.d = self.new_dispatcher()

    def new_dispatcher(self) -> Dispatcher:
        """A dispatcher on the same target and clocks: building a second one is a restart."""
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

    def finishing_session(self):
        """One live ORCH that is finishing, in memory and in the claude listing; the mark is then written."""
        self.d.tick()  # no mark yet: the ordinary start
        assert len(self.claude.started) == 1
        self.d.sessions[0].finishing = True
        listed = self.claude._sessions[self.d.sessions[0].session]
        listed.update(cwd=str(self.claude.cwd), name=self.d.sessions[0].name)  # so a restarted dispatcher adopts it
        self.gh._candidates = [_issue(2)]  # one more ready issue waits
        write_mark(self.root, SHA)

    def end_session(self):
        self.claude._sessions.pop(self.d.sessions[0].session)
        self.update.answer = TAKEN  # nothing blocks the update any more

    def holds(self) -> list[str]:
        return [m for m in self.log if m.startswith("core waits: orch starts and offers held")]

    def ended_lines(self) -> list[str]:
        """The one line written when the hold passes `core_hold_max_min` (the notice moment)."""
        return [m for m in self.log if m.startswith("core waits: held ") and "still pending" in m]

    def notices(self) -> list[tuple[str, str]]:
        return [(t, m) for t, m in self.claude.waked if t == BRAIN and "core waits" in m]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_row1_a_mark_and_a_finishing_session_hold_the_start_with_one_line(rig):
    rig.finishing_session()

    for _ in range(3):
        rig.d.tick()

    assert len(rig.claude.started) == 1  # only the first, before the mark
    assert len(rig.holds()) == 1
    assert "(1 live, mark 5ebb24f0)" in rig.holds()[0]
    assert rig.update.calls == 4  # the first start's before-start update, then one retry per held tick (#188 rule 4)


def test_row2_the_session_ends_then_the_pending_update_runs_before_the_start(rig):
    rig.finishing_session()
    rig.d.tick()
    rig.end_session()
    rig.d.tick()  # the end is noted; a session that just ended is replaced no earlier than the next tick
    assert len(rig.claude.started) == 1
    rig.d.tick()

    assert len(rig.claude.started) == 2
    assert rig.update.starts_at_call[-2] == 1  # the pending update ran when 1 start had happened, before the 2nd
    assert read_mark(rig.root) is None
    assert not (rig.root / MARK_REL).exists()


def test_row3_a_state_file_still_listing_the_ended_session_does_not_delay_the_update(rig):
    rig.finishing_session()
    rig.d.tick()
    stale = json.loads((rig.root / ORCH_STATE_ENTRY).read_text(encoding="utf-8"))
    assert stale["sessions"]  # the file lists the finishing session
    rig.end_session()
    rig.d.sessions.clear()  # ended earlier in the tick, before the file was rewritten
    calls_before = rig.update.calls

    rig.d.tick()

    assert rig.update.calls > calls_before  # the pending update ran on the first tick after the end
    assert rig.update.starts_at_call[calls_before] == 1  # ... before the second start
    assert read_mark(rig.root) is None


def test_row4_past_the_bound_the_hold_goes_on_with_one_line_and_one_notice(rig):
    # #188 rule 1 reversed the old row 4 (the start went ahead after the bound): no time cap lets a start go on
    rig.finishing_session()
    rig.d.tick()
    assert len(rig.holds()) == 1
    rig.clock.advance(61 * 60)

    rig.d.tick()
    rig.d.tick()

    assert len(rig.claude.started) == 1
    assert len(rig.ended_lines()) == 1
    assert "held 61 min, the core update is still pending" in rig.ended_lines()[0]
    assert len(rig.notices()) == 1
    assert rig.notices()[0][0] == BRAIN
    assert len(rig.holds()) == 1


def test_row4_just_inside_the_bound_the_hold_stands(rig):
    rig.finishing_session()
    rig.d.tick()
    rig.clock.advance(59 * 60)

    rig.d.tick()

    assert len(rig.claude.started) == 1
    assert rig.ended_lines() == [] and rig.notices() == []


def test_row4_play_time_does_not_count_toward_the_bound(rig, monkeypatch):
    rig.finishing_session()
    rig.d.tick()
    # the machine stood in play for 50 of the 61 minutes
    monkeypatch.setattr(machine_mode, "stood_between", lambda a, b: 50 * 60.0)
    rig.clock.advance(61 * 60)

    rig.d.tick()

    assert len(rig.claude.started) == 1
    assert rig.ended_lines() == [] and rig.notices() == []


def test_the_bound_is_the_setting_core_hold_max_min(rig, monkeypatch):
    import tools.agentops.dispatch as dispatch
    real = dispatch._load_settings
    monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "core_hold_max_min": 10})
    rig.finishing_session()
    rig.d.tick()
    rig.clock.advance(11 * 60)

    rig.d.tick()

    assert len(rig.claude.started) == 1  # #188: the setting is only the moment of the notice
    assert "held 11 min" in rig.ended_lines()[0]


def test_a_target_without_the_setting_reads_60():
    assert core_settings.DEFAULTS["core_hold_max_min"] == 60


def test_no_mark_nothing_changes(rig):
    rig.d.tick()
    rig.d.sessions[0].finishing = True
    rig.gh._candidates = [_issue(2)]

    rig.d.tick()
    rig.d.tick()

    assert rig.holds() == []
    assert len(rig.claude.started) == 2  # every live session finishing: a further one starts, as before


def test_a_restart_mid_hold_continues_the_clock_and_repeats_no_line_or_notice(rig):
    rig.finishing_session()
    rig.d.tick()
    assert len(rig.holds()) == 1
    rig.clock.advance(40 * 60)
    rig.d = rig.new_dispatcher()  # the dispatcher restarts: it adopts the live session
    rig.d.tick()
    rig.d.sessions[0].finishing = True
    rig.d.tick()
    assert len(rig.claude.started) == 1 and len(rig.holds()) == 1  # still held, no second hold line

    rig.clock.advance(21 * 60)  # 61 min since the first line, 21 of them after the restart
    rig.d.tick()
    assert len(rig.claude.started) == 1 and len(rig.ended_lines()) == 1 and len(rig.notices()) == 1

    rig.d = rig.new_dispatcher()  # a second restart after the notice
    rig.d.tick()
    rig.d.tick()
    assert len(rig.holds()) == 1 and len(rig.ended_lines()) == 1 and len(rig.notices()) == 1


def test_a_mark_cleared_and_written_again_starts_a_fresh_hold(rig):
    rig.finishing_session()
    rig.d.tick()
    assert (rig.root / HOLD_REL).is_file()
    rig.clock.advance(50 * 60)
    clear_mark(rig.root)
    ready, rig.gh._candidates = rig.gh._candidates, []  # nothing to start on the tick without a mark
    rig.d.tick()  # no mark: no hold, and the old hold is gone
    assert not (rig.root / HOLD_REL).exists()
    rig.gh._candidates = ready
    write_mark(rig.root, SHA)
    rig.clock.advance(20 * 60)  # 70 min after the first hold's start

    rig.d.tick()

    assert len(rig.holds()) == 2  # a second hold line: a fresh hold
    assert rig.ended_lines() == [] and rig.notices() == []
