"""#3: a researcher-how that must write its one research file gets a place where the write passes.

Text test over the two files that carry the rule: ORCH's delegation rules (docs/agents/orch.md) and the
researcher-how role file (.claude/agents/researcher-how.md).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "docs" / "agents" / "orch.md"
RESEARCHER_HOW = ROOT / ".claude" / "agents" / "researcher-how.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_orch_names_worktree_line_for_researcher_how_research_file():
    text = _read(ORCH)
    # ORCH names the place in the prompt: the Worktree line, and the research file under its docs/research.
    assert "`Worktree: <path>`/docs/research" in text
    assert "researcher-how" in text
    assert "docs/research" in text


def test_researcher_how_writes_research_file_under_prompt_worktree():
    text = _read(RESEARCHER_HOW)
    # The file goes to the path ORCH names in the prompt, not to the checkout's own docs/research.
    assert "`Worktree: <path>`/docs/research" in text
    assert "names no worktree" in text
    # The old rule, which sent the write into the checkout's docs/research, is gone.
    assert "in the docs/research folder" not in text
