"""Issue #318: a pending owner decision is a question in the hand-off start line, never a blank.

Row 1: the nudge strings of ``tools/hooks/brain_reset_nudge.py`` (the "line" and the "ceiling"
verdicts) carry the "ask the owner:" form and no phrase that tells the owner to append an answer.
Row 2: ``docs/agents/brain.md`` carries the "ask the owner:" form; neither ``brain.md`` nor
``docs/agents/sessions.md`` tells the owner to append an answer to the start line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.hooks.brain_reset_nudge import _nudge_text  # noqa: E402

BRAIN_MD = PROJECT_ROOT / "docs" / "agents" / "brain.md"
SESSIONS_MD = PROJECT_ROOT / "docs" / "agents" / "sessions.md"

ASK_FORM = "ask the owner:"
#: a phrase that tells the owner to put an answer into the start line (the same words in every check)
FORBIDDEN = re.compile(
    r"append|after the line|допиш|\b(add|type|fill)\b[^.\n]{0,40}\b(answer|line|reply)\b",
    re.IGNORECASE,
)

LINE_CEILING = 100_000
CONTEXT_CEILING = 200_000


def _verdict_texts() -> dict[str, str]:
    """The nudge text for the "line" and the "ceiling" verdicts, with no live subagent or shell."""
    _, line_text = _nudge_text(150_000, LINE_CEILING, CONTEXT_CEILING, {})
    _, ceiling_text = _nudge_text(250_000, LINE_CEILING, CONTEXT_CEILING, {})
    return {"line": line_text, "ceiling": ceiling_text}


def test_nudge_verdicts_are_the_line_and_the_ceiling():
    kinds = {_nudge_text(150_000, LINE_CEILING, CONTEXT_CEILING, {})[0],
             _nudge_text(250_000, LINE_CEILING, CONTEXT_CEILING, {})[0]}
    assert kinds == {"line", "ceiling"}


def test_nudge_strings_carry_the_ask_the_owner_form():
    for verdict, text in _verdict_texts().items():
        assert ASK_FORM in text, f"{verdict} nudge lacks {ASK_FORM!r}"


def test_nudge_strings_tell_the_owner_to_append_nothing():
    hits = {verdict: FORBIDDEN.findall(text) for verdict, text in _verdict_texts().items()}
    assert all(not found for found in hits.values()), hits


def test_brain_md_carries_the_ask_the_owner_form():
    assert ASK_FORM in BRAIN_MD.read_text(encoding="utf-8")


def test_brain_and_sessions_docs_tell_the_owner_to_append_nothing():
    for path in (BRAIN_MD, SESSIONS_MD):
        found = FORBIDDEN.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} tells the owner to append/add/type an answer: {found}"
