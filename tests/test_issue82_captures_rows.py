"""Issue #82: the two rewritten rows of the brain's "Captures as it goes" table.

The owner's standing words are written as the owner said them, and a grill continues
from its "Settled so far" list, not from the previous round's artifact.
"""
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1] / "docs" / "agents" / "brain.md"
HEADER = "| said | written to |"
PREFERENCE = "a reading of the brain's is marked as its own and never narrows his words"
GRILL = "builds from that list, not from the previous round's artifact"


def _captures_table() -> str:
    lines = BRAIN.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "Captures as it goes" in line)
    header = next(i for i in range(start, len(lines)) if lines[i].strip() == HEADER)
    end = header
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    return "\n".join(lines[header:end])


def test_preference_row_holds_the_reading_sentence():
    assert PREFERENCE in _captures_table()


def test_grill_row_builds_from_the_settled_list():
    assert GRILL in _captures_table()


def test_grill_row_keeps_verbatim_round_by_round():
    assert "every question and answer verbatim, written round by round" in _captures_table()
