"""tools/agentops/dispatch.py (issues #50, #58, #60): the ORCH dispatcher loop, with a fake
`claude.exe` CLI, a fake `gh` and a fake clock -- no real `claude` or `gh` calls. Since #60 (D104)
one session holds up to 3 issues; the cases (a)-(d) of #60's "Done when" are marked below."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

import pytest

from tools.agentops.dispatch import (
    ClaudeCli,
    Dispatcher,
    GuardError,
    IssueStatus,
    IssueSummary,
    MAX_CONSECUTIVE_NO_CHECKPOINT,
    MAX_WAKES_PER_ISSUE,
    REPO_NAME,
    WAKE_COOLDOWN_SECONDS,
    build_command,
    build_prompt,
    build_unblocker_command,
    build_wake_message,
    end_session,
    find_claude_exe,
    parse_candidates,
    running_subagent_ids,
    session_matches_root,
)
from tools.agentops.worktree import ROOT
from tests._area_rule import _no_area_rule  # noqa: F401

READY = ("ready-for-agent",)


class FakeClaude:
    """A fake `claude.exe`: `start` hands out sequential session ids, `list_sessions` reports
    whatever state the test sets, `stop` just records the call and, by default, pops the session --
    modelling the outcome the dispatcher relies on to know the session is over. Set
    `stop_leaves_process = True` to model the real #86 bug instead: `stop` marks state `stopped` but
    leaves the pid listed, so `end_session` must fall through to `end_process`."""

    def __init__(self, cwd: Path = ROOT) -> None:
        self.cwd = cwd
        self.started: list[tuple[list[int], list[str]]] = []
        self.stopped: list[str] = []
        self.ended_pids: list[int] = []
        self.end_process_fails: set[int] = set()
        self.stop_leaves_process = False
        self._sessions: dict[str, dict] = {}
        self._next_id = 1
        #: #137: every `(target, message)` this fake was asked to wake, and the canned reply.
        self.waked: list[tuple[str, str]] = []
        self.wake_fails = False
        #: #209: every `(issue, argv)` the dispatcher started an unblocker with (never in `started`: that is ORCH's).
        self.unblockers: list[tuple[int, list[str]]] = []

    def start_unblocker(self, number: int) -> tuple[str, list[str]]:
        session_id = f"sess-{self._next_id}"
        self._next_id += 1
        cmd = build_unblocker_command(number, "claude.exe")
        self.unblockers.append((number, cmd))
        self._sessions[session_id] = {"id": session_id, "sessionId": f"uuid-{session_id}", "name": cmd[3],
                                      "kind": "background", "state": "working", "status": "busy"}
        return session_id, cmd

    def start(self, offered: list[int], parked_by=None) -> tuple[str, list[str]]:
        session_id = f"sess-{self._next_id}"
        self._next_id += 1
        cmd = build_command(offered, "claude.exe", parked_by)
        self.started.append((list(offered), cmd))
        self._sessions[session_id] = {"id": session_id, "sessionId": f"uuid-{session_id}",
                                      "kind": "background", "state": "working", "status": "busy"}
        return session_id, cmd

    def list_sessions(self) -> list[dict]:
        return list(self._sessions.values())

    def stop(self, session_id: str) -> bool:
        self.stopped.append(session_id)
        if self.stop_leaves_process:
            session = self._sessions.get(session_id)
            if session is not None:
                session["state"] = "stopped"
        else:
            self._sessions.pop(session_id, None)
        return True

    def end_process(self, pid: int) -> bool:
        self.ended_pids.append(pid)
        if pid in self.end_process_fails:
            return False
        self._sessions = {sid: s for sid, s in self._sessions.items() if s.get("pid") != pid}
        return True

    def wake(self, target: str, message: str) -> tuple[bool, str]:
        self.waked.append((target, message))
        if self.wake_fails:
            return False, "boom"
        return True, '{"success":true}'

    # test helpers, not part of the real ClaudeCli surface
    def set_state(self, session_id: str, *, state: str, status: str) -> None:
        self._sessions[session_id].update(state=state, status=status)

    def add_session(self, session_id: str, **fields) -> None:
        self._sessions[session_id] = {"id": session_id, "kind": "background", **fields}


class FakeGh:
    """A fake `gh`: a small in-memory issue table plus every call the dispatcher can make.
    `claimed_issues` is derived from the table (open, `ready-for-agent`, assigned)."""

    def __init__(self, issues: dict[int, IssueStatus] | None = None,
                 candidates: list[IssueSummary] | None = None) -> None:
        self.issues: dict[int, IssueStatus] = dict(issues or {})
        self._candidates = list(candidates or [])
        self.comments: list[tuple[int, str]] = []
        self.removed_assignees: list[int] = []
        self.added_labels: list[tuple[int, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.claims_fail = False
        #: #378: per-issue time of the latest `assigned` event; unset = far future (assigned after any
        #: session start), `None` = no event. `assigned_reads` lists every events read.
        self.assigned_at: dict[int, datetime | None] = {}
        self.assigned_reads: list[int] = []
        #: #650: per-issue time of the latest comment; unset = no comment.
        self.comment_at: dict[int, datetime | None] = {}
        #: per-issue body of the latest comment; unset = none.
        self.comment_body: dict[int, str] = {}

    def last_comment_at(self, number: int) -> datetime | None:
        return self.comment_at.get(number)

    def issue_comments(self, number: int):  # #209: the unblocker line reads a thread; none by default
        return []

    def last_comment_names(self, number: int, session_ids: tuple = ()):  # #832: last comment names the session
        body = self.comment_body.get(number)
        return bool(body) and any(i and i in body for i in session_ids)

    def own_checkpoint_parks(self, number: int, session_ids: tuple = (), held_from: str = "", held_until: str = "", other_ids=None):
        return False  # #852: the real rule is tested through `GhCli` in test_issue852_released_own_note.py

    def last_comment_body(self, number: int) -> str | None:
        return self.comment_body.get(number)

    def checkpoint_holder(self, number: int, session_ids: tuple = ()):  # #918: real rule via `GhCli`
        return None, False

    def last_assigned_at(self, number: int) -> datetime | None:
        self.assigned_reads.append(number)
        return self.assigned_at.get(number, datetime.now(timezone.utc) + timedelta(days=1))

    def ready_for_agent_issues(self) -> list[IssueSummary]:
        return list(self._candidates)

    def claimed_issues(self) -> list[int] | None:
        if self.claims_fail:
            return None
        return sorted(n for n, s in self.issues.items() if s.open and "ready-for-agent" in s.labels and s.assignees)

    def issue_status(self, number: int) -> IssueStatus:
        return self.issues[number]

    def comment(self, number: int, body: str) -> bool:
        self.comments.append((number, body))
        return True

    def remove_assignee(self, number: int, login: str = "@me") -> bool:
        self.removed_assignees.append(number)
        status = self.issues.get(number)
        if status is not None:
            self.issues[number] = IssueStatus(number=number, open=status.open, labels=status.labels, assignees=())
        return True

    def unlabelled_open_issues(self) -> list[dict]:
        """#874: the merge-result relabel read; no such issue in this fake."""
        return []

    def add_label(self, number: int, label: str) -> bool:
        self.added_labels.append((number, label))
        status = self.issues.get(number)
        if status is not None and label not in status.labels:
            self.issues[number] = IssueStatus(number=number, open=status.open,
                                              labels=(*status.labels, label), assignees=status.assignees)
        return True

    def remove_label(self, number: int, label: str) -> bool:
        self.removed_labels.append((number, label))
        status = self.issues.get(number)
        if status is not None and label in status.labels:
            self.issues[number] = IssueStatus(number=number, open=status.open,
                                              labels=tuple(l for l in status.labels if l != label),
                                              assignees=status.assignees)
        return True

    # the autopilot seat (#116) runs beside ORCH every tick; no goal in these ORCH-only tests.
    def autopilot_goals(self) -> list[int]:
        return []

    def latest_checkpoint_comment(self, number: int) -> str | None:
        return None

    def parking_mark(self, number: int, end_at: str = "", session_ids: tuple = ()):  # #359: no mark unless a test sets one
        return None

    # test helpers: what ORCH itself does on GitHub
    def claim(self, number: int) -> None:
        self.issues[number] = IssueStatus(number=number, open=True, labels=READY, assignees=("bot",))

    def close(self, number: int) -> None:
        self.issues[number] = IssueStatus(number=number, open=False, labels=(), assignees=())

    def park(self, number: int) -> None:
        self.issues[number] = IssueStatus(number=number, open=True, labels=(), assignees=())


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _issue(number: int, day: int = 1, *, blockers: tuple[int, ...] = (),
           labels: tuple[str, ...] = ()) -> IssueSummary:
    return IssueSummary(number=number, created_at=f"2026-09-{day:02d}T00:00:00Z",
                        open_blockers=blockers, labels=labels)


def _dispatcher(tmp_path: Path, *, claude: FakeClaude, gh: FakeGh, max_hours: float = 4.0,
                clock: FakeClock | None = None, orch_offer_max: int = 99) -> Dispatcher:
    # 99: these tests are about picking and holding; the offer cap (#4) has its own file, tests/test_orch_offer_max.py
    return Dispatcher(claude=claude, gh=gh, max_hours=max_hours, events_file=tmp_path / "events.jsonl",
                      orch_offer_max=orch_offer_max,
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=clock or FakeClock())


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _start(tmp_path: Path, candidates: list[IssueSummary], messages: list[str] | None = None,
           **kwargs) -> tuple[FakeClaude, FakeGh, Dispatcher, str]:
    claude = FakeClaude()
    gh = FakeGh(candidates=candidates)
    d = _dispatcher(tmp_path, claude=claude, gh=gh, **kwargs)
    if messages is not None:
        d.log = messages.append
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    return claude, gh, d, live_id


# -- picking and offering ---------------------------------------------------------------------

def test_60a_five_ready_issues_start_one_session_offered_all_five(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(n, n) for n in (5, 1, 4, 2, 3)])

    assert len(claude.started) == 1
    offered, cmd = claude.started[0]
    assert offered == [1, 2, 3, 4, 5]  # oldest first
    assert cmd[-1] == build_prompt([1, 2, 3, 4, 5])
    assert cmd[-1].startswith("Take every ready issue (ready now, in dispatch order: #1, #2, #3, #4, #5)")
    assert cmd[3] == f"orch {REPO_NAME} #1 #2 #3 #4 #5"

    for issue in (1, 2, 3):  # it claims three (#4, #5 overlap); another issue gets ready meanwhile
        gh.claim(issue)
    gh._candidates = [_issue(4, 4), _issue(5, 5), _issue(6, 6)]
    for _ in range(3):
        d.tick()
    assert len(claude.started) == 1  # never a second session while this one lives
    assert d.current is not None and d.current.held == {1, 2, 3}
    assert json.loads((tmp_path / "orch_session.json").read_text())["held"] == [1, 2, 3]


def test_no_candidates_starts_nothing(tmp_path: Path):
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh())

    d.tick()

    assert claude.started == [] and _events(tmp_path) == []


def test_60d_an_issue_with_an_open_native_blocker_is_never_offered(tmp_path: Path):
    messages: list[str] = []
    claude, _, _, _ = _start(tmp_path, [_issue(33, 1, blockers=(42,)), _issue(54, 20)], messages)

    assert claude.started[0][0] == [54]
    assert any("#33 (by #42)" in line for line in messages)


def test_53_only_blocked_candidates_start_nothing(tmp_path: Path):
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(candidates=[_issue(51, blockers=(42,))]))

    d.tick()

    assert claude.started == [] and _events(tmp_path) == []


def test_53_parse_candidates_ignores_closed_blockers():
    data = [
        {"number": 33, "createdAt": "2026-09-01T00:00:00Z", "comments": [],
         "blockedBy": {"nodes": [{"number": 42, "state": "OPEN"}, {"number": 6, "state": "CLOSED"}],
                       "totalCount": 2}},
        {"number": 36, "createdAt": "2026-09-02T00:00:00Z", "comments": [],
         "blockedBy": {"nodes": [{"number": 58, "state": "CLOSED"}], "totalCount": 1}},
        {"number": 54, "createdAt": "2026-09-03T00:00:00Z", "comments": []},
    ]

    parsed = {c.number: c.open_blockers for c in parse_candidates(data)}

    assert parsed == {33: (42,), 36: (), 54: ()}


# -- #192: issue_ready_seen / issue_taken, once per ready episode --------------------------------

def test_192_an_issue_offered_to_a_new_session_is_seen_then_taken_once(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])

    events = _events(tmp_path)
    seens = [e for e in events if e["kind"] == "issue_ready_seen"]
    takens = [e for e in events if e["kind"] == "issue_taken"]
    assert seens == [{"ts": seens[0]["ts"], "kind": "issue_ready_seen", "issue": 1}]
    assert takens == [{"ts": takens[0]["ts"], "kind": "issue_taken", "issue": 1, "how": "offered"}]

    gh.claim(1)
    gh._candidates = []  # a real `gh` would drop the now-assigned #1 from its own ready list
    d.tick()  # no repeat: the episode already ended
    events = _events(tmp_path)
    assert len([e for e in events if e["kind"] == "issue_ready_seen"]) == 1
    assert len([e for e in events if e["kind"] == "issue_taken"]) == 1


def test_192_an_issue_claimed_by_a_live_session_is_seen_then_taken_once(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    gh.claim(1)
    d.tick()  # #1 held; its own issue_ready_seen/issue_taken(offered) already happened at start

    gh._candidates = [_issue(2, 2)]  # #2 becomes ready while the session sleeps
    d.tick()
    events = _events(tmp_path)
    seens_2 = [e for e in events if e["kind"] == "issue_ready_seen" and e["issue"] == 2]
    assert seens_2 == [{"ts": seens_2[0]["ts"], "kind": "issue_ready_seen", "issue": 2}]
    assert [e for e in events if e["kind"] == "issue_taken" and e["issue"] == 2] == []

    gh.claim(2)
    gh._candidates = []  # a real `gh` would drop the now-assigned #2 from its own ready list
    d.tick()
    events = _events(tmp_path)
    takens_2 = [e for e in events if e["kind"] == "issue_taken" and e["issue"] == 2]
    assert takens_2 == [{"ts": takens_2[0]["ts"], "kind": "issue_taken", "issue": 2, "how": "claimed"}]

    d.tick()  # no repeat: the episode already ended
    events = _events(tmp_path)
    assert len([e for e in events if e["kind"] == "issue_ready_seen" and e["issue"] == 2]) == 1
    assert len([e for e in events if e["kind"] == "issue_taken" and e["issue"] == 2]) == 1


def test_192_an_issue_that_drops_off_the_queue_is_forgotten_without_issue_taken(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    gh.claim(1)
    d.tick()

    gh._candidates = [_issue(2, 2)]
    d.tick()
    assert any(e["kind"] == "issue_ready_seen" and e["issue"] == 2 for e in _events(tmp_path))

    gh._candidates = []  # #2 never claimed, just drops off the ready queue (e.g. parked)
    d.tick()

    assert not any(e["kind"] == "issue_taken" and e["issue"] == 2 for e in _events(tmp_path))


# -- #137: the dispatcher wakes the live ORCH session about a new ready issue --------------------

def test_137_a_new_ready_issue_wakes_the_live_session_once(tmp_path: Path):
    """Number 1: a live session holds #1 with #B (here #2) unassigned and unblocked appearing:
    within 1 tick exactly 1 wake naming #2, and no second session started."""

    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    gh.claim(1)
    d.tick()  # this tick's refresh_claims learns #1 is held
    assert claude.waked == []

    gh._candidates = [_issue(2, 2)]  # #2 becomes ready while the session sleeps
    report = d.tick()

    assert claude.waked == [(f"orch {REPO_NAME} #1", build_wake_message([2], session=live_id))]
    assert report.woke == (2,)
    assert len(claude.started) == 1  # never a second session


def test_137_an_unclaimed_wake_repeats_after_the_cooldown_capped_at_three(tmp_path: Path):
    """Number 2: #B stays unclaimed: 0 further wakes inside the cooldown, then 1 more once it
    elapses; at most MAX_WAKES_PER_ISSUE wakes for the same issue by the same session."""

    clock = FakeClock()
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)], clock=clock)
    gh.claim(1)
    d.tick()
    gh._candidates = [_issue(2, 2)]

    d.tick()
    assert len(claude.waked) == 1  # the first wake, this tick

    clock.advance(WAKE_COOLDOWN_SECONDS - 1)
    d.tick()
    assert len(claude.waked) == 1  # still inside the cooldown: no repeat

    clock.advance(1)
    d.tick()
    assert len(claude.waked) == 2  # the cooldown elapsed: one more

    clock.advance(WAKE_COOLDOWN_SECONDS)
    d.tick()
    assert len(claude.waked) == MAX_WAKES_PER_ISSUE == 3

    clock.advance(WAKE_COOLDOWN_SECONDS)
    d.tick()
    assert len(claude.waked) == 3  # the cap holds: no fourth wake


def test_137_a_claimed_candidate_gets_no_further_wake(tmp_path: Path):
    """Number 3: #B is claimed after the wake: 0 further wakes."""

    clock = FakeClock()
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)], clock=clock)
    gh.claim(1)
    d.tick()
    gh._candidates = [_issue(2, 2)]
    d.tick()
    assert len(claude.waked) == 1

    gh.claim(2)  # ORCH claimed it; a real `gh` would drop it from --search no:assignee too
    gh._candidates = []
    clock.advance(WAKE_COOLDOWN_SECONDS * 2)
    d.tick()

    assert len(claude.waked) == 1


def test_137_a_natively_blocked_candidate_is_never_woken(tmp_path: Path):
    """Number 4, the blocker half: #B has an open native blocker -- 0 wakes. (The assignee half is
    unreachable by construction: `GhCli.ready_for_agent_issues` already searches `no:assignee`, the
    same candidate pool `_maybe_start` offers a fresh ORCH, so an assigned issue never becomes a
    wake candidate either.)"""

    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    gh.claim(1)
    d.tick()
    gh._candidates = [_issue(2, 2, blockers=(9,))]

    d.tick()

    assert claude.waked == []


def test_137_wake_target_is_the_adopted_sessions_own_name(tmp_path: Path):
    """The wake's target is read from the live listing's `name` field at adoption, not rebuilt."""

    claude = FakeClaude()
    claude.add_session("058fec17", name="orch #60 #61", state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh(candidates=[_issue(60), _issue(61, 2), _issue(62, 3)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()  # adopts "058fec17"
    gh.claim(60)
    gh.claim(61)

    report = d.tick()  # learns held = {60, 61}; #62 is already a candidate, woken this same tick

    assert claude.waked == [("orch #60 #61", build_wake_message([62], session="058fec17"))]
    assert report.woke == (62,)


def test_137_a_wake_failure_still_counts_toward_the_cap(tmp_path: Path):
    """A failed wake is logged and counted, never retried in a storm."""

    messages: list[str] = []
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)], messages)
    claude.wake_fails = True
    gh.claim(1)
    d.tick()
    gh._candidates = [_issue(2, 2)]

    d.tick()

    assert len(claude.waked) == 1
    assert any("failed" in m for m in messages)


def test_an_issue_claimed_before_the_session_started_is_not_held(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(7)])
    gh.claim(99)  # a human's claim, older than the session
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()
    gh.claim(7)
    d.tick()

    assert d.current is not None and d.current.held == {7}


# -- the end of a session: every held issue resolved on its own ---------------------------------

def test_60b_session_ends_holding_three_two_closed_one_parked_and_the_next_starts(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _start(tmp_path, [_issue(n, n) for n in (1, 2, 3, 4)], messages)
    for issue in (1, 2, 3):
        gh.claim(issue)
    d.tick()
    gh.close(1)
    d.tick()  # #1 closed mid-session: no longer claimed, still held
    gh.close(2)
    gh.park(3)
    gh._candidates = [_issue(4, 4)]
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.ended == live_id and report.why == "done"
    assert report.outcomes == {1: "closed", 2: "closed", 3: "parked"}
    assert claude.stopped == [live_id]
    assert gh.comments == [] and gh.removed_assignees == []
    ends = [e for e in _events(tmp_path) if e["kind"] == "orch_end"]
    assert [(e["issue"], e["outcome"]) for e in ends] == [(1, "closed"), (2, "closed"), (3, "parked")]
    assert f"orch {live_id} ended (done): #1 closed, #2 closed, #3 parked" in messages

    d.tick()  # the next tick starts a fresh session
    assert [offered for offered, _ in claude.started] == [[1, 2, 3, 4], [4]]


def test_60c_one_issue_left_claimed_takes_the_no_checkpoint_path_alone(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(n, n) for n in (1, 2, 3)])
    for issue in (1, 2, 3):
        gh.claim(issue)
    d.tick()
    gh.close(1)
    gh.park(2)  # #3 stays assigned and `ready-for-agent`
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.outcomes == {1: "closed", 2: "parked", 3: "no_checkpoint"}
    assert [n for n, _ in gh.comments] == [3] and live_id in gh.comments[0][1]
    assert gh.removed_assignees == [3]
    assert gh.added_labels == []  # first failure: not yet capped


def test_third_consecutive_no_checkpoint_failure_labels_needs_info(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    for _ in range(MAX_CONSECUTIVE_NO_CHECKPOINT):
        gh._candidates = [_issue(9)]
        d.tick()
        live_id = claude.list_sessions()[0]["id"]
        gh.claim(9)
        claude.set_state(live_id, state="done", status="idle")
        report = d.tick()
        assert report.outcomes == {9: "no_checkpoint"}

    assert gh.added_labels[-1] == (9, "needs-info")
    assert gh.removed_labels[-1] == (9, "ready-for-agent")


def test_a_session_that_claimed_nothing_ends_with_a_nothing_claimed_event(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.ended == live_id and report.outcomes == {}
    ends = [e for e in _events(tmp_path) if e["kind"] == "orch_end"]
    assert ends[-1]["outcome"] == "nothing_claimed"


# -- subagents keep a `done` session alive -------------------------------------------------------

def _subagent(projects: Path, session_uuid: str, agent_id: str) -> None:
    sub = projects / session_uuid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.jsonl").write_text("{}\n", encoding="utf-8")


def _notify(projects: Path, session_uuid: str, agent_id: str, status: str = "completed") -> None:
    """The shape Claude Code 2.1.280 writes (measured on #60's own transcript): a `queue-operation`
    line whose `content` holds the notification, newlines between the tags."""
    text = (f"<task-notification>\n<task-id>{agent_id}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
            f"<status>{status}</status>\n<summary>Agent finished</summary>\n</task-notification>")
    line = json.dumps({"type": "queue-operation", "operation": "enqueue", "content": text})
    with (projects / f"{session_uuid}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_a_done_session_with_running_subagents_is_not_ended_until_they_finish(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _start(tmp_path, [_issue(1), _issue(2, 2)], messages)
    projects = tmp_path / "projects"
    uuid = f"uuid-{live_id}"
    gh.claim(1)
    gh.claim(2)
    _subagent(projects, uuid, "aaa")
    _subagent(projects, uuid, "bbb")
    claude.set_state(live_id, state="done", status="idle")  # its turn ended; the workers run

    d.tick()
    d.tick()
    _notify(projects, uuid, "aaa")
    d.tick()

    assert claude.stopped == [] and d.current is not None
    assert sum("subagent(s) running" in m for m in messages) == 2  # 2 running, then 1: logged per change
    _notify(projects, uuid, "bbb", status="failed")
    gh.close(1)
    gh.close(2)
    report = d.tick()
    assert report.outcomes == {1: "closed", 2: "closed"} and claude.stopped == [live_id]


def test_running_subagent_ids_reads_the_transcripts(tmp_path: Path):
    _subagent(tmp_path, "u", "x1")
    _subagent(tmp_path, "u", "x2")
    _notify(tmp_path, "u", "x1")
    _notify(tmp_path, "u", "x2", status="running")

    assert running_subagent_ids(tmp_path, "u") == {"x2"}
    assert running_subagent_ids(tmp_path, "missing") == set()
    assert running_subagent_ids(tmp_path, None) == set()


# -- an idle session in a live state (#58) -------------------------------------------------------

def test_an_idle_session_still_holding_an_issue_waits_on_the_owner_logged_once(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _start(tmp_path, [_issue(11)], messages)
    gh.claim(11)
    gh._candidates = [_issue(12, 2)]
    claude.set_state(live_id, state="working", status="idle")  # a permission prompt
    claude._sessions[live_id]["waitingFor"] = "permission prompt"  # #766: the listing says so

    d.tick()
    d.tick()

    assert len(claude.started) == 1 and claude.stopped == []
    assert sum("waiting on the owner" in m for m in messages) == 1


def test_58a_idle_working_session_whose_issues_are_all_parked_ends_and_the_next_starts(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(24)])
    gh.claim(24)
    d.tick()
    gh.park(24)
    claude.set_state(live_id, state="working", status="idle")
    gh._candidates = [_issue(53, 2)]

    report = d.tick()

    assert report.why == "idle" and report.outcomes == {24: "parked"}
    assert claude.stopped == [live_id] and d.current is None
    d.tick()
    assert [offered for offered, _ in claude.started] == [[24], [53]]


@pytest.mark.parametrize("final, outcome", [
    (IssueStatus(number=30, open=False, labels=READY, assignees=("bot",)), "closed"),
    (IssueStatus(number=30, open=True, labels=("needs-info",), assignees=()), "needs_info"),
])
def test_58cd_idle_working_session_of_a_closed_or_needs_info_issue_ends(tmp_path: Path, final, outcome):
    claude, gh, d, live_id = _start(tmp_path, [_issue(30)])
    gh.claim(30)
    d.tick()
    gh.issues[30] = final
    claude.set_state(live_id, state="working", status="idle")

    report = d.tick()

    assert report.outcomes == {30: outcome} and claude.stopped == [live_id] and gh.comments == []


def test_unreadable_claims_leave_an_idle_session_waiting(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(32)])
    gh.claims_fail = True
    claude.set_state(live_id, state="working", status="idle")

    report = d.tick()

    assert report.ended is None and d.current is not None and claude.stopped == []


def test_a_busy_session_is_left_alone(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(31)])

    d.tick()
    d.tick()

    assert d.current is not None and claude.stopped == []


# -- dead states (#58 1b) --------------------------------------------------------------------------

def test_58f_stopped_session_is_finished_without_a_second_stop(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(33)])
    gh.claim(33)
    d.tick()
    gh.park(33)
    claude.add_session(live_id, state="stopped")

    report = d.tick()

    assert report.why == "stopped" and report.outcomes == {33: "parked"}
    assert claude.stopped == [] and d.current is None


def test_58f_any_other_dead_state_ends_the_session(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(34)])
    gh.claim(34)
    d.tick()
    gh.close(34)
    claude.set_state(live_id, state="failed", status="idle")

    report = d.tick()

    assert report.outcomes == {34: "closed"} and claude.stopped == [live_id]


# -- adoption by a restarted dispatcher (#58) ------------------------------------------------------

@pytest.mark.parametrize("name", ["orch #58", "orch #58 #59"])
def test_58e_a_live_orch_session_is_adopted_and_nothing_is_started(tmp_path: Path, name):
    claude = FakeClaude()
    claude.add_session("old-done", name="orch #6", state="done", status="idle", startedAt=1)
    claude.add_session("abc12345", name=name, state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 60_000)
    claude.add_session("chat", name="Orch", state="working", status="busy", kind="interactive")
    claude.add_session("other", name="orchestra", state="working", status="busy")
    gh = FakeGh(candidates=[_issue(53)])
    gh.claim(58)
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.log = messages.append

    d.tick()

    assert claude.started == [] and d.current is not None and d.current.session == "abc12345"
    assert any(e["kind"] == "orch_adopt" for e in _events(tmp_path))
    assert any(m.startswith(f"adopted live {name} (abc12345)") for m in messages)

    d.tick()  # the adopted session's claim becomes held
    assert d.current.held == {58} and claude.started == []

    gh.close(58)
    claude.set_state("abc12345", state="done", status="idle")
    report = d.tick()
    assert report.outcomes == {58: "closed"} and claude.stopped == ["abc12345"]
    d.tick()
    assert [offered for offered, _ in claude.started] == [[53]]


def test_58e_an_adopted_session_keeps_its_age_for_max_hours(tmp_path: Path):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
    claude = FakeClaude()
    claude.add_session("old", name="orch #40", state="working", status="busy",
                       startedAt=int((now.timestamp() - 5 * 3600) * 1000))
    gh = FakeGh()
    gh.claim(40)
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=None, projects_dir=tmp_path, clock=FakeClock(100_000.0), timestamp=lambda: now)

    d.tick()  # adopt
    report = d.tick()  # over --max-hours

    assert report.why == "timeout" and report.outcomes == {40: "timeout"} and claude.stopped == ["old"]


# -- #208: a session belongs to a dispatcher only when its cwd is this dispatcher's own root --------

def test_208_session_matches_root_helper(tmp_path: Path):
    here = tmp_path / "repo-a"
    here.mkdir()
    assert session_matches_root({"cwd": str(here)}, here) is True
    assert session_matches_root({"cwd": str(here) + "\\"}, here) is True  # trailing separator
    assert session_matches_root({"cwd": str(here).upper()}, here) is True  # case
    assert session_matches_root({"cwd": str(tmp_path / "repo-b")}, here) is False
    assert session_matches_root({}, here) is True  # no cwd: same verdict find_own_session_id gives


def test_208_one_of_two_live_orch_sessions_with_a_foreign_cwd_is_adopted(tmp_path: Path):
    """Row 1: a listing of 2 live `orch ...` sessions, one with this root as `cwd`, one with
    another folder -> 1 adopted, 1 `orch_adopt` event; the foreign one is never in `self.sessions`."""

    foreign_root = tmp_path / "orch-port-test"
    claude = FakeClaude()
    claude.add_session("own-1", name="orch #100", state="working", status="busy",
                       startedAt=1, cwd=str(ROOT))
    claude.add_session("foreign-1", name="orch #200", state="working", status="busy",
                       startedAt=1, cwd=str(foreign_root))
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert [c.session for c in d.sessions] == ["own-1"]
    assert [e for e in _events(tmp_path) if e["kind"] == "orch_adopt"] == [
        {"ts": _events(tmp_path)[-1]["ts"], "kind": "orch_adopt", "session": "own-1",
         "name": "orch #100", "finishing": False}]


def test_208_own_session_started_when_the_only_live_one_is_foreign(tmp_path: Path):
    """Row 2: same listing, one eligible issue, this dispatcher's own session absent -> 1
    session started (0 before the fix)."""

    foreign_root = tmp_path / "orch-port-test"
    claude = FakeClaude()
    claude.add_session("foreign-1", name="orch #200", state="working", status="busy",
                       startedAt=1, cwd=str(foreign_root))
    gh = FakeGh(candidates=[_issue(1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert len(claude.started) == 1
    assert [offered for offered, _ in claude.started] == [[1]]


def test_208_a_foreign_live_session_is_never_woken(tmp_path: Path):
    """Row 3: a tick with a foreign live session (already holding the only ready issue, so this
    dispatcher's own session is never woken about it either) plus a ready issue -> 0 wake calls to
    the foreign session: it is never adopted, so `_maybe_wake` (which only loops `self.sessions`)
    never runs for it."""

    foreign_root = tmp_path / "orch-port-test"
    claude, gh, d, own_id = _start(tmp_path, [_issue(1)])  # this dispatcher's own live session
    claude.add_session("foreign-1", name="orch #200", state="working", status="busy",
                       startedAt=1, cwd=str(foreign_root))
    gh._candidates = [_issue(2, 2)]  # a further ready issue while both sessions are live

    d.tick()

    targets = [target for target, _ in claude.waked]
    assert "orch #200" not in targets and "foreign-1" not in targets


# -- timeout ---------------------------------------------------------------------------------------

def test_a_session_past_max_hours_is_stopped_and_only_its_claimed_issues_are_released(tmp_path: Path):
    clock = FakeClock()
    claude, gh, d, live_id = _start(tmp_path, [_issue(13), _issue(14, 2)], max_hours=1.0, clock=clock)
    gh.claim(13)
    gh.claim(14)
    d.tick()
    gh.close(14)
    clock.advance(1.5 * 3600)

    report = d.tick()

    assert claude.stopped == [live_id]
    assert report.why == "timeout" and report.outcomes == {13: "timeout", 14: "closed"}
    assert [n for n, _ in gh.comments] == [13] and gh.removed_assignees == [13]
    timeouts = [e for e in _events(tmp_path) if e["kind"] == "orch_timeout"]
    assert timeouts[-1] == {"ts": timeouts[-1]["ts"], "kind": "orch_timeout",
                            "issue": 13, "session": live_id}
    assert d.current is None


# -- the exact command line ---------------------------------------------------------------------

def test_build_command():
    assert build_command([54, 55, 56, 57], "claude.exe") == [
        "claude.exe", "--bg", "--name", f"orch {REPO_NAME} #54 #55 #56 #57", "--agent", "orch",
        "--effort", "high", "--permission-mode", "bypassPermissions",
        "Take every ready issue (ready now, in dispatch order: #54, #55, #56, #57). Follow docs/agents/orch.md.",
    ]
    assert build_command(list(range(1, 9)))[3] == f"orch {REPO_NAME} #1 #2 #3 #4 #5 +3"


# -- #137: ClaudeCli.wake's own success detection ------------------------------------------------

def test_137_wake_reads_success_from_the_json_envelope_result_not_the_exit_code(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        import subprocess as sp
        return sp.CompletedProcess(cmd, returncode=0,
                                   stdout='{"result": "sent. {\\"success\\":true}"}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    cli = ClaudeCli("claude.exe")

    ok, detail = cli.wake("orch #60", "hello")

    assert ok is True and "success" in detail
    assert calls[0][:2] == ["claude.exe", "-p"] and "--allowedTools=SendMessage" in calls[0]


def test_137_wake_is_false_when_rc_is_0_but_the_envelope_says_failure(monkeypatch):
    """#123: rc 0 happens while `SendMessage` itself failed -- the exit code is never trusted."""

    def fake_run(cmd, **kwargs):
        import subprocess as sp
        return sp.CompletedProcess(cmd, returncode=0, stdout='{"result": "{\\"success\\":false}"}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    cli = ClaudeCli("claude.exe")

    ok, _detail = cli.wake("orch #60", "hello")

    assert ok is False


# -- CLAUDE_EXE override and newest-version discovery --------------------------------------------

def test_claude_exe_override_wins(tmp_path: Path):
    assert find_claude_exe({"CLAUDE_EXE": r"C:\somewhere\claude.exe"}) == Path(r"C:\somewhere\claude.exe")


def test_claude_exe_discovers_the_newest_version_dir(tmp_path: Path):
    base = tmp_path / "Claude" / "claude-code"
    for version in ("2.0.100", "2.1.280", "2.1.9", "not-a-version"):
        (base / version).mkdir(parents=True)
        (base / version / "claude.exe").write_bytes(b"MZ")
    found = find_claude_exe({"APPDATA": str(tmp_path)})
    assert found == base / "2.1.280" / "claude.exe"


def test_claude_exe_raises_when_nothing_is_found(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # a machine with `claude` on PATH would find it
    with pytest.raises(SystemExit):
        find_claude_exe({"APPDATA": str(tmp_path)})


def test_claude_exe_prefers_the_msix_localcache_over_appdata(tmp_path: Path):
    """The MSIX package's `LocalCache` is where a plain terminal (this dispatcher) finds
    `claude.exe`; `%APPDATA%\\Claude\\claude-code` only resolves inside the app's own container
    (the lead, issue #50) -- so LocalCache wins when both are present."""

    local = tmp_path / "local"
    package = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    for version in ("2.1.275", "2.1.280"):
        (package / version).mkdir(parents=True)
        (package / version / "claude.exe").write_bytes(b"MZ")
    appdata_base = tmp_path / "roaming" / "Claude" / "claude-code" / "9.9.9"
    appdata_base.mkdir(parents=True)

    found = find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")})

    assert found == package / "2.1.280" / "claude.exe"


def test_claude_exe_falls_back_to_appdata_when_no_package_is_found(tmp_path: Path):
    local = tmp_path / "local"  # no Packages dir under it
    appdata_base = tmp_path / "roaming" / "Claude" / "claude-code"
    (appdata_base / "2.1.280").mkdir(parents=True)
    (appdata_base / "2.1.280" / "claude.exe").write_bytes(b"MZ")

    found = find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")})

    assert found == appdata_base / "2.1.280" / "claude.exe"


def test_claude_exe_falls_back_to_path_last(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\on\path\claude.exe" if name == "claude" else None)
    found = find_claude_exe({"LOCALAPPDATA": str(tmp_path / "no-packages"), "APPDATA": str(tmp_path / "no-appdata")})
    assert found == Path(r"C:\on\path\claude.exe")


# -- #60: another chat's claim on the shared account is never held ---------------------------------

def test_60_an_adopted_session_never_holds_another_chats_claim(tmp_path: Path):
    """The live case of 20:11: `orch #60` adopted while #35 and #42 were assigned to the
    "Pocock migration" chat on the same account; they must never be held, commented or unassigned."""
    claude = FakeClaude()
    claude.add_session("058fec17", name="orch #60", state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    for issue in (35, 42, 60):
        gh.claim(issue)
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()  # adopt
    d.tick()
    gh.claim(61)  # a later claim of the session itself
    d.tick()
    assert d.current.held == {60, 61}

    gh.close(60)
    gh.close(61)
    claude.set_state("058fec17", state="done", status="idle")
    report = d.tick()
    assert report.outcomes == {60: "closed", 61: "closed"}
    assert gh.comments == [] and gh.removed_assignees == []
    assert gh.claimed_issues() == [35, 42]


def test_60_a_restarted_dispatcher_restores_held_and_preexisting_from_its_state_file(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(5), _issue(6, 2)])
    gh.claim(5)
    gh.claim(7)  # claimed by the session from a later queue read, not in its name
    d.tick()
    assert d.current.held == {5, 7}

    claude.add_session(live_id, name="orch #5 #6", state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 60_000)
    d2 = _dispatcher(tmp_path, claude=claude, gh=gh)  # same state file
    d2.tick()
    assert d2.current.held == {5, 7} and d2.current.preexisting == frozenset()


# -- #86: `claude stop` alone leaves the process running; `end_session` must finish the job ---------

def test_86_end_session_falls_through_to_end_process_when_stop_leaves_the_pid_listed(tmp_path: Path):
    claude = FakeClaude()
    claude.add_session("cdc0346c", name="orch #60", kind="background", state="working",
                       status="idle", pid=34356)
    claude.stop_leaves_process = True

    ok = end_session(claude, "cdc0346c")

    assert ok is True
    assert claude.stopped == ["cdc0346c"]
    assert claude.ended_pids == [34356]
    assert claude.list_sessions() == []  # the pid is gone from the listing


def test_86_end_session_uses_the_freshest_pid_not_a_stale_one(tmp_path: Path):
    """Pid reuse guard: `end_session` must read the pid right before acting, not from an earlier
    listing -- here the id keeps the same id but the pid the freshest read reports has changed."""

    class ReusedPidClaude:
        def __init__(self) -> None:
            self.calls = 0
            self.ended_pids: list[int] = []

        def stop(self, session_id: str) -> bool:
            return True

        def list_sessions(self) -> list[dict]:
            self.calls += 1
            # a stale pid (11111) would be wrong; only the freshest read (22222) is correct.
            pid = 11111 if self.calls == 0 else 22222
            return [{"id": "x", "kind": "background", "pid": pid}]

        def end_process(self, pid: int) -> bool:
            self.ended_pids.append(pid)
            return True

    claude = ReusedPidClaude()
    assert end_session(claude, "x") is True
    assert claude.ended_pids == [22222]


def test_86_end_session_never_ends_an_interactive_session(tmp_path: Path):
    claude = FakeClaude()
    claude.add_session("11243e8a", name="Orchestration session", kind="interactive", status="idle")
    claude.stop_leaves_process = True  # an interactive session is never popped by a real `stop`

    with pytest.raises(GuardError):
        end_session(claude, "11243e8a")

    assert claude.ended_pids == []  # never even asked to end a process


def test_86_end_session_logs_when_the_process_survives_the_end_attempt(tmp_path: Path):
    claude = FakeClaude()
    claude.add_session("bcca11d5", name="orch #64 #65 #66", kind="background", state="working",
                       status="idle", pid=39236)
    claude.stop_leaves_process = True
    claude.end_process_fails = {39236}
    messages: list[str] = []

    ok = end_session(claude, "bcca11d5", log=messages.append)

    assert ok is False
    assert any("claude end bcca11d5 failed" in m for m in messages)


def test_86_a_session_already_gone_from_the_listing_counts_as_ended(tmp_path: Path):
    claude = FakeClaude()  # never added: `stop` finds nothing to pop, `list_sessions` is empty

    assert end_session(claude, "gone") is True
    assert claude.ended_pids == []


def test_86_finish_and_timeout_route_through_end_session_not_a_bare_stop(tmp_path: Path):
    """`_finish`/`_timeout` must call the real end path, not merely `claude.stop`: with the #86 bug
    modelled (`stop_leaves_process`), the session's process must still end up gone."""

    claude, gh, d, live_id = _start(tmp_path, [_issue(70)])
    gh.claim(70)
    d.tick()
    gh.close(70)
    session = claude._sessions[live_id]
    session["pid"] = 55555
    claude.stop_leaves_process = True
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.outcomes == {70: "closed"}
    assert claude.ended_pids == [55555]
    assert claude.list_sessions() == []


# -- #319: exactly one area label ------------------------------------------------------------------

def _area_candidates() -> list[IssueSummary]:
    return [_issue(1, labels=("area:runs",)), _issue(2), _issue(3, labels=("area:runs", "area:builds"))]


def test_319_candidates_without_exactly_one_area_are_dropped_and_logged_once(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.agentops.dispatch.area_labels", lambda: ["area:runs", "area:builds"])
    messages: list[str] = []
    claude, gh, d, _ = _start(tmp_path, _area_candidates(), messages)
    assert claude.started[0][0] == [1]
    lines = [m for m in messages if m.startswith("skipped #")]
    assert len(lines) == 2 and "#2: no area" in lines[0] and "#3: two areas" in lines[1]
    d._eligible_candidates(offering=True)
    assert len([m for m in messages if m.startswith("skipped #")]) == 2


def test_319_areas_empty_offers_every_candidate(tmp_path: Path):
    claude, gh, d, _ = _start(tmp_path, _area_candidates(), [])
    assert claude.started[0][0] == [1, 2, 3]


# -- #378: a stale assignee is nobody's claim ------------------------------------------------------

def test_378_row1_a_reopened_still_assigned_issue_is_stale_unassigned_and_woken(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, a_id = _start(tmp_path, [_issue(10)], messages)  # session A offered #10
    gh.claim(10)
    d.tick()
    gh.close(10)
    gh._candidates = [_issue(20)]
    claude.set_state(a_id, state="done", status="idle")
    d.tick()  # A ends
    d.tick()  # B starts, offered #20
    b_id = claude.list_sessions()[0]["id"]
    assert b_id != a_id and claude.started[1][0] == [20]

    # #10 reopens after B's start, assignee still set; its last `assigned` event is older than B
    gh.issues[10] = IssueStatus(number=10, open=True, labels=READY, assignees=("bot",))
    gh.assigned_at[10] = datetime.now(timezone.utc) - timedelta(hours=3)
    gh._candidates = [_issue(10), _issue(20)]  # what a real `gh` lists once the assignee is gone
    claude.waked.clear()
    d.tick()

    assert 10 not in d.current.held
    assert "orch-dispatch: #10 stale assignee (no claim since %s started), unassigned" % b_id in messages
    assert gh.removed_assignees == [10]
    assert any("#10" in message for _, message in claude.waked)


def test_378_row2_an_issue_assigned_after_the_start_and_not_offered_is_held(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)], messages)
    gh._candidates = [_issue(2, 2)]
    gh.claim(2)
    gh.assigned_at[2] = datetime.now(timezone.utc) + timedelta(seconds=5)
    d.tick()

    assert d.current.held == {2}
    assert f"orch {live_id} claimed #2" in messages
    assert gh.removed_assignees == []
    assert gh.assigned_reads == [2]


def test_378_an_offered_issue_is_held_without_an_events_read(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    gh.claim(1)
    gh.assigned_at[1] = datetime.now(timezone.utc) - timedelta(days=9)  # would be stale if read
    d.tick()

    assert d.current.held == {1}
    assert gh.assigned_reads == [] and gh.removed_assignees == []


# -- #481: claude.exe one level deeper (<ver>/<hash>/claude.exe) and the tick-failed frame -------------

def _touch_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def _package_base(local: Path) -> Path:
    return local / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude" / "claude-code"


def test_481_row0_exe_one_level_deeper_under_the_newest_version(tmp_path: Path):
    local = tmp_path / "local"
    base = _package_base(local)
    _touch_exe(base / "2.1.284" / "3f4bed3e44ad" / "claude.exe")
    want = _touch_exe(base / "2.1.286" / "635c1867224a" / "claude.exe")
    (tmp_path / "roaming").mkdir()
    found = find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")})
    assert found == want


def test_481_row1a_flat_layout_still_found(tmp_path: Path):
    local = tmp_path / "local"
    want = _touch_exe(_package_base(local) / "2.1.280" / "claude.exe")
    (tmp_path / "roaming").mkdir()
    assert find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")}) == want


def test_481_row1b_newest_dir_without_exe_is_skipped(tmp_path: Path):
    local = tmp_path / "local"
    base = _package_base(local)
    (base / "2.1.287").mkdir(parents=True)
    want = _touch_exe(base / "2.1.286" / "635c1867224a" / "claude.exe")
    (tmp_path / "roaming").mkdir()
    assert find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")}) == want


def test_481_row1c_no_exe_anywhere_raises_systemexit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    local = tmp_path / "local"
    (_package_base(local) / "2.1.286" / "635c1867224a").mkdir(parents=True)
    (tmp_path / "roaming").mkdir()
    with pytest.raises(SystemExit):
        find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")})


def test_481_several_hash_dirs_take_the_newest_by_mtime(tmp_path: Path):
    local = tmp_path / "local"
    base = _package_base(local)
    old = _touch_exe(base / "2.1.286" / "aaaa" / "claude.exe")
    new = _touch_exe(base / "2.1.286" / "bbbb" / "claude.exe")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    (tmp_path / "roaming").mkdir()
    assert find_claude_exe({"LOCALAPPDATA": str(local), "APPDATA": str(tmp_path / "roaming")}) == new


def _boom() -> None:
    raise FileNotFoundError(2, "x")


def test_481_row2_tick_failed_names_the_innermost_frame(tmp_path: Path):
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=[]))
    d.log = messages.append
    d.tick = _boom  # type: ignore[method-assign]
    d.run(ticks=1)
    assert len(messages) == 1 and "\n" not in messages[0]
    assert re.fullmatch(
        r"tick failed \(FileNotFoundError\(2, 'x'\) at test_agentops_dispatch\.py:\d+ in _boom\); continuing",
        messages[0]), messages[0]
