"""#50: a dispatcher restart never takes a ticket from the ORCH that holds it.

The replay of 2026-10-16 14:43-14:46: session A is woken about #N, A claims #N, the dispatcher restarts and
adopts A, and the claims read finds #N with no `assigned` event listed yet (an empty events list, or a read
that failed). #N must stay with A: no `stale assignee` line, and one `events not readable, kept` line."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _issue
from tools.agentops.dispatch import IssueStatus
from tests._area_rule import _no_area_rule  # noqa: F401

N = 39


class ListingGh(FakeGh):
    """Candidates are the ready issues without an assignee, as the real `--search no:assignee`."""

    def ready_for_agent_issues(self):
        return [_issue(n) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels and not s.assignees]


def _ready(gh: FakeGh, n: int) -> None:
    gh.issues[n] = IssueStatus(number=n, open=True, labels=READY, assignees=())


def _replay(tmp_path: Path, mode: str):
    """A starts offered #1, is woken about #N, claims #N; the dispatcher restarts before any tick reads
    the claim. `mode`: the events list of #N is empty (`empty`) or its read raises (`raises`)."""
    claude, gh = FakeClaude(), ListingGh()
    msgs: list[str] = []
    clock = FakeClock(100000.0)
    d1 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d1.log = msgs.append
    _ready(gh, 1)
    d1.tick()  # A starts, offered #1
    sid = claude.list_sessions()[0]["id"]
    claude._sessions[sid]["name"] = "orch #1"  # the name a start gives: adoption reads the offer from it
    gh.claim(1)
    clock.advance(60)
    _ready(gh, N)
    d1.tick()  # A is woken about #N
    assert any(f"#{N}" in message for _, message in claude.waked)
    gh.claim(N)  # A claims #N
    if mode == "empty":
        gh.assigned_at[N] = None
    else:
        def boom(number: int):
            if number == N:
                raise RuntimeError("gh down")
            return datetime.now(timezone.utc) + timedelta(days=1)

        gh.last_assigned_at = boom
    d2 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)  # the restart
    d2.log = msgs.append
    d2.tick()  # adopts A
    d2.tick()
    return claude, gh, d2, msgs, sid


@pytest.mark.parametrize("mode", ["empty", "raises"])
def test_row1_replay_wake_claim_restart_keeps_the_ticket_with_its_orch(tmp_path: Path, mode: str):
    claude, gh, d2, msgs, sid = _replay(tmp_path, mode)
    assert [m for m in msgs if f"adopted live" in m and sid in m]
    assert gh.removed_assignees == []
    assert gh.issues[N].assignees  # still assigned: the ticket stays with A
    assert not [m for m in msgs if "stale assignee" in m]
    assert len([m for m in msgs if f"#{N}: events not readable, kept" in m]) <= 1


# -- unit 2: wakes and offers persist, a departed session's pid decides ------------------------------

PID = 4242
OLD = timedelta(hours=1)


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8"))


def _wake_then_restart(tmp_path: Path, *, strip_claims: bool = False, a_listed: bool = True):
    """The replay of 14:43-14:46 with A's `claude.exe` pid on record: A starts offered #1, is woken about
    #N, claims #N; #N's events read old and readable. The dispatcher restarts (`d2`, `pid_alive` as set
    by the test). `strip_claims` leaves the state file's entry for A without `held` / `preexisting`
    (an entry that does not tell what A claims); `a_listed` False removes A from the live listing."""
    claude, gh = FakeClaude(), ListingGh()
    msgs: list[str] = []
    clock = FakeClock(100000.0)
    d1 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d1.log = msgs.append
    _ready(gh, 1)
    d1.tick()
    sid = claude.list_sessions()[0]["id"]
    claude._sessions[sid].update(name="orch #1", pid=PID)
    gh.claim(1)
    d1.tick()  # the poll records A's pid
    clock.advance(60)
    _ready(gh, N)
    d1.tick()  # A is woken about #N
    assert any(f"#{N}" in message for _, message in claude.waked)
    gh.claim(N)
    now = datetime.now(timezone.utc)
    gh.assigned_at[N] = now - OLD
    gh.comment_at[N] = now - OLD
    if strip_claims:
        data = _state(tmp_path)
        for entry in data["sessions"]:
            entry.pop("held", None)
            entry.pop("preexisting", None)
        (tmp_path / "orch_session.json").write_text(json.dumps(data), encoding="utf-8")
    if not a_listed:
        claude._sessions.pop(sid)
    d2 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d2.log = msgs.append
    return claude, gh, d2, msgs, sid


def test_row1_variant_state_entry_without_claims_keeps_the_offered_ticket(tmp_path: Path):
    claude, gh, d2, msgs, sid = _wake_then_restart(tmp_path, strip_claims=True)
    d2.tick()  # adopts A: its offered #N comes back from the state file, not from the name
    d2.tick()
    assert [m for m in msgs if "adopted live" in m and sid in m]
    assert N in d2.sessions[0].offered
    assert gh.removed_assignees == []
    assert not [m for m in msgs if "stale assignee" in m]


def test_row2_pid_dead_frees_the_ticket_with_one_line_naming_it_and_the_session(tmp_path: Path):
    claude, gh, d2, msgs, sid = _wake_then_restart(tmp_path, a_listed=False)
    d2.pid_alive = lambda pid: False
    d2.tick()
    d2.tick()
    assert N in gh.removed_assignees
    lines = [m for m in msgs if "stale assignee" in m and f"#{N} " in m]
    assert len(lines) == 1 and sid in lines[0]


def test_row2_pid_alive_of_a_departed_session_keeps_its_ticket(tmp_path: Path):
    claude, gh, d2, msgs, sid = _wake_then_restart(tmp_path, a_listed=False)
    d2.pid_alive = lambda pid: pid == PID
    d2.tick()
    d2.tick()
    assert N not in gh.removed_assignees
    assert not [m for m in msgs if "stale assignee" in m and f"#{N} " in m]


def test_row3_wake_is_written_at_the_wake_and_two_sessions_read_back_identical(tmp_path: Path):
    claude, gh = FakeClaude(), ListingGh()
    clock = FakeClock(100000.0)
    d1 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    _ready(gh, 1)
    d1.tick()  # A starts, offered #1
    a = d1.sessions[0]
    claude._sessions[a.session].update(name=a.name, pid=PID)
    gh.claim(1)
    d1.tick()
    a.finishing = True  # every live ORCH is finishing: a further one starts
    _ready(gh, 2)
    d1.tick()
    assert len(d1.sessions) == 2
    b = d1.sessions[1]
    claude._sessions[b.session].update(name=b.name, pid=PID + 1)
    gh.claim(2)
    d1.tick()
    clock.advance(60)
    _ready(gh, 3)

    def crash() -> None:
        raise RuntimeError("the dispatcher dies right after the wake")

    d1._track_ready_waits = lambda report: crash()
    with pytest.raises(RuntimeError):
        d1.tick()  # B is woken about #3, then the tick dies before its end-of-tick write
    assert any("#3" in message for _, message in claude.waked)
    written = {e["session"]: e["offered"] for e in _state(tmp_path)["sessions"]}
    assert written == {a.session: [1], b.session: [2, 3]}
    assert {e["session"]: e["pid"] for e in _state(tmp_path)["sessions"]} == {a.session: PID, b.session: PID + 1}
    d2 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d2.tick()
    assert {c.session: list(c.offered) for c in d2.sessions} == written


def test_row2_holder_with_no_recorded_pid_is_not_known_gone_and_keeps_the_ticket(tmp_path: Path):
    """A start offer is written before the first poll learns the pid, and an older state file has none:
    a holder whose pid is unknown is not judged gone."""
    claude, gh, d2, msgs, sid = _wake_then_restart(tmp_path, a_listed=False)
    data = _state(tmp_path)
    data["holders"] = {str(n): {s: None for s in who} for n, who in data.get("holders", {}).items()}
    for entry in data["sessions"]:
        entry.pop("pid", None)
    (tmp_path / "orch_session.json").write_text(json.dumps(data), encoding="utf-8")
    d3 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    d3.log = msgs.append
    d3.pid_alive = lambda pid: False
    d3.tick()
    d3.tick()
    assert N not in gh.removed_assignees
    assert not [m for m in msgs if "stale assignee" in m and f"#{N} " in m]
