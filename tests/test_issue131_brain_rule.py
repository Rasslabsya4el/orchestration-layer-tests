"""#131: the brain's "Never executes" paragraph carries the second-check sentence for verdict facts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs/agents/brain.md"
BASE_BULLETS = 18  # count of lines starting "- **" in docs/agents/brain.md: 16 before #131, +1 for #135 (purpose tag), +1 for #262 (goal-level stop and retro)


def _lines():
    return BRAIN.read_text(encoding="utf8").splitlines()


def _never_executes_paragraph():
    lines = _lines()
    start = next(i for i, line in enumerate(lines) if line.startswith("- **Never executes.**"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("- **"))
    return " ".join(line.strip() for line in lines[start:end])


def test_second_check_sentence_is_in_the_never_executes_paragraph():
    paragraph = _never_executes_paragraph()
    assert "second helper" in paragraph
    assert "--check agree|disagree" in paragraph
    assert "not the first answer" in paragraph


def test_bullet_count_unchanged_no_new_bullet():
    assert sum(1 for line in _lines() if line.startswith("- **")) == BASE_BULLETS
