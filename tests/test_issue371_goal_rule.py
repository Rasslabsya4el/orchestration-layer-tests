"""Issue #371 unit 2: a ticket states a GOAL and a result checked on a whole class of cases.

Rows 3 and the field row of the issue table. Every check reads the rule files' text, so a regression names the
file or the phrase it found. The class of files is the set of agent-facing rule files that carry the ticket
rule (skill write-ticket, workflow, autopilot, core block, the roles, CLAUDE.md, the core templates).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "docs" / "agents"
SKILL = ROOT / ".claude" / "skills" / "write-ticket" / "SKILL.md"
POSTCODE = re.compile(r"\b\d{5}\b")
NOT_NAMED_MARKER = "не названн"

# Row 3: the old "numbers a test must reproduce" wording is gone from every file of the class.
RETIRED_PHRASES = ("numbers a test", "must reproduce")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _class_files() -> list[Path]:
    files = [
        SKILL,
        AGENTS / "workflow.md",
        AGENTS / "autopilot.md",
        AGENTS / "core-block.md",
        ROOT / "CLAUDE.md",
    ]
    files += sorted(p for p in AGENTS.glob("*.md") if p.is_file())
    files += sorted((ROOT / ".claude" / "agents").glob("*.md"))
    files += sorted(p for p in (ROOT / "config" / "core_templates").rglob("*") if p.is_file())
    return sorted(set(files))


def test_no_retired_reproduce_wording_in_class_files():
    # Row 3: the grep for the retired phrases over the class of files is 0 hits.
    files = _class_files()
    assert files, "the class of files is empty: the paths moved"
    hits = []
    for path in files:
        text = _text(path).lower()
        for phrase in RETIRED_PHRASES:
            if phrase in text:
                hits.append(f"{path.relative_to(ROOT)}: {phrase!r}")
    assert hits == [], "retired wording still present:\n" + "\n".join(hits)


def test_template_has_goal_field_and_no_why_field():
    # The human block's second field is Goal; the Why field is removed everywhere in the skill.
    text = _text(SKILL)
    template = text.split("## Template", 1)[1].split("## Rules", 1)[0]
    goal_lines = [line for line in template.splitlines() if line.startswith("- **Goal:**")]
    assert len(goal_lines) == 1, f"template must hold one Goal line, found {len(goal_lines)}"
    assert "**Why:**" not in text, "the Why field must be gone from the whole skill"


def test_example_result_names_a_case_outside_the_named_ones():
    # The example's Result checks a case the ticket does not name; its marker stem is «не названн».
    text = _text(SKILL)
    example = text.split("## Example", 1)[1]
    result_lines = [line for line in example.splitlines() if line.startswith("- **Result:**")]
    assert len(result_lines) == 1, f"example must hold one Result line, found {len(result_lines)}"
    assert NOT_NAMED_MARKER in result_lines[0], result_lines[0]


def test_example_agent_table_has_a_row_outside_the_named_postcodes():
    # The named postcodes are the digits of the Result line before the marker; the example's
    # For-the-agent table must hold a row for each of them and at least one row on a postcode outside them.
    text = _text(SKILL)
    example = text.split("## Example", 1)[1]
    result_line = next(line for line in example.splitlines() if line.startswith("- **Result:**"))
    named = set(POSTCODE.findall(result_line.split(NOT_NAMED_MARKER, 1)[0]))
    agent = example.split("## For the agent", 1)[1]
    table = set()
    for line in agent.splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and POSTCODE.fullmatch(cells[0]):
                table.add(cells[0])
    assert named, "the Result line names no postcode before the marker"
    assert named <= table, f"each named postcode needs a table row; missing {sorted(named - table)}"
    assert table - named, "the table needs a row on a postcode the ticket does not name"
