"""#727 unit 2: a finishing session whose last turn is more than 10 min old and which has no running
subagent is ended by the dispatcher, though the listing still says working/busy. Rows 2, 3a, 3b, extra.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue, _events
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def _transcript_at(projects: Path, session_uuid: str, age_minutes: float) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(minutes=age_minutes)).isoformat().replace("+00:00", "Z")
    line = {"type": "assistant", "timestamp": stamp, "message": {"id": "m1", "usage": {"input_tokens": 10}}}
    (projects / f"{session_uuid}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")


def _setup(tmp_path: Path, *, finishing: bool, age_minutes: float, subagents: int = 0):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, timestamp=FakeTimestamp(NOW))
    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    d.tick()  # S1 holds #1, state working / busy
    projects = tmp_path / "projects"
    _transcript_at(projects, s1["sessionId"], age_minutes)
    for n in range(subagents):
        sub = projects / s1["sessionId"] / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"agent-a{n}.jsonl").write_text("{}\n", encoding="utf-8")
    if finishing:
        mark_finishing(s1["id"], finishing_dir=tmp_path / "finishing")
    gh._candidates = []
    return claude, gh, d, s1["id"]


def test_row2_finishing_busy_last_turn_11_min_old_is_ended(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=11)

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]
    events = _events(tmp_path)
    assert [e for e in events if e["kind"] == "orch_session_end" and e["session"] == sid]
    assert [e for e in events if e["kind"] == "orch_end" and e["session"] == sid and e["issue"] == 1]


def test_row3a_last_turn_5_min_old_is_left_alone(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=5)

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []


def test_row3b_one_running_subagent_is_left_alone(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=11, subagents=1)

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []


def test_row_extra_not_finishing_is_left_alone(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=False, age_minutes=11)

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []
