"""Issue #369 unit 1: the shared rule text drops the pre-v2 lead era and the [v2: ...] patches.

Rows 1, 2, 3, 4 (workflow.md part), 5 and 12 of the issue table. Every check reads the file text, so a
regression names the phrase it found. The section-pointer scan (tests/test_section_pointers.py) covers the
``docs/agents/...`` pointers this unit writes.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "docs" / "agents"
WORKFLOW = AGENTS / "workflow.md"
SESSIONS = AGENTS / "sessions.md"
CLAUDE = ROOT / "CLAUDE.md"

# Row 12: the repo-part chat sentence is scoped to an interactive chat the owner opened.
SCOPED_CHAT_PHRASE = "an interactive chat the owner opened"
OLD_UNSCOPED_CHAT_SENTENCE = "A chat here that is not the brain hands every change"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_v2_bracket_in_agent_docs_or_claude_md():
    # Row 4: every `[v2: ...]` patch is gone; the grep `\[v2:` is 0 lines.
    hits = {}
    for path in sorted(AGENTS.glob("*.md")) + [CLAUDE]:
        found = [line for line in _text(path).splitlines() if "[v2" in line]
        if found:
            hits[path.relative_to(ROOT).as_posix()] = found
    assert not hits, f"[v2 brackets left: {hits}"


def test_workflow_lead_era_paragraph_is_gone():
    # Row 1: the lead-era paragraph and its superseded tail are replaced by the v2 sentence.
    text = _text(WORKFLOW)
    for phrase in ("the lead reviews the diff", "merges in order", "Wall-clock progress is what counts",
                   "the team-lead sentences are superseded"):
        assert phrase not in text, f"workflow.md still has lead-era text: {phrase!r}"


def test_workflow_has_no_principle_6_pointer():
    # Row 2: the benchmark step is named without the number ("principle 6" is not a point of the core block).
    assert "principle 6" not in _text(WORKFLOW)


def test_workflow_progress_table_clause_is_gone():
    # Row 3: the progress-table/estimate clause is dropped or points at brain-reference "The results review".
    text = _text(WORKFLOW)
    assert "progress table" not in text
    assert "revised estimate" not in text


def test_sessions_has_no_day_rhythm_or_hours_rule():
    # Row 5: a new chat starts when the brain clears at the context line; no day count, no hours rule.
    text = _text(SESSIONS)
    assert "About once a day" not in text
    assert "inside the hours" not in text
    assert "never at night" not in text


def test_sessions_new_chat_follows_the_context_line():
    # Row 5: the new-chat rule names the clear at the context line and its section in brain-reference.md.
    text = _text(SESSIONS)
    assert "context line" in text
    assert 'brain-reference.md` "The clear and the hand-off"' in text


def test_claude_md_chat_sentence_is_scoped_to_an_interactive_chat():
    # Row 12: the repo-part sentence concerns an interactive chat the owner opened, not ORCH or subagents.
    head = _text(CLAUDE).split("@docs/agents/core-block.md", 1)[0]
    assert SCOPED_CHAT_PHRASE in head, "CLAUDE.md repo part does not scope the chat sentence"
    assert OLD_UNSCOPED_CHAT_SENTENCE not in _text(CLAUDE), "the unscoped chat sentence is still there"


def test_claude_md_import_line_is_untouched():
    # Constraint of the unit: the core import line stays exactly as the core ships it.
    assert "@docs/agents/core-block.md" in _text(CLAUDE).splitlines()
