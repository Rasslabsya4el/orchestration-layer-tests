"""#196 change 2: a finishing session is not ended by the finishing-idle clock while one of its background
Bash shells (a Bash tool call with `run_in_background: true`) is still running.

The record shapes are the real ones, read from a Claude Code 2.1.x parent transcript: the Bash `tool_use`
carries `"run_in_background": true`; its `tool_result` text starts `Command running in background with ID:
<id>.`; the shell ends with a `<task-notification>` naming `<task-id>` and `<status>completed|failed|killed
</status>` (stored in `queue-operation` and `user` lines), or a `TaskStop` call with `{"task_id": "<id>"}`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from tools.agentops.dispatch import IssueStatus, running_shell_ids

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events
from tests.test_agentops_dispatch_end_parked import _row, _state_file, _tick, ends  # noqa: F401
from tests.test_dispatch_idle_clock import STEP, Rig
from tests.test_agentops_dispatch_idle_end import NOW, _setup
from tests._area_rule import _no_area_rule  # noqa: F401

SHELL = "bwerhi8ow"


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _append(projects: Path, uuid: str, rec: dict) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    with (projects / f"{uuid}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _start_shell(projects: Path, uuid: str, minutes_ago: float, task: str = SHELL) -> None:
    _append(projects, uuid, {"type": "assistant", "timestamp": _stamp(minutes_ago), "message": {"content": [{
        "type": "tool_use", "id": "toolu_01", "name": "Bash",
        "input": {"command": "python -m pytest -q", "timeout": 3000000, "run_in_background": True}}]}})
    _append(projects, uuid, {"type": "user", "timestamp": _stamp(minutes_ago), "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "toolu_01", "is_error": False,
        "content": f"Command running in background with ID: {task}. Output is being written to: "
                   f"C:\\tmp\\tasks\\{task}.output. You will be notified when it completes."}]},
        "toolUseResult": {"backgroundTaskId": task}})


def _notify(projects: Path, uuid: str, status: str, task: str = SHELL) -> None:
    _append(projects, uuid, {"type": "queue-operation", "operation": "enqueue", "content": (
        f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>toolu_01</tool-use-id>\n"
        f"<status>{status}</status>\n<summary>Background command finished</summary>\n</task-notification>")})


def _task_stop(projects: Path, uuid: str, task: str = SHELL) -> None:
    _append(projects, uuid, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_02", "name": "TaskStop", "input": {"task_id": task}}]}})


def test_finishing_session_with_a_running_background_shell_is_not_ended(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=12)
    _start_shell(tmp_path / "projects", claude.list_sessions()[0]["sessionId"], 12)

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []


def test_the_same_session_with_the_shell_completed_is_ended(tmp_path: Path):
    for status in ("completed", "failed", "killed"):
        sub = tmp_path / status
        claude, gh, d, sid = _setup(sub, finishing=True, age_minutes=12)
        uuid = claude.list_sessions()[0]["sessionId"]
        _start_shell(sub / "projects", uuid, 12)
        _notify(sub / "projects", uuid, status)

        d.tick()

        assert sid in d.ended_sessions and claude.stopped == [sid], status
        ends = [e for e in _events(sub) if e["kind"] == "orch_end" and e["session"] == sid]
        assert ends and ends[0]["why"] == "finishing and idle 10 min", status  # the "idle-timeout" outcome


def test_a_shell_stopped_by_task_stop_is_ended(tmp_path: Path):
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=12)
    uuid = claude.list_sessions()[0]["sessionId"]
    _start_shell(tmp_path / "projects", uuid, 12)
    _task_stop(tmp_path / "projects", uuid)

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]


def test_an_old_shell_with_no_end_record_holds_only_until_the_session_is_silent_30_min(tmp_path: Path):
    # #265: the shell has no end record; it holds the session while the session wrote within 30 min, not past that
    claude, gh, d, sid = _setup(tmp_path, finishing=True, age_minutes=20)
    _start_shell(tmp_path / "projects", claude.list_sessions()[0]["sessionId"], 20)

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []

    sub = tmp_path / "silent"
    claude, gh, d, sid = _setup(sub, finishing=True, age_minutes=180)
    _start_shell(sub / "projects", claude.list_sessions()[0]["sessionId"], 180)

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]


def test_idle_listed_finishing_session_with_a_running_shell_is_not_ended(tmp_path: Path):
    rig = Rig(tmp_path, finishing=True)
    _start_shell(tmp_path / "projects", rig.uuid, 0)
    rig.go_idle()

    rig.run(15)

    assert rig.claude.stopped == [] and rig.ended_after is None


def test_idle_listed_session_with_the_shell_completed_follows_todays_idle_rules(tmp_path: Path):
    rig = Rig(tmp_path, finishing=True)
    _start_shell(tmp_path / "projects", rig.uuid, 0)
    _notify(tmp_path / "projects", rig.uuid, "completed")
    rig.go_idle()

    rig.run(9.5)
    assert rig.ended_after is None and rig.claude.stopped == []
    rig.run(1)

    assert rig.claude.stopped == [rig.sid]
    assert 600 <= rig.ended_after <= 600 + STEP


def test_a_still_listed_ended_session_with_a_running_shell_is_not_ended_by_the_975_step(tmp_path: Path, ends):
    for shell_done in (False, True):
        sub = tmp_path / str(shell_done)
        sub.mkdir()
        claude = FakeClaude()
        claude.stop_leaves_process = True
        uuid = _row(claude, "dead-1")
        _state_file(sub, [("dead-1", uuid, [1])])
        projects = sub / "projects"
        projects.mkdir()
        _start_shell(projects, uuid, 30)
        if shell_done:
            _notify(projects, uuid, "completed")
        gh = FakeGh(issues={1: IssueStatus(number=1, open=False, labels=(), assignees=())})
        before = len(ends)

        _tick(sub, claude, gh)

        assert (len(ends) - before == 1) is shell_done, shell_done


def test_running_shell_ids_reads_only_background_shell_starts(tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    uuid = "u1"
    _start_shell(projects, uuid, 5, "bone")
    _start_shell(projects, uuid, 5, "btwo")
    _notify(projects, uuid, "completed", "bone")
    _append(projects, uuid, {"type": "user", "message": {"content": [{
        "type": "tool_result", "content": "Monitor started (task bmon, expires in 30m unless the source ends first)"}]}})

    assert running_shell_ids(projects, uuid) == {"btwo"}
    assert running_shell_ids(projects, None) == set()
    assert running_shell_ids(projects, "missing") == set()
