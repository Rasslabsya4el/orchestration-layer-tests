"""Issue #369 unit 2: the research and brain-reference rule files hold no lead-era or Poe-Junkroan-only text.

Rows 6-9 of the issue table: research.md says ORCH merges and the brain reads at source only where a verdict
turns; the domain wiki cache and the conservative-number practice leave the core file (one line in this repo's
local file points at Poe-Junkroan); brain-reference.md points at "The chat brain during a stretch" in
autopilot.md instead of a non-existent point 3, and its dead "Working with the owner" section is gone.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "docs" / "agents" / "research.md"
LOCAL_RESEARCH = ROOT / "docs" / "agents" / "local" / "research.md"
BRAIN_REFERENCE = ROOT / "docs" / "agents" / "brain-reference.md"
AUTOPILOT = ROOT / "docs" / "agents" / "autopilot.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Row 6: the lead-era merge sentence and the brain reading the wiki itself are gone.
def test_research_has_no_lead_era_merge_sentence():
    text = read(RESEARCH)
    assert "reviews the diff and merges" not in text
    assert "the main model reviews" not in text


def test_research_names_orch_as_merger_and_the_brain_reads_only_verdict_facts():
    text = read(RESEARCH)
    assert "ORCH merges it" in text
    assert "reads at source itself only a fact that decides a verdict" in text


def test_research_has_no_wiki_reading_by_the_main_model():
    text = read(RESEARCH)
    assert "read by the main model itself" not in text
    assert "The wiki" not in text
    assert "var/cache/wiki" not in text
    assert "wiki outranks" not in text
    assert "main model's browser" not in text


def test_research_has_no_main_model_word_left():
    assert "main model" not in read(RESEARCH)


# Row 7: the conservative-number practice is Poe-Junkroan's; the core keeps the general rule only.
def test_research_has_no_conservative_number_practice():
    text = read(RESEARCH)
    assert "conservative number" not in text
    assert "would move the number by X%" not in text
    assert "optimistic lane" not in text


def test_research_keeps_the_general_pending_rule():
    text = read(RESEARCH)
    assert "land as **pending** facts" in text
    assert "the owner approves" in text


def test_local_research_points_at_poe_junkroan_for_the_practice():
    text = read(LOCAL_RESEARCH)
    assert "Poe-Junkroan keeps" in text
    assert "conservative-number" in text
    assert "its own `docs/agents/local/research.md`" in text


# Row 8: the dead "point 3 of the autopilot's rule" becomes a real pointer.
def test_brain_reference_has_no_point_3_pointer():
    assert "point 3 of the autopilot" not in read(BRAIN_REFERENCE)


def test_brain_reference_points_at_the_chat_brain_during_a_stretch():
    text = read(BRAIN_REFERENCE)
    assert '`docs/agents/autopilot.md`, "The chat brain during a stretch"' in text


def test_the_pointed_heading_exists_in_autopilot():
    headings = [line.strip() for line in read(AUTOPILOT).splitlines() if line.startswith("## ")]
    assert "## The chat brain during a stretch" in headings


# Row 9: the dead "Working with the owner" section is gone.
def test_brain_reference_has_no_working_with_the_owner_section():
    text = read(BRAIN_REFERENCE)
    assert "Working with the owner" not in text
    assert "The owner's part of the work" not in text
    assert '"The lead" there is the brain' not in text
