"""Issue #369 unit 3 (rows 10 and 11): the autopilot review has a third section, its three places point at it,
and the fold step needs no DELETE (the permission guard's red line 4 refuses `gh api --method DELETE`).

The guard's verdict is read by calling its pure ``decide`` on the doc's own call text; no gh call is run.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "agents" / "autopilot.md"
GUARD = ROOT / "tools" / "hooks" / "permission_guard.py"
SECTION = "Questions for the owner"
POINTER = f'docs/agents/autopilot.md "{SECTION}"'


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _block(text: str, start: str, end: str | None = None) -> str:
    begin = text.index(start)
    stop = text.index(end, begin + 1) if end else len(text)
    return text[begin:stop]


def _decide(command: str) -> str | None:
    spec = importlib.util.spec_from_file_location("permission_guard", GUARD)
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ROOT)})


def test_review_defines_a_third_section():
    review = _block(_text(), "## The review on return")
    items = re.findall(r"^(\d+)\. \*\*(.+?)\*\*", review, re.MULTILINE)
    assert [number for number, _ in items] == ["1", "2", "3"], items
    assert items[2][1].startswith(SECTION), items[2]
    assert "stop reason" in items[2][1] and "out-of-scope" in items[2][1], items[2]


def test_every_review_mention_in_the_stop_rule_points_at_the_section():
    stop = " ".join(_block(_text(), "## The stop rule", "## The chat brain during a stretch").split())
    mentions = re.findall(r"\breview\b", stop)
    paired = re.findall(r"\breview\b.{0,120}?" + re.escape(POINTER), stop)
    assert len(mentions) >= 3, mentions
    assert len(paired) == len(mentions), (len(paired), len(mentions), stop)


def test_fold_holds_no_delete():
    text = _text()
    assert "--method DELETE" not in text
    fold = _block(text, "**Folding a thread", "The dispatcher stops a session")
    call = re.search(r"gh api --method PATCH [^`\n]+", fold)
    assert call, "the fold names a PATCH call"
    assert "-f body='Folded: see <log path>'" in call.group(0), call.group(0)
    assert _decide(call.group(0)) is None, _decide(call.group(0))


def test_guard_refuses_the_old_delete_call():
    reason = _decide("gh api --method DELETE repos/<owner>/<repo>/issues/comments/<id>")
    assert reason is not None and reason.startswith("permission guard, red line 4:"), reason
