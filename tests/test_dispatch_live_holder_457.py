"""#457 unit 1: the dispatcher never offers an issue (start or wake) that another live ORCH session works,
whatever path took the assignee off it; the assignee is put back instead. Fake gh, fake claude."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _events, _issue
from tools.agentops.dispatch import (Dispatcher, IssueStatus, checkpoint_holder_of, finishing_note,
                                   mark_finishing)
from tests._area_rule import _no_area_rule  # noqa: F401

A = "aaaaaaaa"
B = "bbbbbbbb"
C = "cccccccc"
N = 414
WORKING = f"**Checkpoint 4 (ORCH {B}, #{N}).** unit 2 merged into the issue branch; unit 3 running."
PARKING = f"<!-- parked-on-run: waits for the merge of orch-{N} -->\nORCH {B} parks #{N}."
PARKING_A = f"<!-- parked-on-run: waits for the run -->\nORCH {A} parks #{N}."
RESTORED = f"#{N} worked by live {B}: not offered, assignee restored"


class HolderGh(FakeGh):
    """Candidates are the ready issues without an assignee (the real `no:assignee` pool); one comment thread
    per issue; `add_assignee` as `gh issue edit --add-assignee @me`; the comments read can be made to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: dict[int, list[dict]] = {}
        self.added_assignees: list[int] = []
        self.comments_fail = False
        self.add_assignee_ok = True

    def ready_for_agent_issues(self):
        return [_issue(n) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels and not s.assignees]

    def issue_comments(self, number):
        if self.comments_fail:
            return None
        return list(self.thread.get(number, []))

    def checkpoint_holder(self, number, session_ids=()):  # #918: the real rule over the thread
        return checkpoint_holder_of(self.thread.get(number, []), tuple(session_ids))

    def add_assignee(self, number, login="@me"):
        self.added_assignees.append(number)
        if not self.add_assignee_ok:
            return False
        self.claim(number)
        return True

    def outside_unassign(self, number):  # `gh issue edit --remove-assignee` by a person or a script
        s = self.issues[number]
        self.issues[number] = IssueStatus(number=number, open=True, labels=s.labels, assignees=())


def _world(tmp_path: Path, sessions: list[dict], *, max_hours: float = 4.0):
    """`sessions`: dicts with `id`, `finishing`, `held`; every one is live in the fake claude."""
    claude, gh = FakeClaude(), HolderGh()
    gh.issues[N] = IssueStatus(number=N, open=True, labels=READY, assignees=())
    rows = []
    for s in sessions:
        claude.add_session(s["id"], name=f"orch {s['id']}", state="working", status="busy",
                           startedAt=int(time.time() * 1000) - 600_000)
        rows.append({"session": s["id"], "offered": s.get("offered", s["held"]), "held": s["held"],
                     "preexisting": [], "claimed_now": None, "finishing": s["finishing"],
                     "started_at_wall": time.time() - s.get("age", 600)})
    state = {"updated": "x", "session": None, "sessions": rows}
    (tmp_path / "orch_session.json").write_text(json.dumps(state), encoding="utf-8")
    for s in sessions:
        if s["finishing"]:
            mark_finishing(s["id"], finishing_dir=tmp_path / "finishing")
    d = Dispatcher(claude=claude, gh=gh, max_hours=max_hours, events_file=tmp_path / "events.jsonl",
                   orch_offer_max=99, state_file=tmp_path / "orch_session.json",
                   projects_dir=tmp_path / "projects", finishing_dir=tmp_path / "finishing",
                   clock=FakeClock(100000.0))
    msgs: list[str] = []
    d.log = msgs.append
    return claude, gh, d, msgs


def _ticks(d, k: int = 3) -> None:
    for _ in range(k):
        d.tick()
        d.clock.advance(60)


def _woke_about(tmp_path: Path, n: int) -> int:
    """Offer wakes (`orch_wake` events) that name `n`; other messages to a session are not offers."""
    return sum(1 for e in _events(tmp_path) if e.get("kind") == "orch_wake" and n in e.get("issues", []))


def test_start_path_finishing_holder_with_working_checkpoint_is_not_offered(tmp_path: Path):
    """Row 2: the start path."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": WORKING}]
    _ticks(d)
    assert claude.started == []
    assert gh.issues[N].assignees  # n is assigned again
    assert gh.added_assignees == [N]
    assert msgs.count(RESTORED) == 1
    restored = [e for e in _events(tmp_path) if e.get("kind") == "assignee_restored"]
    assert len(restored) == 1 and restored[0]["issue"] == N and restored[0]["session"] == B
    cur = d.sessions[0]
    assert N in cur.held and N not in cur.let_go


def test_timeout_branch_unassign_while_live_holder_works_it(tmp_path: Path):
    """Row 3: the `--max-hours` branch unassigns n while live B works it."""
    claude, gh, d, msgs = _world(tmp_path, [
        {"id": A, "finishing": False, "held": [N], "age": 5 * 3600},  # past --max-hours 4
        {"id": B, "finishing": True, "held": []},
    ])
    gh.claim(N)
    gh.thread[N] = [{"body": WORKING}]
    d.tick()  # adopts both
    d.tick()  # A times out: its end unassigns n
    assert gh.removed_assignees == [N]
    assert not gh.issues[N].assignees
    _ticks(d)
    assert claude.started == []
    assert gh.issues[N].assignees
    assert msgs.count(RESTORED) == 1


def test_outside_removal_start_path_restored_once_logged_once(tmp_path: Path):
    """Row 4, start path: a removal from outside."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.claim(N)
    gh.thread[N] = [{"body": WORKING}]
    d.tick()
    assert gh.added_assignees == []
    gh.outside_unassign(N)
    _ticks(d, 4)
    assert claude.started == []
    assert gh.added_assignees == [N]
    assert msgs.count(RESTORED) == 1


def test_outside_removal_wake_path_not_woken_for_the_other_sessions_issue(tmp_path: Path):
    """Row 4, wake path: a removal from outside."""
    claude, gh, d, msgs = _world(tmp_path, [
        {"id": B, "finishing": True, "held": [N]},
        {"id": C, "finishing": False, "held": []},
    ])
    gh.claim(N)
    gh.thread[N] = [{"body": WORKING}]
    d.tick()
    gh.outside_unassign(N)
    _ticks(d, 4)
    assert _woke_about(tmp_path, N) == 0
    assert claude.started == []
    assert gh.added_assignees == [N]
    assert msgs.count(RESTORED) == 1


def test_parked_and_ended_no_live_holder_is_offered_as_today(tmp_path: Path):
    """Row 5: continuation."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": []}])
    gh.thread[N] = [{"body": PARKING_A}]  # A parked n and ended: no live session works it
    _ticks(d)
    assert claude.started and N in claude.started[0][0]
    assert gh.added_assignees == []
    assert [e for e in _events(tmp_path) if e.get("kind") == "assignee_restored"] == []


def test_a_live_session_that_parked_it_itself_does_not_hold_it(tmp_path: Path):
    """Row 5: the parking session is live but its newest checkpoint parks n."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": WORKING}, {"body": PARKING}]  # B's newest checkpoint parks n
    _ticks(d)
    assert claude.started and N in claude.started[0][0]
    assert gh.added_assignees == []


def test_start_path_comments_read_fails_not_offered_then_restored_when_it_works(tmp_path: Path):
    """Row 6: read failure, start path."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": []}])
    gh.thread[N] = [{"body": WORKING}]
    gh.comments_fail = True
    _ticks(d, 3)
    assert claude.started == []
    assert gh.added_assignees == []
    assert len([m for m in msgs if f"#{N}" in m and "comments" in m and "read" in m]) == 1  # once an hour
    gh.comments_fail = False
    _ticks(d, 2)
    assert claude.started == []
    assert gh.added_assignees == [N]
    assert msgs.count(RESTORED) == 1


def test_a_parking_checkpoint_by_another_session_that_mentions_this_one_does_not_cancel_its_stake(tmp_path: Path):
    """A's parking checkpoint names `ORCH B` in passing: the poster is A (the first `ORCH <id>` of the
    comment), so B's stake (offered n, never claimed, not finishing) stands and C is not woken about n."""
    claude, gh, d, msgs = _world(tmp_path, [
        {"id": B, "finishing": False, "held": [], "offered": [N]},
        {"id": C, "finishing": False, "held": []},
    ])
    gh.thread[N] = [{"body": WORKING}, {"body": PARKING_A + f" ORCH {B}'s units 2 and 3 go on."}]
    _ticks(d, 3)
    assert [e for e in _events(tmp_path) if e.get("kind") == "orch_wake" and N in e["issues"]
            and e["session"] == C] == []


def test_wake_path_comments_read_fails_not_woken_then_restored_when_it_works(tmp_path: Path):
    """Row 6, wake path: the read fails, so n is not woken about this tick (one log line within the hour);
    next tick, with the read working, B's working checkpoint puts the assignee back and n is still not woken."""
    claude, gh, d, msgs = _world(tmp_path, [
        {"id": B, "finishing": True, "held": []},
        {"id": C, "finishing": False, "held": []},
    ])
    gh.thread[N] = [{"body": WORKING}]
    gh.comments_fail = True
    _ticks(d, 3)
    assert _woke_about(tmp_path, N) == 0
    assert gh.added_assignees == []
    assert len([m for m in msgs if f"#{N}" in m and "comments" in m and "read" in m]) == 1
    gh.comments_fail = False
    _ticks(d, 2)
    assert _woke_about(tmp_path, N) == 0
    assert gh.added_assignees == [N]
    assert msgs.count(RESTORED) == 1


def test_the_finishing_note_is_a_working_checkpoint(tmp_path: Path):
    """The note `ORCH <id8>, finishing: ...` carries a parked-on-run marker on its second line, yet it is
    the holder's working checkpoint: n is restored, not offered."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": finishing_note(B, (B,))}]
    _ticks(d)
    assert claude.started == []
    assert gh.added_assignees == [N]


def test_a_failed_restore_leaves_the_issue_out_of_the_offer_and_logs_once(tmp_path: Path):
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": WORKING}]
    gh.add_assignee_ok = False
    _ticks(d, 4)
    assert claude.started == []
    assert len(gh.added_assignees) == 4  # tried every tick
    assert len([m for m in msgs if f"#{N}" in m and "restore failed" in m]) == 1
    assert [e for e in _events(tmp_path) if e.get("kind") == "assignee_restored"] == []


def test_a_restored_assignee_does_not_hide_the_holders_own_parking_checkpoint_at_its_end(tmp_path: Path):
    """The ticket's `_ended_outcome` interaction: after `assignee_restored` n shows an assignee again; B then
    parks n with its own parking checkpoint and ends: its end still reads that checkpoint as its own park (released, unassigned)."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": WORKING}]
    _ticks(d, 2)
    assert gh.added_assignees == [N] and gh.issues[N].assignees
    gh.thread[N].append({"body": PARKING})
    gh.comment_body[N] = PARKING  # the fake's `last_comment_names` / `last_comment_body` read this
    claude._sessions.pop(B)  # B ends
    _ticks(d, 2)
    ends = [e for e in _events(tmp_path) if e.get("kind") == "orch_end" and e.get("issue") == N]
    assert [e["outcome"] for e in ends] == ["released"]  # its own parking checkpoint, not `no_checkpoint`
    assert not gh.issues[N].assignees
    assert gh.comments == []  # no "ended without a closing or parking checkpoint" comment


def test_free_stale_assignees_keeps_the_restored_issue_while_the_holder_lives(tmp_path: Path):
    """The ticket's `_free_stale_assignees` interaction: a restored assignee (assigned long ago by the
    clock of the stale check, newest comment old) is not stripped again while B lives."""
    claude, gh, d, msgs = _world(tmp_path, [{"id": B, "finishing": True, "held": [N]}])
    gh.thread[N] = [{"body": WORKING}]
    _ticks(d, 2)
    assert gh.added_assignees == [N]
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    gh.assigned_at[N] = old
    gh.comment_at[N] = old
    _ticks(d, 3)
    assert gh.removed_assignees == []
    assert gh.issues[N].assignees
