"""#143: a subagent whose first prompt names several #N is charged to a held issue first, else to the issue
its parent session is attributed to at that moment when it is among them, else to the first #N named;
a subagent of a brain-titled chat with no issue gets the ``brain`` key, and one of a non-brain chat stays
UNATTRIBUTED.

Transcripts are built in tmp_path, in the repo's jsonl shape; the scan is `scan_projects_dir`, and the
direct call is `parse_subagent_transcript` with no parent.

    python -m pytest -q -p no:faulthandler tests/test_issue143_helper_attribution.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import token_meter as tm  # noqa: E402

BRAIN_TITLE = tm.BRAIN_TITLE_LITERAL  # the brain chat's title, as token_meter reads config/orchestration.json


def _user(ts: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _assistant(ts: str, mid: str) -> dict:
    message = {"id": mid, "model": "claude-sonnet-5-5",
               "usage": {"input_tokens": 10, "output_tokens": 1}, "content": []}
    return {"type": "assistant", "timestamp": ts, "message": message}


def _write(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def _subagent_file(tmp_path: Path, prompt: str) -> Path:
    """`parent/subagents/agent-x.jsonl` under tmp_path/projects, its first prompt written at 10:05."""
    path = tmp_path / "projects" / "parent" / "subagents" / "agent-x.jsonl"
    _write(path, [
        _user("2026-10-17T10:05:00Z", prompt),
        _assistant("2026-10-17T10:05:10Z", "sub-1"),
        _assistant("2026-10-17T10:05:20Z", "sub-2"),
    ])
    path.with_name("agent-x.meta.json").write_text(json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
    return path


def _scan(tmp_path: Path, parent_objs: list[dict], sub_prompt: str,
          *, orch_session_held: dict[str, list[int]] | None = None) -> list[tm.Turn]:
    """One main session `parent` with `parent_objs`, and one subagent of it whose first prompt is
    `sub_prompt` (written at 10:05). Returns the subagent's turns."""
    projects = tmp_path / "projects"
    _write(projects / "parent.jsonl", parent_objs)
    _subagent_file(tmp_path, sub_prompt)
    result = tm.scan_projects_dir(projects, orch_session_held=orch_session_held)
    turns = [t for t in result.turns if t.is_subagent]
    assert turns, "the subagent's turns were not read"
    return turns


def test_several_numbers_parent_unattributed_takes_first_named(tmp_path):
    turns = _scan(tmp_path, [
        _user("2026-10-17T10:00:00Z", "Plan the rollout"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),  # parent turn with no issue
    ], "Issue #306 (under #300)")
    assert {t.issue for t in turns} == {306}


def test_several_numbers_parent_attributed_takes_parent_issue(tmp_path):
    turns = _scan(tmp_path, [
        _user("2026-10-17T10:00:00Z", "Issue #300"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),  # parent attributed to #300 at 10:00:30
    ], "Issue #306 (under #300)")
    assert {t.issue for t in turns} == {300}


def test_held_issue_beats_parent_issue(tmp_path):
    turns = _scan(tmp_path, [
        _user("2026-10-17T10:00:00Z", "Issue #300"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),  # parent attributed to #300
    ], "Issue #306 (under #300, #303)", orch_session_held={"parent": [303]})
    assert {t.issue for t in turns} == {303}


def test_parent_issue_not_named_takes_first_named(tmp_path):
    turns = _scan(tmp_path, [
        _user("2026-10-17T10:00:00Z", "Issue #300"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),  # parent attributed to #300, not named below
    ], "Issue #306 (under #303)")
    assert {t.issue for t in turns} == {306}


def test_parent_moving_after_the_prompt_keeps_the_earlier_issue(tmp_path):
    turns = _scan(tmp_path, [
        _user("2026-10-17T10:00:00Z", "Issue #300"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),  # #300 when the subagent starts (10:05)
        _user("2026-10-17T10:10:00Z", "Issue #303"),
        _assistant("2026-10-17T10:10:30Z", "p-2"),  # moves to #303 after it
    ], "Issue #306 (under #300, #303)")
    assert {t.issue for t in turns} == {300}


def test_brain_chat_subagent_naming_one_number_keeps_that_issue(tmp_path):
    turns = _scan(tmp_path, [
        {"type": "custom-title", "customTitle": BRAIN_TITLE},
        _user("2026-10-17T10:00:00Z", "Plan the rollout"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),
    ], "Issue #306")
    assert {t.issue for t in turns} == {306}
    assert tm.issue_group_weights(turns[0]) == {"#306": 1.0}


def test_brain_chat_subagent_without_number_gets_brain_key(tmp_path):
    turns = _scan(tmp_path, [
        {"type": "custom-title", "customTitle": BRAIN_TITLE},
        _user("2026-10-17T10:00:00Z", "Plan the rollout"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),
    ], "Read the Zorblax losses and report back")
    assert all(t.issue is None for t in turns)
    assert tm.issue_group_weights(turns[0]) == {"brain": 1.0}


def test_non_brain_chat_subagent_without_number_stays_unattributed(tmp_path):
    turns = _scan(tmp_path, [
        {"type": "custom-title", "customTitle": "Owner audit chat"},
        _user("2026-10-17T10:00:00Z", "Plan the rollout"),
        _assistant("2026-10-17T10:00:30Z", "p-1"),
    ], "Read the Zorblax losses and report back")
    assert all(t.issue is None for t in turns)
    assert tm.issue_group_weights(turns[0]) == {"UNATTRIBUTED": 1.0}


def test_direct_call_without_parent_takes_first_named(tmp_path):
    path = _subagent_file(tmp_path, "Issue #306 (under #300)")
    turns = tm.parse_subagent_transcript(path, {})
    assert {t.issue for t in turns} == {306}


def test_direct_call_without_parent_and_without_number_is_none(tmp_path):
    path = _subagent_file(tmp_path, "Read the Zorblax losses and report back")
    turns = tm.parse_subagent_transcript(path, {})
    assert turns and all(t.issue is None for t in turns)
