"""Issue #81: the Stop hook `tools/hooks/state_claim_check.py` stops a brain reply that claims a
state ("готово", "done", "free", ...) when no reading tool ran in the same turn.

Seam: the hook's stdin payload and its stdout decision, run as a subprocess the way Claude Code
runs it. Fixtures carry the real transcript shape: each content block is its own record,
assistant text is `type: "assistant"` with `message.content[].type == "text"`, a tool call is a
`tool_use` block with `name`, a tool result is a `type: "user"` record with a `tool_result` block,
and a real owner prompt is a `type: "user"` record without one. Records of other types sit
between them and are skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "state_claim_check.py"
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"
TEMPLATE_PATH = PROJECT_ROOT / "config" / "core_templates" / "settings.hooks.json"
MANIFEST_PATH = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"

sys.path.insert(0, str(PROJECT_ROOT))
from tools.agentops.core_settings import load as _load_settings  # noqa: E402

MAIN_TITLE = _load_settings()["brain_chat_title"]
LOG_NAME = "state_claim.log"
SESSION = "81-session"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "repo": "owner/name",
        "brain_chat_title": MAIN_TITLE,
        "brain_chats": {"area:orchestration": "Brain: fake area"},
        "context_line": 150000,
        "claude_projects_dir": "x",
        "bridge_dir": str(tmp_path / "bridge"),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
    }), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(settings))
    monkeypatch.setenv("AGENTOPS_STATE_CLAIM_LOG", str(tmp_path / LOG_NAME))
    return tmp_path


# -- transcript records in the real shape ----------------------------------------------------------

_counter = iter(range(10_000))


def _owner(text: str = "проверь ПК") -> dict:
    """A real owner prompt: a user record with no tool_result block."""
    return {"type": "user", "message": {"role": "user", "content": text}}


def _text(text: str) -> dict:
    """One assistant text block, its own record."""
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _tool(name: str) -> dict:
    """One tool_use block, its own record."""
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{next(_counter)}", "name": name, "input": {}}]}}


def _result() -> dict:
    """A tool result: a user record carrying a tool_result block (not an owner prompt)."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]}}


def _noise() -> list[dict]:
    """Record types the hook must skip between the real ones."""
    return [{"type": "attachment", "attachment": {"type": "x"}},
            {"type": "system", "subtype": "y"},
            {"type": "queue-operation", "operation": "enqueue"}]


def _transcript(tmp_path: Path, records: list[dict], head: dict | None = None,
                malformed: bool = False) -> Path:
    """Write the records one per line. `head` is merged into the first record (the launch facts
    `sessionKind` / `agentSetting` of a background or other-agent session); `malformed` appends a
    broken line at the end."""
    lines = list(records)
    if head:
        lines[0] = {**lines[0], **head}
    text = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"
    if malformed:
        text += '{"type": "assistant", "message": {"content": [\n'
    path = tmp_path / "transcript.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def _run(transcript: object, stop_hook_active: bool = False,
         raw: str | None = None) -> subprocess.CompletedProcess:
    if raw is None:
        payload = {"hook_event_name": "Stop", "session_id": SESSION,
                   "stop_hook_active": stop_hook_active}
        if transcript is not None:
            payload["transcript_path"] = str(transcript)
        raw = json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=raw,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _block_reason(proc: subprocess.CompletedProcess) -> dict | None:
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    assert out["decision"] == "block"
    return out


# -- row 1: a claim with no check in the turn is blocked ------------------------------------------

def test_row1_claim_with_no_check_in_the_turn_is_blocked(_env, tmp_path):
    """Row 40's shape: owner prompt, one assistant text record "PC можно пользоваться", no tool."""
    t = _transcript(tmp_path, _noise() + [_owner(), *_noise(), _text("PC можно пользоваться")])
    out = _block_reason(_run(t))
    assert out is not None
    assert out["reason"]
    assert "check" in out["reason"]


def test_row1_block_is_logged_once_with_time_session_and_word(_env, tmp_path):
    t = _transcript(tmp_path, [_owner(), _text("PC можно пользоваться")])
    _block_reason(_run(t))
    rows = [json.loads(line) for line in (tmp_path / LOG_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["session"] == SESSION
    assert rows[0]["word"] == "можно пользоваться"
    assert rows[0]["ts"]


# -- row 2: a claim with a reading tool in the same turn passes -----------------------------------

@pytest.mark.parametrize("claim, tools", [
    # row 49's shape: one Bash read, a claim word in the closing text
    ("Разбор записан, готово.", ["Bash"]),
    # row 94: "Пересборка завершена", "Готово" with about twenty Bash calls
    ("Пересборка завершена. Готово.", ["Bash", "Bash", "Bash"]),
    # row 100: "пересобран в 03:32" with one Bash
    ("Пересобран в 03:32.", ["Bash"]),
    # row 161: partial move admitted, Bash reads before it
    ("Перенесено частично, часть перенёс.", ["Bash", "Bash"]),
])
def test_row2_claim_with_a_reading_tool_in_the_turn_passes(_env, tmp_path, claim, tools):
    records = [_owner("сделай")]
    for name in tools:
        records += [_tool(name), _result()]
    records.append(_text(claim))
    t = _transcript(tmp_path, records)
    assert _run(t).stdout.strip() == ""
    assert not (tmp_path / LOG_NAME).exists()


def test_row2_a_read_in_an_earlier_turn_does_not_count(_env, tmp_path):
    """The turn starts at the last owner prompt: a Bash call before it does not clear this claim."""
    records = [_owner("первое"), _tool("Bash"), _result(), _text("Смотрел, всё нормально."),
               _owner("а теперь?"), _text("Готово, всё собрано.")]
    t = _transcript(tmp_path, records)
    assert _block_reason(_run(t)) is not None


# -- row 3: stop_hook_active and background / other-agent transcripts pass ----------------------

def test_row3_stop_hook_active_gives_no_output(_env, tmp_path):
    t = _transcript(tmp_path, [_owner(), _text("PC можно пользоваться")])
    assert _run(t, stop_hook_active=True).stdout.strip() == ""


@pytest.mark.parametrize("head", [{"sessionKind": "bg"}, {"agentSetting": "orch"}])
def test_row3_background_or_other_agent_transcript_gives_no_output(_env, tmp_path, head):
    t = _transcript(tmp_path, [_owner(), _text("PC можно пользоваться")], head=head)
    assert _run(t).stdout.strip() == ""


def test_row3_the_brain_agent_setting_is_still_checked(_env, tmp_path):
    t = _transcript(tmp_path, [_owner(), _text("PC можно пользоваться")],
                    head={"agentSetting": "brain"})
    assert _block_reason(_run(t)) is not None


# -- rows 4: only messaging tools, and a turn with no claim word ------------------------------------

@pytest.mark.parametrize("tool", ["SendMessage", "Agent", "Task", "Write", "Edit", "Skill",
                                  "ToolSearch", "mcp__ccd_turn__open"])
def test_row4_claim_after_only_non_reading_tools_is_blocked(_env, tmp_path, tool):
    """Row 46 / 51 shape: a relay of a subagent report, SendMessage only, claim word in the text."""
    records = [_owner("отчёт"), _tool("SendMessage"), _result(), _tool(tool), _result()]
    if tool in ("Agent", "Task"):
        records = [_owner("отчёт"), _tool(tool), _result()]
    records.append(_text("Записал как решённое и передал. Готово."))
    t = _transcript(tmp_path, records)
    assert _block_reason(_run(t)) is not None


def test_row4_send_message_turn_with_a_claim_is_blocked(_env, tmp_path):
    records = [_owner("отчёт"), _tool("SendMessage"), _result(),
               _text("Субагент сообщил: собрано.")]
    assert _block_reason(_run(_transcript(tmp_path, records))) is not None


def test_row4_turn_with_no_claim_word_gives_no_output(_env, tmp_path):
    records = [_owner("передай"), _tool("SendMessage"), _result(),
               _text("Передал, жду ответа.")]
    assert _run(_transcript(tmp_path, records)).stdout.strip() == ""


def test_row4_turn_with_text_only_and_no_claim_word_gives_no_output(_env, tmp_path):
    t = _transcript(tmp_path, [_owner(), _text("Смотрю, что там.")])
    assert _run(t).stdout.strip() == ""


# -- row 5: broken input fails open ---------------------------------------------------------------

def test_row5_malformed_transcript_line_gives_no_output_and_exit_0(_env, tmp_path):
    t = _transcript(tmp_path, [_owner(), _text("PC можно пользоваться")], malformed=True)
    proc = _run(t)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_row5_missing_transcript_path_gives_no_output_and_exit_0(_env):
    proc = _run(None)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_row5_nonexistent_transcript_gives_no_output_and_exit_0(_env, tmp_path):
    proc = _run(tmp_path / "absent.jsonl")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize("raw", ["not json", "[]"])
def test_row5_broken_stdin_gives_no_output_and_exit_0(_env, raw):
    proc = _run(None, raw=raw)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# -- row 6: registration (Stop event) in both hook files and the manifest -----------------------

def test_row6_stop_hook_is_registered_in_settings_and_template_and_manifest():
    for path in (SETTINGS_PATH, TEMPLATE_PATH):
        entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
        hits = [e for e in entries if any("state_claim_check.py" in h["command"] for h in e["hooks"])]
        assert len(hits) == 1, str(path)
        assert hits[0]["hooks"][0]["type"] == "command"
        assert hits[0]["hooks"][0]["command"] == \
            'python "$CLAUDE_PROJECT_DIR/tools/hooks/state_claim_check.py"'
    manifest = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    assert "tools/hooks/state_claim_check.py" in manifest
