"""#128: `live_things` skips a launch only when its record names another session than the transcript's own.

A forked chat's transcript starts with the parent's records copied in, and those records carry the
parent's ``sessionId``. A launch whose record names the parent is not this session's. A launch whose
record carries no ``sessionId`` is not a copied record: it counts as before. When the own session
cannot be found at all, every launch counts as before."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.hooks.transcript import live_things

PARENT = "59c0ab7d-09ab-40b1-b252-99822f72a07f"
OWN = "b4c071c2-c07e-4e08-aadb-12cf36ef7956"
NONE = {"subagent": 0, "shell": 0, "monitor": 0}


def _launch(sid: str | None, agent_id: str) -> dict:
    text = (f"Async agent launched successfully. agentId: {agent_id} "
            f"(internal ID {agent_id}). The agent is working in the background.")
    record = {"type": "user", "sessionId": sid, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_" + agent_id,
         "content": [{"type": "text", "text": text}]}]}}
    if sid is None:
        del record["sessionId"]
    return record


def _own(sid: str, text: str = "a turn of the chat") -> dict:
    return {"type": "user", "sessionId": sid, "message": {"role": "user", "content": text}}


def _notification(sid: str, task_id: str, status: str = "completed") -> dict:
    body = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>{status}</status>\n"
            f"<summary>done</summary>\n</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "sessionId": sid, "content": body}


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_launches_copied_from_the_parent_do_not_count_in_a_fork(tmp_path: Path):
    transcript = _write(tmp_path / f"{OWN}.jsonl", [
        _launch(PARENT, "a92546000dbe92dd1"),
        _launch(PARENT, "a92546000dbe92dd2"),
        _own(OWN, "btw: hey"),
        _own(OWN, "a reply"),
    ])

    assert live_things(str(transcript)) == NONE


def test_a_fresh_fork_of_only_copied_parent_records_counts_zero(tmp_path: Path):
    # The file name's id (OWN) is not the parent's (PARENT) and no record of OWN exists yet:
    # the launches name PARENT, which is not the file name's id, so both are skipped.
    transcript = _write(tmp_path / f"{OWN}.jsonl", [
        _own(PARENT, "start a worker"),
        _launch(PARENT, "a92546000dbe92dd1"),
        _launch(PARENT, "a92546000dbe92dd2"),
    ])

    assert live_things(str(transcript)) == NONE


def test_a_launch_of_the_own_session_without_end_counts(tmp_path: Path):
    transcript = _write(tmp_path / f"{OWN}.jsonl", [
        _launch(PARENT, "a92546000dbe92dd1"),
        _launch(PARENT, "a92546000dbe92dd2"),
        _own(OWN, "btw: hey"),
        _launch(OWN, "a92546000dbe92dd3"),
        _own(OWN, "a reply"),
    ])

    assert live_things(str(transcript)) == {"subagent": 1, "shell": 0, "monitor": 0}


def test_an_ordinary_launch_with_its_completed_notification_counts_zero(tmp_path: Path):
    transcript = _write(tmp_path / f"{OWN}.jsonl", [
        _own(OWN, "start a worker"),
        _launch(OWN, "a92546000dbe92dd4"),
        _own(OWN, "waiting"),
        _notification(OWN, "a92546000dbe92dd4", "completed"),
    ])

    assert live_things(str(transcript)) == NONE


def test_a_name_that_is_not_a_session_id_takes_the_newest_session_id(tmp_path: Path):
    transcript = _write(tmp_path / "fixture.jsonl", [
        _launch(PARENT, "a92546000dbe92dd1"),
        _own(OWN, "btw: hey"),
        _launch(OWN, "a92546000dbe92dd3"),
    ])

    assert live_things(str(transcript)) == {"subagent": 1, "shell": 0, "monitor": 0}


@pytest.mark.parametrize("name", [f"{OWN}.jsonl", "fixture.jsonl"])
def test_a_transcript_with_no_session_id_at_all_counts_its_launch(tmp_path: Path, name: str):
    transcript = _write(tmp_path / name, [
        _launch(None, "a92546000dbe92dd5"),
        _launch(None, "a92546000dbe92dd6"),
    ])

    assert live_things(str(transcript)) == {"subagent": 2, "shell": 0, "monitor": 0}


def test_a_launch_without_session_id_counts_among_records_that_carry_ids(tmp_path: Path):
    transcript = _write(tmp_path / f"{OWN}.jsonl", [
        _launch(PARENT, "a92546000dbe92dd1"),
        _own(OWN, "a turn of the chat"),
        _launch(None, "a92546000dbe92dd7"),
    ])

    assert live_things(str(transcript)) == {"subagent": 1, "shell": 0, "monitor": 0}
