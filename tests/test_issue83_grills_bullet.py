"""Issue #83: the Grills bullet of docs/agents/brain.md carries the decided-without-a-grill rule."""

from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1] / "docs" / "agents" / "brain.md"
SENTENCE = "A decision ticket none of whose questions is the owner's is decided by the brain without a grill"


def _grills_bullet() -> str:
    lines = BRAIN.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("- **Grills**"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("- **"))
    return "\n".join(lines[start:end])


def test_grills_bullet_holds_the_decided_without_grill_sentence():
    assert SENTENCE in _grills_bullet()
