"""#366 unit 1: the brain's context nudge texts point at the rule that lives now.

Rows (the issue's acceptance rows 1-4, scoped to this unit):
1. ``_nudge_text`` over the ceiling with no live work: kind ``ceiling``, the verbatim text of rule 1, no dead pointer.
2. ``_nudge_text`` between the line and the ceiling: kind ``line``, no ``If you are the brain``, the new pointer.
3. ``_nudge_text`` over the ceiling with live work: kind ``wait``, ending with the owner's-message sentence.
4. the three dead pointer forms appear nowhere in ``tools/hooks/`` and ``tools/agentops/*.py``.
   (``docs/agents/`` is scanned after the merge by ORCH; unit 2 fixes those files in parallel.)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hooks.brain_reset_nudge import _nudge_text  # noqa: E402

LINE = 150_000
CEILING = 200_000

#: the three dead pointer forms (issue #366, acceptance row 4), as literal substrings
DEAD_POINTERS = (
    'brain.md "Reset"',
    'brain.md` "Reset"',
    "brain.md` (Reset)",
)

#: rule 1 of #366, verbatim, with {k} = 250 and {ceiling_k} = 200
CEILING_TEXT = (
    "Context is 250k, over the context ceiling (200k). Answer the owner's message and think it through in full, "
    "as at any size; start no new work, and write the hand-off (docs/agents/brain-reference.md, "
    '"The clear and the hand-off"), ending with one ```text copy block that holds only the next chat\'s start line. '
    "What the next chat must do first goes into that same line as its arguments, never a separate sentence in prose. "
    "A pending owner decision goes in as `ask the owner: <decision>`, never as a blank for the owner to fill in. "
    "Never clear yourself."
)


def test_ceiling_text_is_rule_one_verbatim_and_has_no_dead_pointer():
    kind, text = _nudge_text(250_000, LINE, CEILING, {})
    assert kind == "ceiling"
    assert text == CEILING_TEXT
    assert "think it through in full" in text
    assert "start no new work" in text
    assert "brain-reference.md" in text
    assert "ask the owner:" in text
    assert "do nothing but" not in text
    assert 'brain.md "Reset"' not in text


def test_line_text_drops_the_brain_prefix_and_points_at_the_hand_off_section():
    kind, text = _nudge_text(160_000, LINE, CEILING, {})
    assert kind == "line"
    assert "If you are the brain" not in text
    assert '(docs/agents/brain-reference.md, "The clear and the hand-off")' in text
    assert 'brain.md "Reset"' not in text


def test_wait_text_over_the_ceiling_ends_with_the_owner_sentence():
    kind, text = _nudge_text(250_000, LINE, CEILING,
                             {"subagent": 1, "shell": 0, "monitor": 0})
    assert kind == "wait"
    assert text.endswith("Start nothing new; the owner's message is still answered in full.")


def test_no_dead_hand_off_pointer_in_hooks_or_agentops():
    scanned = sorted((ROOT / "tools" / "hooks").glob("*.py")) + sorted((ROOT / "tools" / "agentops").glob("*.py"))
    assert scanned, "no hook or agentops files found to scan"
    hits = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for pointer in DEAD_POINTERS:
                if pointer in line:
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {pointer}")
    assert hits == []
