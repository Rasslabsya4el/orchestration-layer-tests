"""#1110: a subagent started by another subagent (its `<agent>.meta.json` names a `parentAgentId`, or a
`spawnDepth` >= 2) reports to that parent subagent, never to the session, so it never keeps an ended
session running. Rows 1-3 build tiny fixtures; row 4 replays the 15.10 case (ORCH b4d07023) with the two
meta files, the depth-1 sibling's notification and empty transcripts only."""

from __future__ import annotations

import json
from pathlib import Path

from tools.agentops.dispatch import running_subagent_ids

from tests.test_agentops_dispatch import _issue, _notify, _start, _subagent
from tests._area_rule import _no_area_rule  # noqa: F401

DEPTH1 = "a4e5196de8f08a5e0"  # the worker of #1093 (spawnDepth 1), notified in the parent transcript
DEPTH2 = "ab8fc32f859b74cee"  # its code-review helper (spawnDepth 2), never notified in the parent
META_DEPTH1 = {"agentType": "worker", "description": "#1093 full run row",
               "toolUseId": "toolu_01L5RXFsycTs24LzGtB3ApcV", "spawnDepth": 1,
               "requestShape": "background", "requestNonInteractive": True}
META_DEPTH2 = {"agentType": "general-purpose", "parentAgentId": DEPTH1, "spawnDepth": 2,
               "requestShape": "foreground", "requestNonInteractive": True}
REAL_UUID = "b4d07023-2c80-45b4-a3a2-16bfb29ca848"


def _meta(projects: Path, session_uuid: str, agent_id: str, meta: dict) -> None:
    sub = projects / session_uuid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


# -- row 1: a depth-2 helper of an ended depth-1 subagent does not count -------------------------

def test_row1_a_depth2_helper_without_notification_does_not_count(tmp_path: Path):
    projects = tmp_path / "projects"
    _subagent(projects, "u", DEPTH1)
    _notify(projects, "u", DEPTH1)  # the depth-1 subagent ended: terminal notification in the parent
    _subagent(projects, "u", DEPTH2)
    _meta(projects, "u", DEPTH1, META_DEPTH1)
    _meta(projects, "u", DEPTH2, META_DEPTH2)

    assert running_subagent_ids(projects, "u") == set()


# -- row 2: a depth-1 subagent with no notification is still running -----------------------------

def test_row2_a_depth1_subagent_without_notification_is_still_running(tmp_path: Path):
    projects = tmp_path / "projects"
    _subagent(projects, "u", DEPTH1)
    _meta(projects, "u", DEPTH1, META_DEPTH1)

    assert running_subagent_ids(projects, "u") == {DEPTH1}


# -- row 3: `_poll_session` finishes a `done` session whose only running helper is a depth-2 one ---

def test_row3_a_done_session_with_only_a_depth2_helper_finishes(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(1)])
    projects = tmp_path / "projects"
    uuid = f"uuid-{live_id}"
    gh.claim(1)
    _subagent(projects, uuid, DEPTH1)
    _notify(projects, uuid, DEPTH1)
    _subagent(projects, uuid, DEPTH2)
    _meta(projects, uuid, DEPTH1, META_DEPTH1)
    _meta(projects, uuid, DEPTH2, META_DEPTH2)
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.ended == live_id and claude.stopped == [live_id]
    assert live_id in d.ended_sessions
    assert gh.removed_assignees == [1]


# -- row 4: replay of the 15.10 case (ORCH b4d07023, #1096) -------------------------------------

#: the real record of parent line 304 (`queue-operation`, enqueue, 2026-10-15T16:47:03.089Z) for the
#: depth-1 worker a4e5196de8f08a5e0, cut to its fields; the result text is shortened.
REAL_NOTIFICATION = (
    "<task-notification>\n<task-id>a4e5196de8f08a5e0</task-id>\n"
    "<tool-use-id>toolu_01L5RXFsycTs24LzGtB3ApcV</tool-use-id>\n"
    "<output-file>C:\\Users\\user\\AppData\\Local\\Temp\\claude\\C--Work-target-repo\\"
    "b4d07023-2c80-45b4-a3a2-16bfb29ca848\\tasks\\a4e5196de8f08a5e0.output</output-file>\n"
    "<status>completed</status>\n<summary>Agent \"#1093 full run row\" finished</summary>\n"
    "<note>A task-notification fires each time this agent stops with no live background children of its "
    "own. The user can send it another message and resume it, so the same task-id may notify more than "
    "once.</note>\n<result>done: no full run is running now.</result>\n"
    "<usage><subagent_tokens>117861</subagent_tokens><tool_uses>42</tool_uses>"
    "<duration_ms>292704</duration_ms></usage>\n</task-notification>")


def test_row4_the_real_15_10_case_replays_as_ended(tmp_path: Path):
    projects = tmp_path / "projects"
    record = {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-15T16:47:03.089Z",
              "sessionId": REAL_UUID, "content": REAL_NOTIFICATION}
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"{REAL_UUID}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    _subagent(projects, REAL_UUID, DEPTH1)
    _subagent(projects, REAL_UUID, DEPTH2)
    _meta(projects, REAL_UUID, DEPTH1, META_DEPTH1)
    _meta(projects, REAL_UUID, DEPTH2, META_DEPTH2)

    assert running_subagent_ids(projects, REAL_UUID) == set()
