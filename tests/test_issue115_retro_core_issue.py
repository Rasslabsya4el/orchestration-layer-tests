"""Issue #115: a retro or a prophylaxis finding about the orchestration core is filed as a core issue at the home.

Reads the three rule blocks that open tickets for a finding and checks that each names `core issue`, and that no
sentence in the two prophylaxis blocks says a finding "becomes a ticket" / "a ticket" without naming it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRO = ROOT / "docs" / "agents" / "retro.md"
BRAIN = ROOT / "docs" / "agents" / "brain.md"
WORKFLOW = ROOT / "docs" / "agents" / "workflow.md"

TICKET = re.compile(r"\bbecomes a ticket\b|\ba ticket\b")


def _section(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head:text.index(end, head)]


def _brain_block() -> str:
    return _section(BRAIN.read_text(encoding="utf-8"), "## Self-improvement changes", "## Working with the owner")


def _workflow_block() -> str:
    return _section(WORKFLOW.read_text(encoding="utf-8"), "Weekly, in the brain's prophylaxis",
                    "## Issue categories and relations")


def _sentences(block: str) -> list[str]:
    flat = " ".join(block.split())
    return [sentence for sentence in re.split(r"(?<=\.)\s+", flat) if sentence]


def test_retro_reference_names_core_issue():
    assert "core issue" in RETRO.read_text(encoding="utf-8")


def test_brain_self_improvement_block_names_core_issue():
    assert "core issue" in _brain_block()


def test_workflow_weekly_block_names_core_issue():
    assert "core issue" in _workflow_block()


def test_no_ticket_sentence_without_core_issue_in_either_block():
    for name, block in (("brain.md", _brain_block()), ("workflow.md", _workflow_block())):
        for sentence in _sentences(block):
            if TICKET.search(sentence):
                assert "core issue" in sentence, f"{name}: {sentence!r}"
