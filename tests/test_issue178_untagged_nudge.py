"""Issue #178: the brain is reminded to tag its work the moment it starts untagged
(tools/hooks/brain_read_nudge.py, the Bash/PowerShell branch).

Seam under test: the hook run as a subprocess the way Claude Code runs it (a PreToolUse payload on stdin, a
``hookSpecificOutput`` line or nothing on stdout, one row in ``AGENTOPS_HOOK_LOG``), and, for row 7, the hook's
state function ``state_for_call`` against ``purposes._walk_transcript`` on one fixture. Transcripts are written by
hand in the shape of Claude Code's JSON lines.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_read_nudge.py"
for _path in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tools.agentops.purposes import PURPOSE_WORDS, UNTAGGED, _walk_transcript  # noqa: E402
from tools.agentops.core_settings import SETTINGS_PATH, load as load_settings  # noqa: E402
from tools.hooks import brain_read_nudge  # noqa: E402
from tools.hooks.brain_read_nudge import _tail_records, state_for_call  # noqa: E402

BRAIN_TITLE = "Brain: orch layer"
SIX_WORDS = ("closure-review", "grill", "ticket", "notice", "owner", "research")


@pytest.fixture(autouse=True)
def _hook_log(tmp_path, monkeypatch):
    """Keep the log and the nudge marker out of the real var/agentops folder."""
    path = tmp_path / "var" / "brain_nudge.log"
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "no-project"))
    monkeypatch.delenv("AGENTOPS_HOOK_SETTINGS", raising=False)
    return path


@pytest.fixture(autouse=True)
def _brain_settings(_hook_log, tmp_path, monkeypatch):
    """Pin the brain title the hook reads (``AGENTOPS_HOOK_SETTINGS``), so the repo's own
    ``config/orchestration.json`` title does not decide which transcripts are brain chats."""
    settings = load_settings(SETTINGS_PATH)
    settings.update(brain_chat_title=BRAIN_TITLE, brain_chats={})
    path = tmp_path / "settings" / "orchestration.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(path))
    return path


# --- fixture builders ---------------------------------------------------------------------------

def _title(title: str = BRAIN_TITLE) -> dict:
    return {"type": "custom-title", "customTitle": title, "sessionId": "sess-178"}


def _owner(n: int, text: str = "next") -> dict:
    return {"type": "user", "uuid": f"owner-{n}", "message": {"role": "user", "content": text}}


def _shell(message_id: str, command: str, name: str = "Bash") -> dict:
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": f"tu_{message_id}", "name": name, "input": {"command": command}}]}}


def _result(message_id: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"tu_{message_id}", "content": "ok"}]}}


def _write(tmp_path: Path, records: list) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records), encoding="utf-8")
    return path


def _run(transcript: Path, command: str, *, tool_name: str = "Bash", tool_use_id: str | None = None,
         extra: dict | None = None, event: str = "PreToolUse") -> subprocess.CompletedProcess:
    payload = {"hook_event_name": event, "tool_name": tool_name, "tool_input": {"command": command},
               "transcript_path": str(transcript), "session_id": "sess-178"}
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    payload.update(extra or {})
    return subprocess.run([sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=20)


def _context(proc: subprocess.CompletedProcess) -> str | None:
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    assert list(out) == ["hookSpecificOutput"]
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    return hso["additionalContext"]


def _log_rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


# --- row 1: an untagged call after an owner message ------------------------------------------------

def test_untagged_call_gets_one_nudge_naming_the_six_words(tmp_path, _hook_log):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    text = _context(_run(transcript, "ls -la"))
    assert text is not None
    assert len(text.splitlines()) <= 3
    for word in SIX_WORDS:
        assert word in text
    assert "# purpose:" in text and "untagged" in text
    assert 'docs/agents/brain-reference.md, "The purpose tags"' in text
    assert tuple(PURPOSE_WORDS) == SIX_WORDS
    rows = _log_rows(_hook_log)
    assert [(r["event"], r["action"], r["session"]) for r in rows] == [("PreToolUse", "untagged-nudge", "sess-178")]


# --- #368: the third-read nudge is gone; the untagged nudge is the hook's only text -------------------

def _read(message_id: str, path: str) -> dict:
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": [
        {"type": "tool_use", "id": f"tu_{message_id}", "name": "Read", "input": {"file_path": path}}]}}


def test_hook_module_has_no_third_read_text():
    assert not hasattr(brain_read_nudge, "NUDGE_TEXT")


def test_third_read_in_a_row_gets_no_text(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1), _read("r1", "a.md"), _result("r1"),
                                   _read("r2", "b.md"), _result("r2"), _read("r3", "c.md")])
    assert _context(_run(transcript, "c.md", tool_name="Read", tool_use_id="tu_r3")) is None


def test_post_tool_use_is_silent_even_when_untagged(tmp_path, _hook_log):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "ls -la", event="PostToolUse")) is None
    assert _log_rows(_hook_log) == []


# --- row 2: the call's own tag line ------------------------------------------------------------------

def test_own_purpose_tag_line_is_silent(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "# purpose: ticket\nls -la")) is None


def test_own_unknown_word_is_not_a_tag_and_is_nudged(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "# purpose: banana\nls")) is not None


# --- row 3: a tag already in force ---------------------------------------------------------------------

def test_tag_in_force_since_the_last_owner_message_is_silent(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "# purpose: grill\nls"), _result("m1")])
    assert _context(_run(transcript, "ls -la")) is None


def test_tag_before_the_last_owner_message_does_not_hold(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "# purpose: grill\nls"), _result("m1"),
                                   _owner(2)])
    assert _context(_run(transcript, "ls -la")) is not None


def test_the_current_calls_own_record_in_the_transcript_is_not_a_tag_before_it(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "ls -la")])
    assert _context(_run(transcript, "ls -la", tool_use_id="tu_m1")) is not None


# --- row 4: one nudge per owner message -------------------------------------------------------------------

def test_second_untagged_call_before_the_next_owner_message_is_silent(tmp_path, _hook_log):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "ls")) is not None
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "ls"), _result("m1")])
    assert _context(_run(transcript, "ls -la")) is None
    assert [r["action"] for r in _log_rows(_hook_log)] == ["untagged-nudge", "untagged-skip"]
    assert _log_rows(_hook_log)[1]["why"] == "already nudged"


def test_a_new_owner_message_allows_one_more_nudge(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "ls")) is not None
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "ls"), _result("m1"), _owner(2)])
    assert _context(_run(transcript, "ls")) is not None
    assert _context(_run(transcript, "ls")) is None


# --- row 5: PowerShell behaves as Bash ----------------------------------------------------------------------

def test_powershell_is_nudged_and_tagged_like_bash(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "# purpose: owner\nGet-ChildItem", tool_name="PowerShell")) is None
    text = _context(_run(transcript, "Get-ChildItem", tool_name="PowerShell"))
    assert text is not None and all(word in text for word in SIX_WORDS)


def test_a_powershell_tag_is_in_force_for_a_later_bash_call(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1), _shell("m1", "# purpose: research\nGet-Date", "PowerShell")])
    assert _context(_run(transcript, "ls")) is None


# --- row 6: who is never nudged ---------------------------------------------------------------------------------

def test_background_session_is_silent(tmp_path, _hook_log):
    transcript = _write(tmp_path, [{"sessionKind": "bg"}, _title(), _owner(1)])
    assert _context(_run(transcript, "ls")) is None
    assert _log_rows(_hook_log) == []


def test_another_agent_is_silent(tmp_path):
    transcript = _write(tmp_path, [{"agentSetting": "orch"}, _title(), _owner(1)])
    assert _context(_run(transcript, "ls")) is None
    brain_transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(brain_transcript, "ls", extra={"agent_type": "orch"})) is None


def test_a_chat_whose_title_is_not_a_brain_title_is_silent(tmp_path):
    transcript = _write(tmp_path, [_title("Some other chat"), _owner(1)])
    assert _context(_run(transcript, "ls")) is None
    untitled = _write(tmp_path, [_owner(1)])
    assert _context(_run(untitled, "ls")) is None


def test_a_subagent_call_is_silent(tmp_path):
    transcript = _write(tmp_path, [_title(), _owner(1)])
    assert _context(_run(transcript, "ls", extra={"agent_id": "a1b2c3"})) is None


# --- the tail window and bad input -----------------------------------------------------------------------------

def test_owner_message_outside_the_tail_window_is_a_logged_skip(tmp_path, _hook_log):
    padding = [_shell(f"f{i}", "echo " + "x" * 2000) for i in range(700)]
    transcript = _write(tmp_path, [_title(), _owner(1)] + padding)
    assert transcript.stat().st_size > 1024 * 1024
    assert _context(_run(transcript, "ls")) is None
    assert [(r["action"], r["why"]) for r in _log_rows(_hook_log)] == [("untagged-skip", "owner message outside tail")]


def test_bad_stdin_and_missing_transcript_are_silent(tmp_path):
    proc = subprocess.run([sys.executable, str(HOOK_SCRIPT)], input="not json", capture_output=True, text=True,
                          timeout=20)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert _context(_run(tmp_path / "missing.jsonl", "ls")) is None


# --- row 7: the hook's tag state is purpose_split's ---------------------------------------------------------------

def test_hook_state_equals_the_purposes_walk_on_every_shell_call(tmp_path):
    records = [
        _title(),
        _owner(1, "first"),
        _shell("a1", "ls"),                                   # untagged
        _shell("a2", "# purpose: grill\nls"),                 # grill
        _shell("a3", "git status"),                           # grill still in force
        _owner(2, "second"),                                  # reset
        _shell("a4", "git status"),                           # untagged
        _shell("a5", "# purpose: ticket\nGet-Date", "PowerShell"),
        _shell("a6", "ls"),                                   # ticket
        _shell("a7", "# purpose: banana\nls"),                # unknown word: back to untagged
        _shell("a8", "ls"),                                   # untagged
        _shell("a9", "  # purpose:   research  \nls"),        # known word, spaces around
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta line"}},
        _shell("a10", "ls"),                                  # a meta line is no reset: research
        {"type": "user", "message": {"role": "user", "content": "<task-notification>x</task-notification>"}},
        _shell("a11", "ls"),                                  # a task notice is no reset: research
        _result("a11"),
        _owner(3, "third"),
        _shell("a12", "echo no tag"),                         # untagged
    ]
    path = _write(tmp_path, records)
    walk = _walk_transcript(path)
    shell_ids = [r["message"]["id"] for r in records
                 if r.get("type") == "assistant" and r["message"]["content"][1]["name"] in ("Bash", "PowerShell")]
    assert len(shell_ids) == 12
    saw_tagged = saw_untagged = False
    for mid in shell_ids:
        call = next(r for r in records if r.get("type") == "assistant" and r["message"]["id"] == mid)
        block = call["message"]["content"][1]
        state = state_for_call(_tail_records(str(path)), block["id"], block["input"])
        expected = walk.message_purpose[mid] != UNTAGGED
        assert state.in_force == expected, mid
        assert state.purpose == walk.message_purpose[mid], mid
        saw_tagged |= expected
        saw_untagged |= not expected
    assert saw_tagged and saw_untagged
