"""#89 rule 3: ORCH's protocol says it never waits inside its session for a core update."""
from pathlib import Path

ORCH_MD = Path(__file__).resolve().parent.parent / "docs" / "agents" / "orch.md"


def test_orch_protocol_says_it_never_waits_for_a_core_update_in_its_session():
    text = " ".join(ORCH_MD.read_text(encoding="utf-8").split())
    assert "never waits inside its session for a core update" in text
