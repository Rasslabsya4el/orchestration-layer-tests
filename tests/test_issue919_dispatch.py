"""#919: a ticket a live ORCH session parked (held, now unassigned + ready-for-agent) is offered to it
again; a watch that names its own expiry stops counting as running after that expiry + 1 minute."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _issue
from tools.agentops.dispatch import IssueStatus, checkpoint_holder_of, running_watch_ids
from tests._area_rule import _no_area_rule  # noqa: F401

B = "addbbbbb"
N = 889
CHECKPOINT = f"**Checkpoint 9 (ORCH {B} parks #{N}).**"


class HeldGh(FakeGh):
    """Candidates = every open ready issue (assigned or not); the #918 holder rule is real."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: list[dict] = []

    def ready_for_agent_issues(self):
        return [_issue(n, labels=("area:builds",)) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels]

    def checkpoint_holder(self, number, session_ids=()):
        return checkpoint_holder_of(self.thread, tuple(session_ids))


def _world(tmp_path: Path, *, assigned: bool, thread: list[dict]):
    claude, gh = FakeClaude(), HeldGh()
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    gh.issues[N] = IssueStatus(number=N, open=True, labels=READY, assignees=("bot",) if assigned else ())
    gh.assigned_at[N] = old
    gh.comment_at[N] = old
    gh.thread = thread
    claude.add_session(B, name="orch B", state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 600_000)
    state = {"updated": "x", "session": None, "sessions": [
        {"session": B, "offered": [N], "held": [N], "preexisting": [], "claimed_now": [],
         "finishing": False, "started_at_wall": time.time() - 600}]}
    (tmp_path / "orch_session.json").write_text(json.dumps(state), encoding="utf-8")
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    d.log = lambda _m: None
    return claude, d


def _woke(claude) -> int:
    return sum(1 for _, m in claude.waked if f"#{N}" in m)


def test_row1_held_unassigned_ready_with_newer_comment_is_woken_once(tmp_path: Path):
    claude, d = _world(tmp_path, assigned=False, thread=[{"body": CHECKPOINT}, {"body": "owner: look"}])
    d.tick()
    d.tick()
    assert _woke(claude) == 1


def test_row2_own_checkpoint_newest_means_no_wake(tmp_path: Path):
    claude, d = _world(tmp_path, assigned=False, thread=[{"body": CHECKPOINT}])
    for _ in range(3):
        d.tick()
        d.clock.advance(700)
    assert _woke(claude) == 0


def test_row3_still_assigned_to_the_session_means_no_wake(tmp_path: Path):
    claude, d = _world(tmp_path, assigned=True, thread=[{"body": CHECKPOINT}, {"body": "owner: look"}])
    for _ in range(3):
        d.tick()
        d.clock.advance(700)
    assert _woke(claude) == 0


def _transcript(tmp_path: Path, start: datetime) -> Path:
    rec = {"type": "user", "timestamp": start.isoformat().replace("+00:00", "Z"),
           "message": {"role": "user", "content": [{
               "type": "tool_result",
               "content": "Monitor started (task bzxg68m4e, expires in 15m unless the source ends first)"}]}}
    (tmp_path / "S.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return tmp_path


def test_row4_a_watch_counts_at_14_min_not_at_16_min(tmp_path: Path):
    t0 = datetime(2026, 10, 12, 11, 45, 34, tzinfo=timezone.utc)
    proj = _transcript(tmp_path, t0)
    assert running_watch_ids(proj, "S", now=t0 + timedelta(minutes=14)) == {"bzxg68m4e"}
    assert running_watch_ids(proj, "S", now=t0 + timedelta(minutes=16)) == set()
