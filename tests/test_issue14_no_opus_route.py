"""#14: no rule sends a delegation to an Opus subagent (`effort-*`)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE_FILES = sorted(p for p in (ROOT / "docs/agents").glob("*.md")) + [ROOT / "CLAUDE.md"]
EFFORT_DESCRIPTION = "Effort exists only in an agent definition"
OPUS_OK = ("rollback Opus", "fallback Opus", "never Opus", "never sent to Opus", "claude-opus-5-5")


def _hits(word):
    for path in RULE_FILES:
        for n, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
            if word in line.lower():
                yield path.name, n, line.strip()


def test_effort_agents_are_only_described_never_named_as_a_target():
    found = [h for h in _hits("effort-") if not h[2].startswith(EFFORT_DESCRIPTION)]
    assert found == []


def test_opus_appears_only_as_a_model_of_a_role_or_as_a_refusal():
    found = [h for h in _hits("opus")
             if h[0] in ("orch.md", "brain.md") and not any(ok.lower() in h[2].lower() for ok in OPUS_OK)]
    assert found == []


def test_the_replacing_routes_exist():
    orch = (ROOT / "docs/agents/orch.md").read_text(encoding="utf8")
    brain = (ROOT / "docs/agents/brain.md").read_text(encoding="utf8")
    assert "`researcher-how` each" in orch and "`researcher-how` each" in brain
    assert "never sent to Opus" in orch and "never sent to Opus" in brain
    assert "the check of a verdict-changing claim" in orch and "`worker-strong`" in orch
