"""Issue #213: a session nobody sits at refuses a permission prompt instead of waiting for it.

Two halves, both without a real `claude.exe`:

- the `PermissionRequest` hook (`tools/hooks/refuse_permission_prompt.py`): when the payload's
  transcript says the session was started with `claude --bg` -- the session's own records, and a
  subagent's under it, carry `sessionKind: bg` -- it denies and writes one event line. An
  interactive session's transcript carries no such mark and gets no decision at all: the prompt
  appears exactly as before.
- the dispatcher's safety net: a stubbed listing row that is `blocked` / `waitingFor = permission
  prompt` produces one event and one wake of the brain's chat within 2 ticks, and no more than one
  per `PERMISSION_REPORT_COOLDOWN` after that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools.agentops.core_settings import load as load_settings
from tools.agentops.dispatch import (
    BLOCKED_STATE,
    PERMISSION_REPORT_COOLDOWN,
    PERMISSION_WAIT,
    build_permission_block_message,
)
from tools.hooks import refuse_permission_prompt as hook
from tools.hooks.transcript import head_records, is_background

from tests.test_agentops_dispatch import (
    FakeClaude,
    FakeClock,
    FakeGh,
    _dispatcher,
    _events,
    _issue,
    _subagent,
)
from tests._area_rule import _no_area_rule  # noqa: F401

HOOK_PATH = Path(hook.__file__)

CALL = {"command": "rmdir 'C:/Users/user/AppData/Local/Temp/probe'",
        "description": "Remove the specified directory"}

#: The records a real `claude --bg` session writes first, cut down to the fields this hook reads
#: (shape copied from a measured run, #213: `sessionKind` rides on every conversation record, the
#: session's own and its subagents').
BG_HEAD = [
    {"type": "custom-title", "customTitle": "orch #213", "sessionId": "sess-1"},
    {"type": "permission-mode", "permissionMode": "default", "sessionId": "sess-1"},
    {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "go"},
     "permissionMode": "auto", "sessionKind": "bg", "sessionId": "sess-1"},
]
#: The same for the brain's chat: a human typed into it, so no record carries `sessionKind`.
INTERACTIVE_HEAD = [
    {"type": "custom-title", "customTitle": "brain", "sessionId": "sess-2"},
    {"type": "permission-mode", "permissionMode": "default", "sessionId": "sess-2"},
    {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "go"},
     "userType": "external", "sessionId": "sess-2"},
]
#: What a subagent of a background session writes into its own file under the parent's directory.
SUBAGENT_HEAD = [
    {"type": "user", "isSidechain": True, "agentId": "a8989f43a4e234aa0",
     "message": {"role": "user", "content": "run rmdir"}, "sessionKind": "bg",
     "sessionId": "sess-1"},
]


def _transcript(tmp_path: Path, records: list[dict], name: str = "t.jsonl") -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _payload(transcript: str | None, tool_input: dict | None = None, **extra) -> dict:
    payload = {"session_id": "sess-1", "hook_event_name": "PermissionRequest",
               "tool_name": "Bash", "tool_input": dict(CALL if tool_input is None else tool_input)}
    if transcript is not None:
        payload["transcript_path"] = transcript
    payload.update(extra)
    return payload


# -- the transcript head: how a session says whether anybody is sitting at it --------------------

def test_a_background_transcript_is_recognised_and_an_interactive_one_is_not(tmp_path: Path):
    assert is_background(_payload(_transcript(tmp_path, BG_HEAD)))
    assert not is_background(_payload(_transcript(tmp_path, INTERACTIVE_HEAD, "i.jsonl")))


def test_a_subagent_of_a_background_session_is_recognised_by_its_own_transcript(tmp_path: Path):
    """The row-1 case: the prompt is raised inside a Task, and the file named may be the
    subagent's own, under `<project>/<session-id>/subagents/`. Claude Code copies `sessionKind`
    into it, so the same head read answers (measured, #213)."""

    path = _transcript(tmp_path, SUBAGENT_HEAD, "sess-1/subagents/agent-a898.jsonl")

    assert is_background(_payload(path))


def test_no_readable_transcript_is_not_evidence_that_nobody_is_there(tmp_path: Path):
    assert not is_background(_payload(None))
    assert not is_background(_payload(str(tmp_path / "gone.jsonl")))
    assert not is_background(_payload(""))
    assert not is_background("not a payload")


def test_the_head_read_stops_at_its_window(tmp_path: Path):
    """The window is what bounds the cost of a hook that runs on every prompt: a mark past it is
    not read."""

    path = _transcript(tmp_path, [{"type": "pad", "x": "y" * 4000}] * 5 + BG_HEAD)

    assert is_background(_payload(path))
    assert not is_background(_payload(path), chunk_size=64)


def test_only_lines_naming_a_key_are_parsed(tmp_path: Path):
    """The substring filter keeps a 60 kB system-prompt line out of `json.loads`; a line that does
    not mention the key is never parsed, so broken JSON there costs nothing."""

    path = tmp_path / "mixed.jsonl"
    path.write_text("{ this line is not JSON at all }\n"
                    + json.dumps({"sessionKind": "bg"}) + "\n", encoding="utf-8")

    assert [r["sessionKind"] for r in head_records(str(path), ("sessionKind",))] == ["bg"]


# -- the hook: a background session -> deny ------------------------------------------------------

def test_a_background_session_is_denied_and_writes_one_event_line(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    payload = _payload(_transcript(tmp_path, BG_HEAD), session_id="abc123")

    decision = hook.handle(payload, {hook.EVENTS_ENV: str(events)})

    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["hookEventName"] == "PermissionRequest"
    assert out["decision"]["behavior"] == "deny"
    assert out["decision"]["message"]

    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "permission_refused"
    assert row["session"] == "abc123"
    assert row["tool"] == "Bash"
    assert row["call"] == "rmdir 'C:/Users/user/AppData/Local/Temp/probe'"
    assert row["ts"]


def test_the_event_keeps_only_the_first_100_characters_of_the_call(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    payload = _payload(_transcript(tmp_path, BG_HEAD), {"command": "rm -rf " + "a" * 300})

    hook.handle(payload, {hook.EVENTS_ENV: str(events)})

    row = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert len(row["call"]) == 100
    assert row["call"].startswith("rm -rf aaa")


def test_a_tool_with_no_command_string_still_names_its_call(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    payload = _payload(_transcript(tmp_path, BG_HEAD), {"unknown": "x" * 400})
    payload["tool_name"] = "WebFetch"

    hook.handle(payload, {hook.EVENTS_ENV: str(events)})

    row = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert row["tool"] == "WebFetch" and len(row["call"]) == 100 and "unknown" in row["call"]


def test_a_failed_event_write_never_withholds_the_refusal(tmp_path: Path):
    blocked = tmp_path / "taken"
    blocked.write_text("not a directory", encoding="utf-8")
    payload = _payload(_transcript(tmp_path, BG_HEAD))

    decision = hook.handle(payload, {hook.EVENTS_ENV: str(blocked / "events.jsonl")})

    assert decision is not None
    assert decision["hookSpecificOutput"]["decision"]["behavior"] == "deny"


# -- the hook: an interactive session -> no decision ---------------------------------------------

def test_an_interactive_session_keeps_its_prompt(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    payload = _payload(_transcript(tmp_path, INTERACTIVE_HEAD))

    assert hook.handle(payload, {hook.EVENTS_ENV: str(events)}) is None
    assert not events.exists()


def test_a_payload_naming_no_readable_transcript_keeps_its_prompt(tmp_path: Path):
    events = tmp_path / "events.jsonl"

    assert hook.handle(_payload(None), {hook.EVENTS_ENV: str(events)}) is None
    assert hook.handle(_payload(str(tmp_path / "gone.jsonl")),
                       {hook.EVENTS_ENV: str(events)}) is None
    assert not events.exists()


# -- the hook as Claude Code runs it: a process, stdin in, stdout out ----------------------------

def _run_hook(payload: dict, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK_PATH)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=120)


def test_the_hook_process_prints_the_deny_decision(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    payload = _payload(_transcript(tmp_path, BG_HEAD))

    result = _run_hook(payload, {hook.EVENTS_ENV: str(events)})

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert len(events.read_text(encoding="utf-8").splitlines()) == 1


def test_the_hook_process_prints_nothing_for_an_interactive_session(tmp_path: Path):
    payload = _payload(_transcript(tmp_path, INTERACTIVE_HEAD))

    result = _run_hook(payload, {hook.EVENTS_ENV: str(tmp_path / "events.jsonl")})

    assert result.returncode == 0 and result.stdout.strip() == ""


def test_a_payload_the_hook_cannot_read_leaves_the_flow_alone(tmp_path: Path):
    result = subprocess.run([sys.executable, str(HOOK_PATH)], input="not json at all",
                            capture_output=True, text=True, env=dict(os.environ), timeout=120)

    assert result.returncode == 0 and result.stdout.strip() == ""


# -- the dispatcher's safety net -----------------------------------------------------------------

def _blocked_row(claude: FakeClaude, live: str) -> None:
    claude._sessions[live].update(state=BLOCKED_STATE, status="waiting", waitingFor=PERMISSION_WAIT)


def _running(tmp_path: Path, claude: FakeClaude, live: str) -> None:
    """The live case #213 is about: the parent stopped on the prompt its subagent raised, so the
    subagent is still running and the dispatcher's own state machine leaves the session alone."""

    _subagent(tmp_path / "projects", claude._sessions[live]["sessionId"], "aaa")


def _stuck_dispatcher(tmp_path: Path) -> tuple[FakeClaude, FakeGh, object, FakeClock, str]:
    """One live session, polled once while moving, then stopped on a permission prompt."""

    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(7)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()                       # starts the session
    live = claude.list_sessions()[0]["id"]
    gh.claim(7)
    clock.advance(30.0)
    d.tick()                       # adopted-tick rule: polled from here, and still moving
    _running(tmp_path, claude, live)
    _blocked_row(claude, live)
    claude.waked.clear()
    return claude, gh, d, clock, live


def _reports(claude: FakeClaude) -> list[tuple[str, str]]:
    return [w for w in claude.waked if PERMISSION_WAIT in w[1]]


def test_a_blocked_session_is_reported_to_the_brain_within_two_ticks(tmp_path: Path):
    claude, gh, d, clock, live = _stuck_dispatcher(tmp_path)

    clock.advance(30.0)
    d.tick()                       # the first tick that sees it: one event, one wake

    reports = _reports(claude)
    assert len(reports) == 1
    target, message = reports[0]
    assert target == load_settings()["brain_chat_title"]
    assert live in message
    assert "30 s" in message       # a human duration, never a bare count of seconds

    blocked_events = [e for e in _events(tmp_path) if e["kind"] == "permission_blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0]["session"] == live
    assert blocked_events[0]["waited"] == "30 s"
    assert blocked_events[0]["waited_seconds"] == 30.0


def test_the_same_stuck_session_is_not_reported_again_within_fifteen_minutes(tmp_path: Path):
    claude, gh, d, clock, live = _stuck_dispatcher(tmp_path)

    for _ in range(20):            # 20 ticks of 30 s = 10 min, still inside the cooldown
        clock.advance(30.0)
        d.tick()
    assert len(_reports(claude)) == 1

    clock.advance(PERMISSION_REPORT_COOLDOWN)
    d.tick()
    reports = _reports(claude)
    assert len(reports) == 2
    assert "25 min" in reports[1][1]      # 30 s + 10 min + 15 min of waiting

    blocked_events = [e for e in _events(tmp_path) if e["kind"] == "permission_blocked"]
    assert len(blocked_events) == 2


def test_a_session_that_never_blocks_is_never_reported(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(7)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    gh.claim(7)
    for _ in range(6):
        clock.advance(30.0)
        d.tick()

    assert [e for e in _events(tmp_path) if e["kind"] == "permission_blocked"] == []
    assert _reports(claude) == []


def test_a_session_that_moves_again_starts_the_count_over(tmp_path: Path):
    claude, gh, d, clock, live = _stuck_dispatcher(tmp_path)

    clock.advance(30.0)
    d.tick()                        # reported once
    claude._sessions[live].update(state="working", status="busy", waitingFor=None)
    clock.advance(30.0)
    d.tick()                        # it moved: the mark is cleared

    current = d.sessions[0]
    assert current.blocked_since is None and current.blocked_reported is None

    _blocked_row(claude, live)      # stuck again: reported again, cooldown or not
    clock.advance(30.0)
    d.tick()
    assert len(_reports(claude)) == 2


def test_a_blocked_session_the_dispatcher_ends_is_still_reported_first(tmp_path: Path):
    """No subagent is running, so the state machine ends the session this same tick -- the brain
    still hears about the prompt that stopped it."""

    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(7)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    live = claude.list_sessions()[0]["id"]
    gh.claim(7)
    clock.advance(30.0)
    d.tick()
    _blocked_row(claude, live)
    claude.waked.clear()

    clock.advance(30.0)
    d.tick()

    assert len(_reports(claude)) == 1
    assert [e["kind"] for e in _events(tmp_path)].count("permission_blocked") == 1
    assert live in claude.stopped   # and the session is ended as before


def test_the_message_names_the_session_and_its_wait():
    message = build_permission_block_message("sess-1", "4.0 min", "orch #213")

    assert "orch #213" in message and "sess-1" in message and "4.0 min" in message
    assert "permission prompt" in message
