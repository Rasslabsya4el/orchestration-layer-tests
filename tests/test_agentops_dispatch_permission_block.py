"""#285: the dispatcher's permission-block test reads the session transcript too. A listing row that
says `blocked` while the transcript ends with the end of a turn is a finished session, not a prompt;
the message names the refusal hook only when the events log shows no refusal in the wait."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _events, _issue
from tools.agentops.dispatch import (
    BLOCKED_STATE,
    PERMISSION_WAIT,
    build_permission_block_message,
    last_timed_record_type,
)
from tests._area_rule import _no_area_rule  # noqa: F401

T = "2026-09-28T11:14:5{}.000Z"

ASSISTANT_TOOL = {"type": "assistant", "timestamp": T.format(1),
                  "message": {"id": "msg_1", "content": [
                      {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]}}
USER_RESULT = {"type": "user", "timestamp": T.format(2),
               "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]}}
ASSISTANT_TEXT = {"type": "assistant", "timestamp": T.format(3),
                  "message": {"id": "msg_2", "content": [{"type": "text", "text": "done"}]}}
TURN_DURATION = {"type": "system", "subtype": "turn_duration", "durationMs": 17321,
                 "timestamp": T.format(4)}
UNTIMED = [{"type": "last-prompt", "lastPrompt": "x", "sessionId": "u"},
           {"type": "cost-state", "totalCostUSD": 3.39, "sessionId": "u"}]

#: the ending of the three 2026-09-28 sessions: the final text, the end of the turn, then untimed
#: bookkeeping records (measured on 4e60eb83's transcript).
TURN_END_VARIANTS = {
    "text then turn_duration then bookkeeping": [ASSISTANT_TOOL, USER_RESULT, ASSISTANT_TEXT,
                                                 TURN_DURATION, *UNTIMED],
    "final text only": [ASSISTANT_TOOL, USER_RESULT, ASSISTANT_TEXT],
    "turn_duration only": [ASSISTANT_TOOL, USER_RESULT, TURN_DURATION, *UNTIMED],
}
PENDING = [USER_RESULT, ASSISTANT_TOOL, *UNTIMED]


def _write(tmp_path: Path, live_uuid: str, records: list[dict]) -> Path:
    path = tmp_path / "projects" / f"{live_uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _running_session(tmp_path: Path):
    claude, gh, clock = FakeClaude(), FakeGh(candidates=[_issue(7)]), FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    live = claude.list_sessions()[0]["id"]
    gh.claim(7)
    clock.advance(30.0)
    d.tick()
    claude.waked.clear()
    return claude, d, clock, live


def _reports(claude: FakeClaude) -> list[tuple[str, str]]:
    return [w for w in claude.waked if PERMISSION_WAIT in w[1]]


def _end_whys(tmp_path: Path) -> list[str]:
    return [e["why"] for e in _events(tmp_path) if e["kind"] == "orch_end"]


@pytest.mark.parametrize("name", list(TURN_END_VARIANTS))
def test_row1_blocked_listing_with_a_turn_end_transcript_is_done_and_silent(tmp_path: Path, name: str):
    claude, d, clock, live = _running_session(tmp_path)
    _write(tmp_path, claude._sessions[live]["sessionId"], TURN_END_VARIANTS[name])
    claude._sessions[live].update(state=BLOCKED_STATE, status="waiting")

    clock.advance(30.0)
    d.tick()

    assert claude.waked == []
    assert [e for e in _events(tmp_path) if e["kind"] == "permission_blocked"] == []
    assert _end_whys(tmp_path) == ["done"]


def _stuck(tmp_path: Path):
    claude, d, clock, live = _running_session(tmp_path)
    uuid = claude._sessions[live]["sessionId"]
    _write(tmp_path, uuid, PENDING)
    claude._sessions[live].update(state=BLOCKED_STATE, status="waiting", waitingFor=PERMISSION_WAIT)
    return claude, d, clock, live, uuid


def test_row2_pending_tool_call_is_one_wake_and_ends_blocked(tmp_path: Path):
    claude, d, clock, live, _ = _stuck(tmp_path)

    clock.advance(30.0)
    d.tick()

    assert len(_reports(claude)) == 1
    assert "the refusal hook did not answer this call" in _reports(claude)[0][1]
    assert _end_whys(tmp_path) == ["blocked"]


def test_row3_the_event_holds_state_waiting_for_and_the_last_record(tmp_path: Path):
    claude, d, clock, live, _ = _stuck(tmp_path)

    clock.advance(30.0)
    d.tick()

    event = [e for e in _events(tmp_path) if e["kind"] == "permission_blocked"][0]
    assert event["state"] == BLOCKED_STATE
    assert event["waiting_for"] == PERMISSION_WAIT
    assert event["last_record"] == "tool_use_pending"


def test_row4_a_refusal_in_the_wait_gives_the_count_not_the_hook_sentence(tmp_path: Path):
    claude, d, clock, live, uuid = _stuck(tmp_path)
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with open(tmp_path / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": "permission_refused", "session": uuid,
                             "tool": "Bash", "call": "rmdir x"}) + "\n")
        # another session's refusal and an old one of this session must not count
        fh.write(json.dumps({"ts": ts, "kind": "permission_refused", "session": "other"}) + "\n")
        fh.write(json.dumps({"ts": "2020-01-01 00:00:00", "kind": "permission_refused",
                             "session": uuid}) + "\n")

    clock.advance(30.0)
    d.tick()

    message = _reports(claude)[0][1]
    assert "refused 1 call(s)" in message
    assert "did not answer" not in message


def test_a_missing_transcript_keeps_the_listing_reading(tmp_path: Path):
    claude, d, clock, live = _running_session(tmp_path)
    claude._sessions[live].update(state=BLOCKED_STATE, status="waiting", waitingFor=PERMISSION_WAIT)

    clock.advance(30.0)
    d.tick()

    assert len(_reports(claude)) == 1


def test_last_timed_record_type_reads_only_the_tail(tmp_path: Path):
    path = _write(tmp_path, "u", [ASSISTANT_TOOL] + [USER_RESULT] * 5000 + [ASSISTANT_TOOL])
    assert path.stat().st_size > 256 * 1024
    assert last_timed_record_type(path) == "tool_use_pending"
    assert last_timed_record_type(tmp_path / "nope.jsonl") is None


def test_the_message_with_a_count_and_without():
    assert "did not answer" in build_permission_block_message("s", "1 min")
    assert "refused 2 call(s)" in build_permission_block_message("s", "1 min", refused=2)
