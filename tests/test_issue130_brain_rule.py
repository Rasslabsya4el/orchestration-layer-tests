"""#130 unit 2: the brain's rule sends a survey of many sources to researcher-survey."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN_DOC = ROOT / "docs" / "agents" / "brain.md"
BRAIN_ROLE = ROOT / ".claude" / "agents" / "brain.md"


def _never_executes_paragraph() -> str:
    text = BRAIN_DOC.read_text(encoding="utf-8")
    start = text.index("- **Never executes.**")
    end = text.index("\n- **", start + 1)
    return text[start:end]


def test_brain_doc_names_researcher_survey():
    assert "researcher-survey" in BRAIN_DOC.read_text(encoding="utf-8")


def test_brain_role_names_researcher_survey():
    assert "researcher-survey" in BRAIN_ROLE.read_text(encoding="utf-8")


def test_brain_doc_names_general_purpose():
    assert "general-purpose" in BRAIN_DOC.read_text(encoding="utf-8")


def test_brain_role_names_general_purpose():
    assert "general-purpose" in BRAIN_ROLE.read_text(encoding="utf-8")


def test_never_executes_paragraph_routes_survey_to_researcher_survey():
    assert "researcher-survey" in _never_executes_paragraph()


def test_never_executes_paragraph_names_wrong_fact():
    assert "wrong-fact" in _never_executes_paragraph()
