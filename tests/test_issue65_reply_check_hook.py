"""Issue #65, unit 1 (hook): the reply check reaches every owner prompt of a brain chat.

`tools/hooks/brain_reset_nudge.py`'s `UserPromptSubmit` handler adds the block between the markers
`<!-- reply-check:start -->` and `<!-- reply-check:end -->` of `docs/agents/brain.md` as
`additionalContext`, on every owner prompt of an interactive chat whose title is `brain_chat_title`
or one of the `brain_chats` titles. The block is read at run time (`AGENTOPS_BRAIN_MD` overrides the
path for tests), capped at 450 tokens (chars / 2.5), and logged once per injection.

Fake transcripts and env overrides (`AGENTOPS_HOOK_SETTINGS`, `AGENTOPS_HOOK_LOG`,
`AGENTOPS_BRAIN_MD`) follow `tests/test_brain_chat_first_prompt.py`.
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

START = "<!-- reply-check:start -->"
END = "<!-- reply-check:end -->"
BLOCK = "Reply check:\n- Answer first, then the reason.\n- Name the ticket and what happened since."
#: the main title is the real `brain_chat_title`: the existing first-prompt text matches that
#: module constant (as in test_brain_chat_first_prompt.py), so the fake settings must carry it too.
MAIN_TITLE = _load_settings()["brain_chat_title"]
AREA_TITLE = "Brain: fake area"
LOG_NAME = "brain_nudge.log"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Fake settings (main title + one area chat), fake brain.md with the markers, log in tmp."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "repo": "owner/name",
        "brain_chat_title": MAIN_TITLE,
        "brain_chats": {"area:orchestration": AREA_TITLE},
        "context_line": 150000,
        "claude_projects_dir": "x",
        "bridge_dir": str(tmp_path / "bridge"),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
    }), encoding="utf-8")
    brain_md = tmp_path / "brain.md"
    _write_brain_md(brain_md, BLOCK)
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(settings))
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / LOG_NAME))
    return tmp_path


def _write_brain_md(path: Path, block: str, markers: bool = True) -> None:
    text = "# Brain\n\nText outside the markers.\n\n"
    if markers:
        text += f"{START}\n{block}\n{END}\n\nMore text outside.\n"
    else:
        text += block + "\n"
    path.write_text(text, encoding="utf-8")


def _transcript(tmp_path: Path, title: str | None, assistant_turns: int = 0,
                head: dict | None = None, usage: dict | None = None) -> Path:
    """A transcript: an optional head record merged into the first user record (``sessionKind`` /
    ``agentSetting``), a ``custom-title`` record when ``title`` is set, and ``assistant_turns``
    assistant turns, each followed by the next owner prompt."""
    first = {"type": "user", "message": {"content": "hi"}}
    if head:
        first.update(head)
    lines = [first]
    if title is not None:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": "s1"})
    for _ in range(assistant_turns):
        lines.append({"type": "assistant",
                      "message": {"id": "m", "usage": usage or {"input_tokens": 1}}})
        lines.append({"type": "user", "message": {"content": "next"}})
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _run(transcript: Path, agent_type: str | None = None) -> subprocess.CompletedProcess:
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": str(transcript),
               "session_id": "s1"}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
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


# -- row 1: the brain's own title gets the block on the 1st and on the 5th prompt ---------------


def test_row1_first_prompt_gets_title_text_then_the_block(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE)
    context = _context(_run(transcript))
    assert context is not None
    assert context.startswith("This chat is titled")
    assert context.endswith("\n\n" + BLOCK)


def test_row1_fifth_prompt_gets_exactly_the_block(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) == BLOCK


# -- row 2: a brain_chats title gets it too ----------------------------------------------------


def test_row2_brain_chats_title_gets_the_block_on_a_later_prompt(tmp_path):
    transcript = _transcript(tmp_path, AREA_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) == BLOCK


def test_row2_brain_chats_title_gets_the_block_on_the_first_prompt(tmp_path):
    """Only `brain_chat_title` gets the existing first-prompt "you are the brain" text; a
    `brain_chats` title gets the block alone."""
    transcript = _transcript(tmp_path, AREA_TITLE)
    assert _context(_run(transcript)) == BLOCK


# -- row 3: nothing for a bg session, an orch session, an untitled chat or another title -------


def test_row3_background_session_gets_nothing(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4, head={"sessionKind": "bg"})
    assert _context(_run(transcript)) is None


def test_row3_background_autopilot_brain_session_gets_nothing(tmp_path):
    """The autopilot is `claude --bg --agent brain`: a bg session, never a brain chat."""
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4,
                             head={"sessionKind": "bg", "agentSetting": "brain"})
    assert _context(_run(transcript, agent_type="brain")) is None


def test_row3_orch_agent_in_transcript_head_gets_nothing(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4,
                             head={"agentSetting": "orch"})
    assert _context(_run(transcript)) is None


def test_row3_orch_agent_in_payload_gets_nothing(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript, agent_type="orch")) is None


def test_row3_untitled_chat_gets_nothing(tmp_path):
    transcript = _transcript(tmp_path, None, assistant_turns=4)
    assert _context(_run(transcript)) is None


def test_row3_other_title_gets_nothing(tmp_path):
    transcript = _transcript(tmp_path, "some other chat", assistant_turns=4)
    assert _context(_run(transcript)) is None


# -- row 4: byte for byte, at most 450 tokens (chars / 2.5), missing markers ---------------------


def test_row4_injected_text_is_the_marked_block_only(tmp_path, monkeypatch):
    brain_md = tmp_path / "brain.md"
    _write_brain_md(brain_md, "\n   " + BLOCK + "   \n")
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) == BLOCK


def test_row4_block_of_1100_chars_is_injected_at_440_tokens(tmp_path, monkeypatch):
    block = "a" * 1100
    brain_md = tmp_path / "brain.md"
    _write_brain_md(brain_md, block)
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    context = _context(_run(transcript))
    assert context == block
    assert len(context) / 2.5 <= 450


def test_row4_block_of_1200_chars_is_refused_and_logged_too_long(tmp_path, monkeypatch):
    """1200 chars is 480 tokens by chars / 2.5: over the 450-token cap, so nothing is injected."""
    brain_md = tmp_path / "brain.md"
    _write_brain_md(brain_md, "a" * 1200)
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) is None
    rows = _log_rows(tmp_path)
    assert any("too long" in str(row.get("why", "")) for row in rows)
    assert not any(row.get("action") == "reply-check" for row in rows)


def test_row4_missing_markers_inject_nothing_and_log(tmp_path, monkeypatch):
    brain_md = tmp_path / "brain.md"
    _write_brain_md(brain_md, BLOCK, markers=False)
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) is None
    rows = _log_rows(tmp_path)
    assert any("markers" in str(row.get("why", "")) for row in rows)


def test_row4_missing_file_injects_nothing_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(tmp_path / "no-such-brain.md"))
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) is None
    rows = _log_rows(tmp_path)
    assert any("missing file" in str(row.get("why", "")) for row in rows)


# -- the title is the last custom-title in the whole transcript (desktop chats write it past the
# -- 64 KiB head: measured first offsets ~96,000 bytes, renames up to ~1 MB) ----------------------

PADDING = {"type": "user", "message": {"content": "x" * 80_000}}


def _late_title_records(titles: list[str], assistant_turns: int = 4) -> list[dict]:
    """A transcript whose custom-title records sit after more than 70 KB of padding, then
    ``assistant_turns`` assistant turns each followed by the next owner prompt."""
    records = [{"type": "user", "message": {"content": "hi"}}, PADDING]
    records += [{"type": "custom-title", "customTitle": t, "sessionId": "s1"} for t in titles]
    for _ in range(assistant_turns):
        records.append({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 1}}})
        records.append({"type": "user", "message": {"content": "next"}})
    return records


def _write_records(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_late_custom_title_past_the_head_gets_the_block_on_the_fifth_prompt(tmp_path):
    transcript = _write_records(tmp_path, _late_title_records([AREA_TITLE]))
    assert _context(_run(transcript)) == BLOCK


def test_rename_later_in_the_file_uses_the_last_title_renamed_into_a_brain_chat(tmp_path):
    transcript = _write_records(tmp_path, _late_title_records(["some other chat", AREA_TITLE]))
    assert _context(_run(transcript)) == BLOCK


def test_rename_later_in_the_file_away_from_a_brain_chat_gets_nothing(tmp_path):
    transcript = _write_records(tmp_path, _late_title_records([AREA_TITLE, "some other chat"]))
    assert _context(_run(transcript)) is None


# -- logging and the existing size nudge --------------------------------------------------------


def test_one_log_line_per_injection(tmp_path):
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4)
    assert _context(_run(transcript)) == BLOCK
    rows = [row for row in _log_rows(tmp_path) if row.get("action") == "reply-check"]
    assert len(rows) == 1
    assert rows[0]["event"] == "UserPromptSubmit"


def test_size_nudge_still_joins_the_block(tmp_path):
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    settings["context_line"] = 1000
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    transcript = _transcript(tmp_path, MAIN_TITLE, assistant_turns=4,
                             usage={"input_tokens": 5000})
    context = _context(_run(transcript))
    assert context is not None
    assert context.startswith(BLOCK + "\n\n")
    assert "over the context line" in context
