"""#64 / #65: the brain's reply check lives once, between the hook's markers in docs/agents/brain.md, with #64's two
lines as points 4 and 5; the autopilot's role file (.claude/agents/brain.md) holds none of it."""
from pathlib import Path

import pytest

from tools.hooks import brain_reset_nudge as hook_module
from tools.hooks.brain_reset_nudge import REPLY_CHECK_MAX_TOKENS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "agents" / "brain.md"
ROLE = ROOT / ".claude" / "agents" / "brain.md"
START, END = "<!-- reply-check:start -->", "<!-- reply-check:end -->"
INTRO = "- Перед каждым ответом владельцу перечитай его глазами человека"
LINE_ANSWER = ("  4. Ответ первым: первая строка — прямой ответ на то, о чём он спросил, или вывод "
               "(сделано или нет, что решено); таблицы и подробности ниже.")
LINE_WHY = "  5. Предлагаешь список или изменение — до списка одной фразой зачем."


@pytest.fixture(autouse=True)
def _reply_check_switch_on(monkeypatch):
    """Issue #347: the reply check's switch is off by default; this file's checks run with it on."""
    monkeypatch.setattr(hook_module, "REPLY_CHECK_ENABLED", True, raising=True)


def _block() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1
    return text.split(START)[1].split(END)[0].strip()


def test_64_row1_both_lines_once_and_numbered_after_the_8_points():
    text = DOC.read_text(encoding="utf-8")
    assert text.count(LINE_ANSWER) == 1 and text.count(LINE_WHY) == 1
    assert text.index("  3. Спрашивай только") < text.index(LINE_ANSWER) < text.index(LINE_WHY)


def test_64_row2_no_other_rule_file_restates_the_lines():
    hits = []
    for folder in (ROOT / ".claude", ROOT / "docs"):
        for p in folder.rglob("*.md"):
            rel = p.relative_to(ROOT).parts
            if "worktrees" in rel or rel[:2] == ("docs", "research"):  # research quotes the proposal
                continue
            if "Ответ первым" in p.read_text(encoding="utf-8", errors="replace"):
                hits.append(p.relative_to(ROOT).as_posix())
    if "Ответ первым" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8"):
        hits.append("CLAUDE.md")
    assert hits == ["docs/agents/brain.md"]


def test_65_row5_the_role_file_holds_no_line_of_the_check_the_doc_holds_it_once():
    assert ROLE.read_text(encoding="utf-8").count(INTRO) == 0
    assert DOC.read_text(encoding="utf-8").count(INTRO) == 1


def test_65_markers_wrap_all_five_points_and_the_block_fits_the_cap():
    block = _block()
    assert block.startswith(INTRO)
    for n in "12345":
        assert f"  {n}. " in block
    assert len(block) / 2.5 <= REPLY_CHECK_MAX_TOKENS
