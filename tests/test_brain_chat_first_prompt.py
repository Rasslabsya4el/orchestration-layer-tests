"""Tests for issue #221: a target's first brain chat, opened as a plain chat.

Row 2 ("acceptance"): `tools/hooks/brain_reset_nudge.py`'s existing `UserPromptSubmit` hook, at a
chat's first prompt, when the chat's title equals `brain_chat_title` -- title matches -> the text
is injected once; title differs -> nothing injected.

Row 3: the port's own report line (step 8 of `docs/agents/PORTING.md`) ends with the next step for
the human, the title of the brain chat to open.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_reset_nudge.py"

sys.path.insert(0, str(PROJECT_ROOT))
from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from tools.agentops import port  # noqa: E402

BRAIN_CHAT_TITLE = _load_settings()["brain_chat_title"]


@pytest.fixture(autouse=True)
def _hook_log(tmp_path, monkeypatch):
    """Keep every test's log lines out of the real var/agentops log (mirrors
    test_brain_reset_nudge.py's own fixture)."""
    path = tmp_path / "brain_nudge.log"
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(path))
    return path


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _write_first_prompt_transcript(tmp_path: Path, title: str, model: str | None = None) -> Path:
    """A transcript with a `custom-title` record and no assistant turn yet -- the shape of a
    chat's very first prompt (issue #221)."""
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "custom-title", "customTitle": title, "sessionId": "s1"},
    ]
    if model is not None:
        lines.append({
            "attachment": {
                "type": "model",
                "identity": {"modelId": "claude-opus-5-5", "marketingName": model},
            },
        })
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_title_match_injects_once(tmp_path):
    """Acceptance row 2: title matches -> the text is injected once."""
    transcript = _write_first_prompt_transcript(tmp_path, BRAIN_CHAT_TITLE)
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript)}
    result = _run_hook(payload)
    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    output = json.loads(lines[0])
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "brain" in context.lower()
    assert "you are the brain" in context
    assert "docs/agents/brain.md" not in context  # #368 unit A: the file is injected whole, not re-read


def test_title_differs_injects_nothing(tmp_path):
    """Acceptance row 2: title differs -> nothing injected."""
    transcript = _write_first_prompt_transcript(tmp_path, "some other chat")
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript)}
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_not_the_first_prompt_gets_no_first_prompt_text(tmp_path):
    """Told it is the brain at its first prompt only: a transcript that already holds an assistant turn gets
    no such text even when the title matches (the reply check of #65 is on every prompt; its own tests)."""
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "custom-title", "customTitle": BRAIN_CHAT_TITLE, "sessionId": "s1"},
        {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1}}},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript)}
    result = _run_hook(payload)
    assert result.returncode == 0
    assert "you are the brain" not in result.stdout


def test_title_match_with_non_primary_model_names_the_core_row(tmp_path):
    """The model/effort mismatch note: a chat left at the app's default model (issue #221's own
    `claude-opus-5-5`) is told the core's own brain row next to its own model."""
    transcript = _write_first_prompt_transcript(tmp_path, BRAIN_CHAT_TITLE, model="Opus 5.5")
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript)}
    result = _run_hook(payload)
    assert result.returncode == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
    assert "Opus 5.5" in context
    assert "Fable 5.1" in context


def test_background_session_gets_nothing_even_at_a_matching_title(tmp_path):
    """The existing brain-only gate (`_not_brain`) still applies: a `claude --bg` session is
    never told it is the brain, whatever its title."""
    lines = [
        {"type": "user", "message": {"content": "hi"}, "sessionKind": "bg"},
        {"type": "custom-title", "customTitle": BRAIN_CHAT_TITLE, "sessionId": "s1"},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript)}
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# -- row 3: the port's report line ends with the brain chat's title to open ----------------------


def test_port_report_line_ends_with_the_brain_chat_title_to_open():
    """Row 3: the report line (step 8 of docs/agents/PORTING.md) ends with the title of the brain
    chat to open."""
    report = port.PortReport()
    report.next_step = port.next_step_message("orch-port-test", None)
    rendered = report.render()
    assert rendered.rstrip().endswith('"Brain: orch-port-test"')


def test_port_report_line_uses_an_explicit_brain_title():
    report = port.PortReport()
    report.next_step = port.next_step_message("orch-port-test", "Brain: custom")
    rendered = report.render()
    assert rendered.rstrip().endswith('"Brain: custom"')
