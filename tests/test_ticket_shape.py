import pytest

from tools.agentops import ticket_shape
from tools.agentops.ticket_shape import check, main

TITLE = "Flasks are active only at 100 % Flask uptime in the heavy tool"
HUMAN = """## For the human
- **What:** in day-1 builds every flask is switched on, even when the heavy tool gives it 0 % uptime, so DPS is overstated.
- **Goal:** a flask is active only when the heavy tool prints "Flask uptime: 100%", for every build the owner runs.
- **Plan:** the `active` flag is no longer set by hand: it comes from the same calculation the heavy tool shows.
- **Result:** on stormbrand-s4 all 5 flasks switch off, FullDPS 1,753,896 -> 1,552,665; shown by a test.
- **Undo:** two-way door (one revert); touches every build that wears flasks.
"""
AGENT = "\n## For the agent\nSpec lives in `src/x.py:12`.\n"
BODY = HUMAN + AGENT
FIELDS = ["What", "Goal", "Plan", "Result", "Undo"]


def with_human(human):
    return human + AGENT


def test_flask_example_ok():
    assert check(TITLE, BODY) == []


def test_no_human_heading():
    assert check(None, BODY.replace("## For the human\n", ""))


@pytest.mark.parametrize("name", FIELDS)
def test_missing_field(name):
    body = "\n".join(l for l in BODY.splitlines() if not l.startswith(f"- **{name}:**"))
    probs = check(None, body)
    assert len(probs) == 1 and name in probs[0]


def test_empty_field():
    body = BODY.replace("- **Plan:** the `active` flag is no longer set by hand: it comes from the same calculation the heavy tool shows.", "- **Plan:**")
    probs = check(None, body)
    assert len(probs) == 1 and "Plan" in probs[0]


def test_swapped_fields():
    lines = HUMAN.splitlines()
    lines[2], lines[3] = lines[3], lines[2]
    probs = check(None, with_human("\n".join(lines) + "\n"))
    assert probs and any("order" in p for p in probs)


def test_continuation_line_counts():
    body = BODY.replace("- **Plan:** the", "- **Plan:**\n  the")
    assert check(None, body) == []


def test_no_agent_heading():
    assert check(None, HUMAN)


def test_title_81_chars():
    assert check("a" * 80, None) == []
    assert check("a" * 81, None)


def test_title_issue_number():
    probs = check("Fix #12 now", None)
    assert probs and "#12" in probs[0]


def test_title_empty():
    assert check("  ", None)


@pytest.mark.parametrize("token", ["daemon.py", "foo.py:12", "GLOSSARY.md"])
def test_human_block_tokens(token):
    body = BODY.replace("touches every build", f"touches {token} and every build")
    probs = check(None, body)
    assert probs and any(token.split(":")[0] in p for p in probs)
    if ":" in token:
        assert any("foo.py:12" in p for p in probs)


def test_clock_and_percent_allowed():
    body = BODY.replace("touches every build", "touches 10:00 runs and every build")
    assert check(None, body) == []
    assert check(None, BODY) == []  # "Flask uptime: 100%" already in Goal


def test_halves_skipped():
    assert check(None, BODY) == []
    assert check(TITLE, None) == []


def test_cli(tmp_path, capsys):
    good = tmp_path / "good.md"
    good.write_text(BODY, encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("nothing", encoding="utf-8")
    assert main(["--title", TITLE, "--body-file", str(good)]) == 0
    assert main(["--body-file", str(bad)]) == 1
    assert "## For the human" in capsys.readouterr().out


def test_leading_parked_lines_pass():
    # #1074: `Parked: <event>` lines (and blank lines) may stand above the human block.
    body = "Parked: until the day-1 prototype is accepted\n\nParked: second line\n" + BODY
    assert check(TITLE, body) == []


def test_other_text_before_human_block_fails():
    # #1074: nothing but `Parked:` lines and blank lines may precede `## For the human`.
    for pre in ("Some preface\n", "Parked: x\nSome preface\n", "- **Note:** x\n", "# Title\n"):
        probs = check(TITLE, pre + BODY)
        assert probs and "## For the human" in probs[0], pre


# #371: the second field is `Goal`; an older body with `Why` passes on edit only.
OLD_WHY_BODY = BODY.replace("- **Goal:**", "- **Why:**")


def test_create_with_goal_accepted():
    assert check(None, BODY, create=True) == []


def test_create_refuses_why_without_goal():
    probs = check(None, OLD_WHY_BODY, create=True)
    assert len(probs) == 1 and "Goal" in probs[0] and "Why" in probs[0]


def test_create_is_the_default():
    probs = check(None, OLD_WHY_BODY)
    assert len(probs) == 1 and "Goal" in probs[0]


def test_edit_accepts_old_why_body():
    assert check(None, OLD_WHY_BODY, create=False) == []


def test_edit_accepts_goal_body():
    assert check(None, BODY, create=False) == []


def test_edit_still_refuses_missing_second_field():
    body = "\n".join(l for l in BODY.splitlines() if not l.startswith("- **Goal:**"))
    probs = check(None, body, create=False)
    assert len(probs) == 1 and "Goal" in probs[0]


def test_cli_edit_flag(tmp_path, capsys):
    old = tmp_path / "old.md"
    old.write_text(OLD_WHY_BODY, encoding="utf-8")
    assert main(["--edit", "--body-file", str(old)]) == 0
    assert main(["--body-file", str(old)]) == 1
    assert "Goal" in capsys.readouterr().out
