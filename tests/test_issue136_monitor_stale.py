"""#136: (a) one `<task-notification>` that lists several `<task-id>`s ends every one of them;
(b) a `<cross-session-message>` or `<task-notification>` user record never sets a chat's ticket."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.token_meter import parse_main_transcript
from tools.agentops.dispatch import ended_task_ids


def _notification(ids: list[str], status: str) -> str:
    parts = "".join(
        f"<task-notification><task-id>{i}</task-id><status>{status}</status></task-notification>"
        for i in ids)
    return parts


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(mid: str) -> dict:
    return {"type": "assistant", "timestamp": "2026-10-17T16:00:00.000Z",
            "message": {"id": mid, "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}}


def test_multi_id_notification_ends_all_ids(tmp_path):
    # the harness's shape: ONE notification, several ids, one status
    text = ("3 background agents didn't finish before the previous session ended "
            "<task-notification>\n<task-id>a1</task-id>\n<task-id>a2</task-id>\n<task-id>a3</task-id>\n"
            "<status>stopped</status></task-notification>")
    parent = _write(tmp_path / "p.jsonl", [_user(text)])
    assert ended_task_ids(parent) == {"a1", "a2", "a3"}


def test_single_id_notifications_keep_results(tmp_path):
    parent = _write(tmp_path / "p.jsonl", [
        _user(_notification(["b1"], "completed")),
        _user(_notification(["b2"], "running")),
        _user(_notification(["b3", "b4"], "killed")),
    ])
    assert ended_task_ids(parent) == {"b1", "b3", "b4"}


def test_peer_message_never_sets_issue(tmp_path):
    parent = _write(tmp_path / "p.jsonl", [
        _user('<cross-session-message from="x">#133 is now parked</cross-session-message>'),
        _assistant("m1"),
    ])
    assert parse_main_transcript(parent).turns[0].issue is None
    parent = _write(tmp_path / "q.jsonl", [
        _user('<cross-session-message from="x">#133 is now parked</cross-session-message>'),
        _user("work on #77"),
        _assistant("m2"),
    ])
    assert parse_main_transcript(parent).turns[0].issue == 77


def test_monitor_scan_ignores_peer_ticket(tmp_path):
    from tools.agentops.monitor_agents import scan_main_transcript
    parent = _write(tmp_path / "s.jsonl", [
        _user('<cross-session-message from="x">#133 is now parked</cross-session-message>'),
        _assistant("m3"),
    ])
    assert scan_main_transcript(parent).turns[0].issue is None
