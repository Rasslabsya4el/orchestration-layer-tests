"""#131: the brain reads at the source a fact that decides a verdict, in the section "Seeing the project" of docs/agents/brain.md.

#348 unit 3: the draft role dropped the second helper and the --check sentence on purpose (grill point 7); the
sentence that stands is the draft's own. The bullet count pinned the old 341-line role and was deleted (grill point 8).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs/agents/brain.md"
VERDICT_FACT = "a fact that decides a verdict is read at its source before it is used"


def _seeing_the_project() -> str:
    lines = BRAIN.read_text(encoding="utf8").splitlines()
    start = lines.index("## Seeing the project")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return " ".join(line.strip() for line in lines[start:end])


def test_verdict_fact_is_read_at_its_source_in_seeing_the_project():
    assert VERDICT_FACT in _seeing_the_project()
