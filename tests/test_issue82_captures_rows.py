"""Issue #82: the grill row of the brain's "Capturing as it goes" table keeps every round verbatim."""
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1] / "docs" / "agents" / "brain.md"
HEADER = "| said | written to |"


def _captures_table() -> str:
    lines = BRAIN.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "Capturing as it goes" in line)
    header = next(i for i in range(start, len(lines)) if lines[i].strip() == HEADER)
    end = header
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    return "\n".join(lines[header:end])


def test_grill_row_keeps_verbatim_round_by_round():
    assert "every question and answer verbatim, round by round" in _captures_table()
