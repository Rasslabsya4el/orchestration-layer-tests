"""#130: the subagent role `researcher-survey` (.claude/agents/researcher-survey.md).

It reads a named list of sources and writes one table file; it hands back past 100k context. The frontmatter is
parsed by hand (a `---` block of `key: value` lines), so the test needs no YAML package.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROLE_FILE = PROJECT_ROOT / ".claude" / "agents" / "researcher-survey.md"
MANIFEST = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"


def _split(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    assert lines[0] == "---", "role file must open with a --- frontmatter block"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1 :])
    return fields, body


def _role() -> tuple[dict[str, str], str]:
    return _split(ROLE_FILE.read_text(encoding="utf-8"))


def test_frontmatter_name_model_effort_and_tools():
    fields, _ = _role()
    assert fields["name"] == "researcher-survey"
    assert fields["model"] == "claude-sonnet-5-5"
    assert fields["effort"] == "medium"
    assert fields["tools"] == "Read, Grep, Glob, Bash, Write, Skill"
    tools = [t.strip() for t in fields["tools"].split(",")]
    for forbidden in ("Agent", "Edit", "MultiEdit", "NotebookEdit"):
        assert forbidden not in tools, forbidden
    assert not any(t.startswith("mcp__") for t in tools)


def test_frontmatter_description_is_one_line_and_names_the_role():
    fields, _ = _role()
    description = fields["description"]
    assert description, "description is missing"
    assert "Internal survey researcher on Sonnet 5.5" in description
    assert "100k" in description


def test_body_carries_the_hand_back_and_the_context_line():
    _, body = _role()
    assert "Context hand-back" in body
    assert "100k" in body


def test_role_is_listed_in_the_core_manifest_after_researcher_how():
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    rel = ".claude/agents/researcher-survey.md"
    assert rel in lines
    assert lines.index(rel) == lines.index(".claude/agents/researcher-how.md") + 1
    assert lines.index(rel) == lines.index(".claude/agents/reviewer-spec.md") - 1
