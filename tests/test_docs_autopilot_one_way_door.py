"""The autopilot decides a reversible question provisionally; only a one-way door waits (issue #1133, unit 1).

A read-only check over docs/agents/autopilot.md. It does not start PoB.

(a) The reversible test is stated: "one revert" (one revert, a config flag or a doc line undoes it).
(b) The checkpoint's questions entry carries "one-way door: <what>" for each question.
(c) A session's final message never ends on a question to the owner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUTOPILOT = ROOT / "docs" / "agents" / "autopilot.md"
REQUIRED_PHRASES = (
    "one revert",
    "final message never ends on a question",
)
QUESTIONS_ENTRY = "`questions:`"
ONE_WAY_FORMAT = "one-way door: <what>"


def _text(path: Path) -> str:
    # Phrases wrap across lines in the rule files; compare on words, not on line breaks.
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("phrase", REQUIRED_PHRASES)
def test_autopilot_rule_states_the_one_way_door_statement(phrase: str) -> None:
    hits = _text(AUTOPILOT).count(phrase)
    assert hits >= 1, f"{AUTOPILOT.relative_to(ROOT)} lacks {phrase!r}"


def test_reversible_test_names_the_provisional_command() -> None:
    text = _text(AUTOPILOT)
    assert "python -m tools.agentops provisional" in text
    assert "one revert" in text


def test_questions_entry_carries_the_one_way_door_format() -> None:
    # The checkpoint's questions entry (the bullet that opens with `questions:`) names the format.
    text = _text(AUTOPILOT)
    start = text.find(QUESTIONS_ENTRY)
    assert start != -1, f"{AUTOPILOT.relative_to(ROOT)} lacks the {QUESTIONS_ENTRY} entry"
    window = text[start : start + 400]
    assert ONE_WAY_FORMAT in window, f"questions entry lacks {ONE_WAY_FORMAT!r}"
