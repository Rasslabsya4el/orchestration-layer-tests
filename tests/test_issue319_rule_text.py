"""#319: the rule 'Red before the ticket' is written into the core text.

The rule text is the product here, so the test reads the two docs and checks
that the rule's named parts are present and that the bullet stays short.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "docs" / "agents" / "orch.md"
WORKFLOW = ROOT / "docs" / "agents" / "workflow.md"

BULLET_HEAD = "- **Red before the ticket.**"


def _orch_bullet_lines() -> list[str]:
    lines = ORCH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(BULLET_HEAD))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("- ") or line.startswith("**") or line.strip() == "":
            break
        block.append(line)
    return block


def test_orch_names_the_red_on_base_command_and_marker_file():
    text = ORCH.read_text(encoding="utf-8")
    assert "red-on-base" in text
    assert "Tests red on main before" in text
    assert "tests/red_on_main.txt" in text


def test_orch_bullet_carries_the_exit_exempt_prequeue_and_core_rules():
    text = ORCH.read_text(encoding="utf-8")
    assert "exit 3" in text
    assert "exempt" in text
    assert "prequeue run per test" in text
    assert "core file" in text


def test_workflow_points_at_the_red_before_the_ticket_rule():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Red before the ticket" in text


def test_red_before_the_ticket_bullet_is_at_most_ten_lines():
    # an ORCH bullet stays short: the cap is ten physical lines, head included
    bullet = _orch_bullet_lines()
    assert 1 <= len(bullet) <= 10, f"bullet is {len(bullet)} lines"
