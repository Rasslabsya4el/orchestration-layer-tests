"""#906: a finishing session with a live watch (a Monitor it armed) is kept; ended as before without one."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_agentops_dispatch_idle_end import NOW, _setup, _transcript_at
from tests._area_rule import _no_area_rule  # noqa: F401


def _append(projects: Path, uuid: str, rec: dict) -> None:
    with (projects / f"{uuid}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _start_watch(projects: Path, uuid: str, task: str = "b01h5m13n") -> None:
    """The shape Claude Code writes: the Monitor tool result carries `Monitor started (task <id>, ...`."""
    _append(projects, uuid, {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "content": f"Monitor started (task {task}, expires in 30m unless the source ends first)"}]},
        "toolUseResult": {"taskId": task}})


def _stop_watch(projects: Path, uuid: str, task: str = "b01h5m13n") -> None:
    _append(projects, uuid, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "TaskStop", "input": {"task_id": task}}]}})


def test_row1_finishing_with_one_live_watch_is_kept(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=11)
    _start_watch(tmp_path / "projects", claude.list_sessions()[0]["sessionId"])

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []


def test_row2_finishing_with_no_watch_is_ended_as_before(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=11)

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]


def test_cap_a_forgotten_watch_stops_counting_after_the_cap(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=61)
    _start_watch(tmp_path / "projects", claude.list_sessions()[0]["sessionId"])

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]


def test_watch_ended_by_task_stop_or_notification_no_longer_holds(tmp_path: Path):
    for ender in ("stop", "notification"):
        sub = tmp_path / ender
        claude, gh, d, sid = _setup(sub, finishing=True, age_minutes=11)
        uuid = claude.list_sessions()[0]["sessionId"]
        _start_watch(sub / "projects", uuid)
        if ender == "stop":
            _stop_watch(sub / "projects", uuid)
        else:
            _append(sub / "projects", uuid, {"type": "queue-operation", "content": (
                "<task-notification>\n<task-id>b01h5m13n</task-id>\n<status>completed</status>\n</task-notification>")})

        d.tick()

        assert sid in d.ended_sessions and claude.stopped == [sid], ender
