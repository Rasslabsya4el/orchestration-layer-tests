"""#130 unit 2: the brain's rule sends a survey of many sources to researcher-survey."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN_REFERENCE = ROOT / "docs" / "agents" / "brain-reference.md"


def _which_helper_bullet() -> str:
    text = BRAIN_REFERENCE.read_text(encoding="utf-8")
    start = text.index("- **Which helper.**")
    end = text.index("\n- **", start + 1)
    return text[start:end]


def test_brain_doc_names_researcher_survey():
    assert "researcher-survey" in BRAIN_REFERENCE.read_text(encoding="utf-8")


def test_which_helper_routes_survey_to_researcher_survey():
    assert "`researcher-survey` (many sources into one table, one row per item" in _which_helper_bullet()
