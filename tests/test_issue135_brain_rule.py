"""#135 unit 2: the brain's purpose tag is in the section "What the brain does" of docs/agents/brain.md,
and the brain's role file points to that protocol."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN_DOC = ROOT / "docs" / "agents" / "brain.md"
BRAIN_ROLE = ROOT / ".claude" / "agents" / "brain.md"
WORDS = ("closure-review", "grill", "ticket", "notice", "owner", "research")


def _what_the_brain_does() -> str:
    lines = BRAIN_DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index("## What the brain does")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return "\n".join(lines[start:end])


def test_section_names_the_purpose_tag():
    assert "# purpose:" in _what_the_brain_does()


def test_section_holds_each_purpose_word():
    section = _what_the_brain_does()
    missing = [word for word in WORDS if f"`{word}`" not in section]
    assert missing == []


def test_brain_role_points_to_the_protocol():
    assert "docs/agents/brain.md" in BRAIN_ROLE.read_text(encoding="utf-8")
