"""#209 row 2: the role file .claude/agents/unblocker.md exists, pins Fable 5.1 at high effort,
is listed in the core manifest in its sorted slot, and its body carries the `<!-- brain-answer -->` marker
the unblocker writes, tells it to unassign itself, and forbids it to write code.

The frontmatter is read line by line (`key: value` between the two `---` fences), the same way the other
role-file tests read theirs; no YAML dependency. The helper is a copy: tests/test_issue130_researcher_survey.py
keeps its own `_split` bound to researcher-survey.md, so it is not importable as a shared helper."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROLE_FILE = PROJECT_ROOT / ".claude" / "agents" / "unblocker.md"
MANIFEST = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"


def _frontmatter_and_body(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    assert lines[0] == "---", "the role file opens with a frontmatter fence"
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, "\n".join(lines[end + 1 :])


def test_role_file_exists():
    assert ROLE_FILE.is_file(), ".claude/agents/unblocker.md is missing"


def test_frontmatter_pins_fable_model_and_high_effort():
    fields, _ = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert fields.get("name") == "unblocker"
    assert fields.get("model") == "claude-fable-5-1"
    assert fields.get("effort") == "high"


def test_frontmatter_tools_build_nothing():
    fields, _ = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    tools = [t.strip() for t in fields.get("tools", "").split(",")]
    assert tools == ["Bash", "Read", "Grep", "Glob"]


def test_role_is_listed_in_the_core_manifest_in_its_sorted_slot():
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    rel = ".claude/agents/unblocker.md"
    assert rel in lines
    assert lines.index(rel) == lines.index(".claude/agents/reviewer-standards.md") + 1
    assert lines.index(rel) == lines.index(".claude/agents/worker-strong.md") - 1


def test_body_carries_the_brain_answer_marker():
    _, body = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert "<!-- brain-answer -->" in body


def test_body_tells_the_unblocker_to_unassign_itself():
    _, body = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert "--remove-assignee @me" in body
    assert "unassign" in body.lower()


def test_body_forbids_writing_code():
    _, body = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert "no code" in body


def test_body_writes_the_loop_stop_as_first_section_and_next_steps_for_orch():
    _, body = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert "Loop stop" in body
    assert "next steps" in body
    assert body.index("Loop stop") < body.index("next steps")


def test_asks_owner_condition_is_exclusive():
    _, body = _frontmatter_and_body(ROLE_FILE.read_text(encoding="utf-8"))
    assert "only when" in body
