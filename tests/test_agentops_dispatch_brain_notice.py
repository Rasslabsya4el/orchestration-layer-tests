"""#946 unit 2b: the dispatcher tells the brain chat ONCE when a session parks or closes a ticket.

Through `Dispatcher` with its fakes (no real `claude`, no real `gh`, no real message): the notifier of
`brain_notice.py` is the real one, its seams are the dispatcher's own.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue
from tools.agentops.dispatch import Dispatcher, IssueStatus

#: this repo's brain chat as `claude agents --json --all` names it (#946: was "Brain", which matched nothing).
BRAIN = "Brain: Main"
SESSION = "orch1"


class NoticeClaude(FakeClaude):
    """The fake `claude` with a brain chat in its listing (unless `brain=False`)."""

    def __init__(self, *, brain: bool = True) -> None:
        super().__init__()
        self.add_session(SESSION, name="orch #1 #2", state="working", status="busy",
                         startedAt=int(time.time() * 1000) - 60_000)
        if brain:
            # the shape of the real entry: no `state` key for an interactive chat
            self.add_session("brain-chat", name=BRAIN, kind="interactive", status="idle")

    def notices(self) -> list[str]:
        """The messages the brain chat was woken with (the other wakes of the dispatcher are not these)."""
        return [m for target, m in self.waked if target == BRAIN and m.startswith("#")]


class NoticeGh(FakeGh):
    """The fake `gh` with the two reads of the notice: title + newest comment, and the parent link."""

    def __init__(self) -> None:
        super().__init__()
        self.titles: dict[int, str] = {}
        self.newest: dict[int, str] = {}
        self.parents: dict[int, int] = {}

    def issue_title_and_last_comment(self, number: int):
        return self.titles.get(number, f"Title {number}"), self.newest.get(number, "")

    def parent_issue(self, number: int):
        return self.parents.get(number)


def _dispatcher(tmp_path: Path, claude: NoticeClaude, gh: NoticeGh, *, notice: bool = True):
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=FakeClock(),
                   brain_notice_file=tmp_path / "brain_notices.json" if notice else None, brain_title=BRAIN)
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def _holding(tmp_path: Path, *, brain: bool = True, notice: bool = True):
    """A live session that holds #1 and #2 (adopted on tick 1, claims read on tick 2)."""
    claude = NoticeClaude(brain=brain)
    gh = NoticeGh()
    gh.titles = {1: "Fix the first thing", 2: "Add the second thing"}
    d = _dispatcher(tmp_path, claude, gh, notice=notice)
    d.tick()
    gh.claim(1)
    gh.claim(2)
    d.tick()
    assert d.current is not None and d.current.held == {1, 2}
    return claude, gh, d


def _notice_lines(d: Dispatcher) -> list[str]:
    return [line for line in d.log_lines if "brain notice" in line]


def test_1_a_session_ends_holding_two_issues_one_message_with_two_lines(tmp_path: Path):
    """The end path: the claims are unreadable on the ending tick, so `_finish` classifies both."""
    claude, gh, d = _holding(tmp_path)
    gh.newest = {1: "<!-- merge result: done -->\nClosed: merged at abc", 2: "ORCH x: parked on a job\nmore text"}
    gh.close(1)
    gh.park(2)
    gh.claims_fail = True
    claude.set_state(SESSION, state="stopped", status="idle")

    report = d.tick()

    assert report.why == "stopped" and report.outcomes == {1: "closed", 2: "parked"}
    assert claude.notices() == ["#1 Fix the first thing: closed: merge result: done\n"
                                "#2 Add the second thing: parked: ORCH x: parked on a job"]


def test_1_the_same_end_with_readable_claims_is_also_one_message(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    gh.newest = {1: "Closed: done", 2: "Parked: waits on a run"}
    gh.close(1)
    gh.park(2)
    claude.set_state(SESSION, state="stopped", status="idle")

    report = d.tick()
    d.tick()

    assert report.outcomes == {1: "closed", 2: "parked"}
    assert claude.notices() == ["#1 Fix the first thing: closed: Closed: done\n"
                                "#2 Add the second thing: parked: Parked: waits on a run"]


def test_2_a_held_issue_parked_while_its_session_lives_one_message_in_that_tick(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    gh.newest = {1: "ORCH s: parked on the owner's answer"}
    assert claude.notices() == []

    gh.park(1)
    report = d.tick()

    assert report.ended is None  # the session lives
    assert claude.notices() == ["#1 Fix the first thing: parked: ORCH s: parked on the owner's answer"]
    assert (tmp_path / "brain_notices.json").is_file()


def test_3_the_same_state_is_not_sent_again_not_by_later_ticks_not_after_a_restart(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    gh.newest = {1: "ORCH s: parked"}
    gh.park(1)
    d.tick()
    for _ in range(3):
        d.tick()
    assert len(claude.notices()) == 1

    restarted = _dispatcher(tmp_path, claude, gh)  # same state dir: the adopted session holds #1 again
    for _ in range(4):
        restarted.tick()

    assert restarted.current is not None and 1 in restarted.current.held
    assert len(claude.notices()) == 1
    assert json.loads((tmp_path / "brain_notices.json").read_text())["1"]["state"] == "parked"


def test_4_an_ancestor_with_the_autopilot_label_and_a_live_autopilot_session_skips_the_issue(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    gh.parents = {1: 80, 80: 90}
    gh.issues[90] = IssueStatus(number=90, open=True, labels=("autopilot",), assignees=())
    gh.issues[80] = IssueStatus(number=80, open=True, labels=(), assignees=())
    claude.add_session("ap", name="autopilot #90", state="working", status="busy")
    gh.newest = {1: "ORCH s: parked"}

    gh.park(1)
    d.tick()
    d.tick()

    assert claude.notices() == []
    skips = [line for line in d.log_lines if "#1 skipped" in line]
    assert len(skips) == 1 and "autopilot #90" in skips[0]


def test_4_a_closed_goal_with_the_autopilot_label_and_no_live_session_does_not_skip(tmp_path: Path):
    # #1129: an open labelled goal with no live session is a live stretch between sessions (see
    # test_issue1129_stretch_live.py); a closed goal is a stretch that has ended, so it does not skip.
    claude, gh, d = _holding(tmp_path)
    gh.parents = {1: 90}
    gh.issues[90] = IssueStatus(number=90, open=False, labels=("autopilot",), assignees=())
    gh.newest = {1: "ORCH s: parked"}

    gh.park(1)
    d.tick()

    assert len(claude.notices()) == 1


def test_5_no_live_session_titled_like_the_brain_sends_nothing_logs_once(tmp_path: Path):
    claude, gh, d = _holding(tmp_path, brain=False)
    gh.newest = {1: "ORCH s: parked"}

    gh.park(1)
    for _ in range(3):
        d.tick()  # no exception

    assert claude.notices() == [] and claude.waked == []
    lines = _notice_lines(d)
    assert len(lines) == 1 and "not live" in lines[0]


def test_5_a_stopped_brain_chat_is_not_live(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    claude.set_state("brain-chat", state="stopped", status="idle")
    gh.newest = {1: "ORCH s: parked"}

    gh.park(1)
    d.tick()

    assert claude.notices() == [] and len(_notice_lines(d)) == 1


def _end_with_failing_wake(tmp_path: Path, *, notice: bool) -> tuple:
    claude, gh, d = _holding(tmp_path, notice=notice)
    gh.newest = {1: "ORCH s: closed", 2: "ORCH s: parked"}
    gh.close(1)
    gh.park(2)
    gh.claims_fail = True
    claude.wake_fails = True
    claude.set_state(SESSION, state="stopped", status="idle")
    report = d.tick()
    return claude, gh, d, report


def test_6_a_sender_that_fails_changes_nothing_of_the_dispatchers_own_flow(tmp_path: Path):
    claude, gh, d, report = _end_with_failing_wake(tmp_path / "with", notice=True)
    claude0, gh0, d0, report0 = _end_with_failing_wake(tmp_path / "without", notice=False)

    assert report.outcomes == report0.outcomes == {1: "closed", 2: "parked"}
    assert report.why == report0.why and d.sessions == d0.sessions == []
    assert gh.removed_assignees == gh0.removed_assignees
    assert (gh.comments, gh.added_labels, gh.removed_labels) == (gh0.comments, gh0.added_labels, gh0.removed_labels)
    assert len(claude.notices()) == 1  # the one attempt of the end
    failed = [line for line in _notice_lines(d) if "send failed" in line]
    assert len(failed) == 1 and "1 kept for one retry" not in failed[0] and "2 kept for one retry" in failed[0]


def test_6_the_failed_batch_is_retried_once_on_the_next_tick_then_dropped(tmp_path: Path):
    claude, gh, d, _ = _end_with_failing_wake(tmp_path, notice=True)

    for _ in range(4):
        d.tick()

    assert len(claude.notices()) == 2  # the end's attempt, one retry, never a third
    failed = [line for line in _notice_lines(d) if "send failed" in line]
    assert len(failed) == 2 and "dropped after the retry" in failed[1] and "2 dropped" in failed[1]


def test_6_a_retry_that_succeeds_sends_the_same_lines_once(tmp_path: Path):
    claude, gh, d, _ = _end_with_failing_wake(tmp_path, notice=True)
    claude.wake_fails = False

    d.tick()
    d.tick()

    sent = [m for m in claude.notices()]
    assert len(sent) == 2 and sent[0] == sent[1]


def test_a_tick_across_a_day_boundary_with_a_live_brain_chat_sends_the_brain_nothing(tmp_path: Path):
    """#111 unit 1: the dispatcher sends the brain chat no daily sweep message. Ticks on both sides of a day boundary,
    with a live brain chat, send it 0 messages. No due row is fed: the tick reads no sweep list any more, so a due row
    would reach nothing. This pins the outcome only; before the fix, the same tick (with the old daily-sweep wiring
    passed in) sent 3 "Sweep (...)" messages, which is the red run recorded in the commit d631e0b message."""
    claude = NoticeClaude()
    gh = NoticeGh()
    clock = FakeClock()
    eve = datetime(2026, 10, 16, 23, 30).astimezone()
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=clock, timestamp=lambda: eve + timedelta(seconds=clock()), brain_title=BRAIN)
    d.log_lines = []
    d.log = d.log_lines.append

    d.tick()             # 23:30 the day before
    clock.advance(3600)  # 00:30 the next day
    d.tick()
    clock.advance(7200)  # 02:30
    d.tick()

    assert [m for target, m in claude.waked if target == BRAIN] == []


# ----- #213: an issue a session leaves in `needs-info` is a question for the brain ---------------------------------

NEEDS_INFO = ("needs-info",)


class QuestionGh(NoticeGh):
    """The notice's read with a checkpoint id and link per issue: the newest comment is the question."""

    def __init__(self) -> None:
        super().__init__()
        self.checkpoint: dict[int, str] = {}

    def issue_title_and_last_comment(self, number: int):
        from tools.agentops.brain_notice import IssueRead

        cid = self.checkpoint.get(number)
        return IssueRead(self.titles.get(number, f"Title {number}"), self.newest.get(number, ""), cid, None, (),
                         link=f"https://gh.test/{number}#issuecomment-{cid}" if cid else "")

    def ask(self, number: int, checkpoint: str, text: str = "ORCH s: which of the two?") -> None:
        """What ORCH does: the question as a checkpoint comment, the `needs-info` label, the assignee gone."""
        self.checkpoint[number] = checkpoint
        self.newest[number] = text
        self.issues[number] = IssueStatus(number=number, open=True, labels=NEEDS_INFO, assignees=())


def _asking(tmp_path: Path):
    claude = NoticeClaude()
    gh = QuestionGh()
    gh.titles = {1: "Fix the first thing", 2: "Add the second thing"}
    d = _dispatcher(tmp_path, claude, gh)
    d.tick()
    gh.claim(1)
    gh.claim(2)
    d.tick()
    assert d.current is not None and d.current.held == {1, 2}
    return claude, gh, d


def test_213_a_session_that_ends_with_a_needs_info_issue_tells_the_brain_once(tmp_path: Path):
    claude, gh, d = _asking(tmp_path)
    gh.ask(1, "c1")
    gh.close(2)
    gh.newest[2] = "Closed: done"
    claude.set_state(SESSION, state="stopped", status="idle")

    report = d.tick()
    d.tick()
    d.tick()

    assert report.outcomes == {1: "needs_info", 2: "closed"}
    assert claude.notices() == ["#1 Fix the first thing: needs a decision (checkpoint https://gh.test/1#issuecomment-c1)"
                                ": ORCH s: which of the two?\n"
                                "#2 Add the second thing: closed: Closed: done"]
    assert any("needing a decision" in line and "(#1 #2)" in line for line in d.log_lines if line.startswith("told"))


def test_213_a_live_sessions_issue_gaining_needs_info_is_told_and_a_newer_checkpoint_is_told_again(tmp_path: Path):
    from tools.agentops.dispatch import NEEDS_INFO_REREAD_SECONDS

    claude, gh, d = _asking(tmp_path)
    assert claude.notices() == []

    gh.ask(1, "c1")
    d.tick()
    assert len(claude.notices()) == 1 and "needs a decision" in claude.notices()[0]

    d.clock.advance(NEEDS_INFO_REREAD_SECONDS + 1)
    d.tick()  # the same checkpoint: nothing
    assert len(claude.notices()) == 1

    gh.ask(1, "c2", "ORCH s: and now this one?")
    d.tick()  # a newer checkpoint, inside the re-read window: not read yet
    assert len(claude.notices()) == 1
    d.clock.advance(NEEDS_INFO_REREAD_SECONDS + 1)
    d.tick()

    assert len(claude.notices()) == 2
    assert "issuecomment-c2" in claude.notices()[1] and "and now this one?" in claude.notices()[1]
    d.clock.advance(NEEDS_INFO_REREAD_SECONDS + 1)
    d.tick()
    assert len(claude.notices()) == 2
    assert json.loads((tmp_path / "brain_notices.json").read_text())["1"]["key"] == "c2"
