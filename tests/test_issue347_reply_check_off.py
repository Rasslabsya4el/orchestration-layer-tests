"""Issue #347, unit 1: the reply check is off by one switch, `REPLY_CHECK_ENABLED` in
`tools/hooks/brain_reset_nudge.py` (default False, the owner's trial of #344).

Off (the default): a brain prompt gets no reply-check text and the log gets no `reply-check` line;
the size nudge still joins the output as before. On: the injected text equals the block between the
markers of `docs/agents/brain.md` at commit e2ac3d8, byte for byte.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_reset_nudge.py"
# The reference block: docs/agents/brain.md at e2ac3d8, kept byte-exact as a fixture (#348). The live file
# carries no reply-check block since the new brain role (#348), so the block the switch would inject is read here.
DOC = PROJECT_ROOT / "tests" / "fixtures" / "brain_e2ac3d8.md"
RESTORE_REF = "e2ac3d8"

sys.path.insert(0, str(PROJECT_ROOT))
from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from tools.hooks import brain_reset_nudge as hook  # noqa: E402

START = "<!-- reply-check:start -->"
END = "<!-- reply-check:end -->"
MAIN_TITLE = _load_settings()["brain_chat_title"]
LOG_NAME = "brain_nudge.log"


def _block_of(text: str) -> str:
    assert text.count(START) == 1 and text.count(END) == 1
    return text.split(START)[1].split(END)[0].strip()


def _current_block() -> str:
    return _block_of(DOC.read_text(encoding="utf-8"))


def _restore_ref_or_skip() -> None:
    """Skip with the reason when the restore reference is not in this clone (the public test copy has no
    such history); every test that reads ``RESTORE_REF`` calls this first."""
    probe = subprocess.run(["git", "-C", str(PROJECT_ROOT), "cat-file", "-e", RESTORE_REF],
                           capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"commit {RESTORE_REF} is not reachable in this clone (the public test copy has no such history)")


def _restore_bytes() -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{RESTORE_REF}:docs/agents/brain.md"],
        capture_output=True, check=True,
    ).stdout


def _restore_block() -> str:
    return _block_of(_restore_bytes().decode("utf-8"))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Fake settings (main title), log in tmp; the real docs/agents/brain.md is read by the hook."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "repo": "owner/name",
        "brain_chat_title": MAIN_TITLE,
        "brain_chats": {},
        "context_line": 150000,
        "claude_projects_dir": "x",
        "bridge_dir": str(tmp_path / "bridge"),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
    }), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(settings))
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / LOG_NAME))
    monkeypatch.delenv("AGENTOPS_BRAIN_MD", raising=False)
    return tmp_path


def _transcript(tmp_path: Path, assistant_turns: int = 0, usage: dict | None = None) -> Path:
    lines = [{"type": "user", "message": {"content": "hi"}},
             {"type": "custom-title", "customTitle": MAIN_TITLE, "sessionId": "s1"}]
    for _ in range(assistant_turns):
        lines.append({"type": "assistant",
                      "message": {"id": "m", "usage": usage or {"input_tokens": 1}}})
        lines.append({"type": "user", "message": {"content": "next"}})
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _payload(transcript: Path) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript),
            "session_id": "s1"}


def _run_script(transcript: Path) -> subprocess.CompletedProcess:
    """The real hook script, default switch, as Claude Code runs it (a subprocess, no monkeypatch)."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(_payload(transcript)),
        capture_output=True, text=True, timeout=10,
    )


def _context(result: subprocess.CompletedProcess) -> str | None:
    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    assert len(lines) == 1
    return json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]


def _log_rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _reply_check_rows(tmp_path: Path) -> list[dict]:
    return [row for row in _log_rows(tmp_path)
            if row.get("action") == "reply-check" or "reply-check" in str(row.get("why", ""))]


# -- (1) the default switch: the 5th owner prompt of a brain chat gets no reply check -----------


def test_default_off_fifth_prompt_gets_no_block_text_and_no_reply_check_log_line(tmp_path):
    assert hook.REPLY_CHECK_ENABLED is False
    transcript = _transcript(tmp_path, assistant_turns=4)
    context = _context(_run_script(transcript))
    assert context is None
    assert _reply_check_rows(tmp_path) == []


# -- (2) the same with the context past the size line: the size nudge still comes -------------


def test_default_off_size_nudge_still_injected_without_the_block(tmp_path):
    settings_path = Path(os.environ["AGENTOPS_HOOK_SETTINGS"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["context_line"] = 1000
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    transcript = _transcript(tmp_path, assistant_turns=4, usage={"input_tokens": 5000})
    context = _context(_run_script(transcript))
    assert context is not None
    assert "over the context line" in context
    assert _current_block() not in context
    assert _reply_check_rows(tmp_path) == []


def test_default_off_first_prompt_hand_off_still_injected_without_the_block(tmp_path):
    """The other rules of the hook inject as today: the brain's first-prompt title text, no block."""
    transcript = _transcript(tmp_path, assistant_turns=0)
    context = _context(_run_script(transcript))
    assert context is not None
    assert context.startswith("This chat is titled")
    assert _current_block() not in context


def test_default_off_wayfinder_close_nudge_still_injects(tmp_path, monkeypatch):
    """The wayfinder-close nudge is untouched by the switch: same trigger as test_issue320_brain_rules.py row 1."""
    assert hook.REPLY_CHECK_ENABLED is False
    monkeypatch.setattr(hook, "_gh_labels", lambda n, timeout=5: ["wayfinder:map"])
    transcript = _transcript(tmp_path, assistant_turns=0)
    payload = {"hook_event_name": "PostToolUse", "transcript_path": str(transcript), "session_id": "s1",
               "tool_response": {"stdout": "✓ Closed issue o/n#5"}}
    out = hook.run(payload)
    assert out is not None
    assert "A wayfinder ticket closed (#5)" in out["hookSpecificOutput"]["additionalContext"]


def test_current_block_equals_the_e2ac3d8_block():
    """The block the hook reads from the repo's own docs/agents/brain.md is the e2ac3d8 block, unchanged."""
    _restore_ref_or_skip()
    assert _current_block() == _restore_block()


# -- (3) the switch on: the injected text is the e2ac3d8 block, byte for byte ----------------------


def test_switch_on_injects_the_e2ac3d8_block_byte_for_byte(tmp_path, monkeypatch):
    _restore_ref_or_skip()
    monkeypatch.setattr(hook, "REPLY_CHECK_ENABLED", True)
    brain_md = tmp_path / "brain.md"
    brain_md.write_bytes(_restore_bytes())
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    transcript = _transcript(tmp_path, assistant_turns=4)
    out = hook.handle_user_prompt_submit(_payload(transcript))
    assert out is not None
    assert out["hookSpecificOutput"]["additionalContext"] == _restore_block()
    assert len([row for row in _reply_check_rows(tmp_path) if row.get("action") == "reply-check"]) == 1
