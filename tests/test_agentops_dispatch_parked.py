"""tools/agentops/dispatch.py (#226): the end check reads who holds the issue, not its label -- an
open issue with no assignee is `parked` whatever labels it carries, even `ready-for-agent` put back
by an answer that landed before the session's end was read. Numbers 1, 3, 4, 5 of #226's "Numbers the
tests must reproduce"; numbers 2 and 6 are already covered, unchanged, by
`test_60c_one_issue_left_claimed_takes_the_no_checkpoint_path_alone` and
`test_third_consecutive_no_checkpoint_failure_labels_needs_info`."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import Dispatcher, IssueStatus, MAX_CONSECUTIVE_NO_CHECKPOINT

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _events, _issue, _start
from tests._area_rule import _no_area_rule  # noqa: F401


def test_1_answered_before_the_end_is_read_is_parked_not_no_checkpoint(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)])
    gh.claim(9)
    d.tick()

    # ORCH parks #9 with a checkpoint: needs-info set, ready-for-agent removed, unassigned.
    gh.issues[9] = IssueStatus(number=9, open=True, labels=("needs-info",), assignees=())
    # The brain answers in the issue before the dispatcher reads the end: ready-for-agent back,
    # needs-info removed, still unassigned.
    gh.issues[9] = IssueStatus(number=9, open=True, labels=READY, assignees=())
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.outcomes == {9: "parked"}
    assert gh.comments == []
    assert gh.added_labels == [] and gh.removed_labels == []
    ends = [e for e in _events(tmp_path) if e["kind"] == "orch_end"]
    assert [(e["issue"], e["outcome"]) for e in ends] == [(9, "parked")]


def test_4_after_row_1_the_issue_is_offered_at_the_next_tick(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)])
    gh.claim(9)
    d.tick()
    gh.issues[9] = IssueStatus(number=9, open=True, labels=("needs-info",), assignees=())
    gh.issues[9] = IssueStatus(number=9, open=True, labels=READY, assignees=())
    claude.set_state(live_id, state="done", status="idle")
    d.tick()

    gh._candidates = [_issue(9)]
    report = d.tick()

    assert len(claude.started) == 2
    assert claude.started[-1][0] == [9]


def test_3_the_streak_is_broken_by_a_parked_end(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    for _ in range(2):  # two consecutive no_checkpoint endings on #9
        gh._candidates = [_issue(9)]
        d.tick()
        live_id = claude.list_sessions()[0]["id"]
        gh.claim(9)
        claude.set_state(live_id, state="done", status="idle")
        report = d.tick()
        assert report.outcomes == {9: "no_checkpoint"}

    # the case of row 1: parked with a checkpoint, relabelled before the end is read
    gh._candidates = [_issue(9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    d.tick()  # registers #9 as held while it is still claimed
    gh.issues[9] = IssueStatus(number=9, open=True, labels=("needs-info",), assignees=())
    gh.issues[9] = IssueStatus(number=9, open=True, labels=READY, assignees=())
    claude.set_state(live_id, state="done", status="idle")
    report = d.tick()
    assert report.outcomes == {9: "parked"}

    # one more no_checkpoint: the streak counts from 1, not 3 -- needs-info is not set
    gh._candidates = [_issue(9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    claude.set_state(live_id, state="done", status="idle")
    report = d.tick()

    assert report.outcomes == {9: "no_checkpoint"}
    assert gh.added_labels == []  # never reached MAX_CONSECUTIVE_NO_CHECKPOINT since the break
    assert MAX_CONSECUTIVE_NO_CHECKPOINT == 3


def test_5_the_max_hours_path_gives_the_same_outcome_for_row_1(tmp_path: Path):
    clock = FakeClock()
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)], max_hours=1.0, clock=clock)
    gh.claim(9)
    d.tick()
    gh.issues[9] = IssueStatus(number=9, open=True, labels=("needs-info",), assignees=())
    gh.issues[9] = IssueStatus(number=9, open=True, labels=READY, assignees=())
    clock.advance(1.5 * 3600)

    report = d.tick()

    assert report.why == "timeout" and report.outcomes == {9: "parked"}
    assert gh.comments == [] and gh.removed_assignees == []


def test_5_the_max_hours_path_gives_the_same_outcome_for_row_2(tmp_path: Path):
    clock = FakeClock()
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)], max_hours=1.0, clock=clock)
    gh.claim(9)
    d.tick()
    clock.advance(1.5 * 3600)  # #9 stays assigned and ready-for-agent

    report = d.tick()

    assert report.why == "timeout" and report.outcomes == {9: "timeout"}
    assert [n for n, _ in gh.comments] == [9] and gh.removed_assignees == [9]


# -- #359: a session's end releases what it parked; a parked issue waits for something newer ---------

import json
import subprocess

from tools.agentops.dispatch import GhCli, IssueSummary, ParkMark, parse_candidates

CHECKPOINT_AT = "2026-09-30T10:00:00Z"
AFTER_AT = "2026-09-30T11:00:00Z"


class MarkGh(FakeGh):
    """FakeGh that answers `parking_mark` with the checkpoint time."""

    def parking_mark(self, number: int, end_at: str = "", session_ids: tuple = ()):
        return ParkMark(comment_at=CHECKPOINT_AT, updated_at=CHECKPOINT_AT)


class RealMarkGh(FakeGh):
    """FakeGh whose `parking_mark` is the real `GhCli.parking_mark`, fed a canned `gh issue view`."""

    def __init__(self, comments: list[dict], updated_at: str, **kw) -> None:
        super().__init__(**kw)
        self._view = json.dumps({"comments": comments, "updatedAt": updated_at})

    def parking_mark(self, number: int, end_at: str = "", session_ids: tuple = ()):
        cli = GhCli(runner=lambda args, stdin: subprocess.CompletedProcess(args, 0, self._view, ""))
        return cli.parking_mark(number, end_at, session_ids)


def _summary(number: int, *, comment_at: str, updated_at: str) -> IssueSummary:
    return IssueSummary(number=number, created_at="2026-09-01T00:00:00Z", labels=READY,
                        latest_comment_at=comment_at, updated_at=updated_at)


def _two_issues_end(tmp_path: Path):
    claude = FakeClaude()
    gh = MarkGh(candidates=[_issue(1), _issue(2)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(1)
    gh.claim(2)
    d.tick()  # both held
    gh.close(2)  # #1 stays assigned and ready-for-agent: the session parked it without unassigning
    claude.set_state(live_id, state="done", status="idle")
    return claude, gh, d


def test_359_end_unassigns_the_parked_issue_and_leaves_the_closed_one(tmp_path: Path):
    claude, gh, d = _two_issues_end(tmp_path)
    d.sessions[0].let_go.add(1)

    report = d.tick()

    assert report.outcomes == {1: "parked", 2: "closed"}
    assert gh.removed_assignees == [1]
    assert gh.issues[1].assignees == ()
    assert gh.comments == []


def test_359_pool_holds_the_parked_issue_until_something_is_newer(tmp_path: Path):
    claude, gh, d = _two_issues_end(tmp_path)
    d.sessions[0].let_go.add(1)
    d.tick()
    assert 1 in d._park_marks

    d.has_pending_job = lambda n: True  # #438: changed; "held" needs a configured reader that says a job is pending (no reader = none awaited)
    gh._candidates = [_summary(1, comment_at=CHECKPOINT_AT, updated_at=CHECKPOINT_AT)]
    assert d._eligible_candidates() == []
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started  # no session started only to park it again

    # a comment (the daemon's hand-back) after the checkpoint: back in the pool, offered in one poll
    gh._candidates = [_summary(1, comment_at=AFTER_AT, updated_at=AFTER_AT)]
    d.tick()
    assert 1 not in d._park_marks
    assert claude.started[-1][0] == [1]


def test_359_a_label_event_after_the_checkpoint_also_returns_the_issue(tmp_path: Path):
    claude, gh, d = _two_issues_end(tmp_path)
    d.sessions[0].let_go.add(1)
    d.tick()

    gh._candidates = [_summary(1, comment_at=CHECKPOINT_AT, updated_at=AFTER_AT)]
    assert [c.number for c in d._eligible_candidates()] == [1]


def test_359_an_answer_after_the_session_end_but_before_the_end_is_read_is_offered(tmp_path: Path):
    # session's last transcript record at T; ORCH's parking checkpoint at T-2 s; the brain's answer
    # at T+5 s lands before the dispatcher reads the end.
    comments = [{"body": "Checkpoint: parked", "createdAt": "2026-09-30T10:00:08.500Z"},
                {"body": "answer", "createdAt": "2026-09-30T10:00:15Z"}]
    claude = FakeClaude()
    gh = RealMarkGh(comments, "2026-09-30T10:00:16Z", candidates=[_issue(1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"uuid-{live_id}.jsonl").write_text(
        json.dumps({"type": "assistant", "timestamp": "2026-09-30T10:00:10Z"}) + "\n", encoding="utf-8")
    d.sessions[0].let_go.add(1)
    claude.set_state(live_id, state="done", status="idle")

    assert d.tick().outcomes == {1: "parked"}
    assert gh.removed_assignees == [1]
    mark = d._park_marks[1]
    assert mark.comment_at == "2026-09-30T10:00:08.500Z"  # the checkpoint, not the answer

    gh._candidates = [_summary(1, comment_at="2026-09-30T10:00:15Z", updated_at="2026-09-30T10:00:16Z")]
    d.tick()  # the next poll
    assert claude.started[-1][0] == [1]


def test_359_parse_candidates_reads_the_times():
    out = parse_candidates([{"number": 1, "createdAt": "c", "updatedAt": "u",
                             "comments": [{"body": "x", "createdAt": "t1"}, {"body": "y", "createdAt": "t2"}]}])
    assert (out[0].latest_comment_at, out[0].updated_at) == ("t2", "u")


# -- #514: an issue another live session holds is resolved at the holder's end, let go or not ------

def _one_session_for_514(tmp_path: Path):
    from tests.test_agentops_dispatch_finishing import _dispatcher as _fin_dispatcher

    claude, gh = FakeClaude(), MarkGh(candidates=[_issue(198, 1)])
    d = _fin_dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(198)
    d.tick()
    return claude, gh, d, a


def _parked_by_finishing_a_reclaimed_by_b(tmp_path: Path):
    """The #435 incident: A (finishing) parked #198 (in A's `let_go`); the hand-back put it back and B
    started and claimed it. Both A and B are live; #198 is assigned and ready-for-agent."""
    from tools.agentops.dispatch import mark_finishing

    claude, gh, d, a = _one_session_for_514(tmp_path)
    gh.park(198)
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    d.tick()  # A finishing; B starts
    b = next(iter({s["id"] for s in claude.list_sessions()} - {a}))
    d.tick()
    a_cur = next(c for c in d.sessions if c.session == a)
    assert a_cur.let_go == {198}
    gh.claim(198)  # the hand-back: ready-for-agent again, B claims
    d.tick()
    assert 198 in next(c for c in d.sessions if c.session == b).held
    return claude, gh, d, a, b


def test_514_row1_the_new_holders_end_resolves_it(tmp_path: Path):
    """#694: B's claim moves #198 out of A's `held`, so B's own end resolves it (was: left alone)."""
    claude, gh, d, a, b = _parked_by_finishing_a_reclaimed_by_b(tmp_path)

    claude.set_state(b, state="done", status="idle")
    report = d.tick()

    assert report.ended == b and report.outcomes == {198: "no_checkpoint"}


def test_514_row2_the_old_holders_end_leaves_it_alone(tmp_path: Path):
    """#694: A no longer holds #198 once B claimed it; A's end resolves nothing for it."""
    claude, gh, d, a, b = _parked_by_finishing_a_reclaimed_by_b(tmp_path)
    claude.set_state(b, state="done", status="idle")
    d.tick()

    claude.set_state(a, state="done", status="idle")
    report = d.tick()

    assert report.ended == a and 198 not in report.outcomes


def test_514_row3_a_non_finishing_session_reclaiming_its_parked_issue_holds_it_again(tmp_path: Path):
    claude, gh, d, a = _one_session_for_514(tmp_path)
    gh.park(198)
    d.tick()
    a_cur = d.sessions[0]
    a_cur.let_go.add(198)  # parked: let go

    gh.claim(198)  # the hand-back comes, A (not finishing) takes it again
    d.tick()
    assert 198 not in a_cur.let_go and 198 in a_cur.held
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started  # D110: no further session while A is live

    claude.set_state(a, state="done", status="idle")
    report = d.tick()
    assert report.outcomes == {198: "no_checkpoint"}
    assert len(gh.comments) == 1 and gh.removed_assignees == [198]


def test_514_row4_the_pool_offers_the_assigned_issue_to_no_third_session(tmp_path: Path):
    claude, gh, d, a, b = _parked_by_finishing_a_reclaimed_by_b(tmp_path)
    claude.set_state(b, state="done", status="idle")
    d.tick()
    started = len(claude.started)

    gh._candidates = []  # the pool is unassigned issues only: #198 is assigned, so it is not in it
    d.tick()
    d.tick()

    assert len(claude.started) == started
