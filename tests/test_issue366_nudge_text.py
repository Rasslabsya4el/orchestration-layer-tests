"""#366 unit 1 and #368 unit A: the brain's context nudge texts point at the rule that lives now.

Rows (the issue's acceptance rows 1-4, scoped to this unit):
1. ``_nudge_text`` over the ceiling with no live work: kind ``ceiling``, the verbatim text of rule 1, no dead pointer.
2. ``_nudge_text`` between the line and the ceiling: kind ``line``, no ``If you are the brain``, the new pointer.
3. ``_nudge_text`` over the ceiling with live work: kind ``wait``, ending with the owner's-message sentence.
4. the three dead pointer forms appear nowhere in ``tools/hooks/`` and ``tools/agentops/*.py``.
   (``docs/agents/`` is scanned after the merge by ORCH; unit 2 fixes those files in parallel.)

#368 unit A (the five texts a hook gives the brain; none tells how a reply opens or that a reply is the hand-off):
5. the five texts (line, wait between line and ceiling, wait over the ceiling, ceiling, wayfinder-close,
   first-prompt) lack "opens with", "every reply", "the reply is the hand-off", "Read docs/agents/brain.md", "end with".
6. the ceiling text carries the pointer to docs/agents/brain-reference.md, "The clear and the hand-off".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hooks import brain_reset_nudge as hook  # noqa: E402
from tools.hooks.brain_reset_nudge import _brain_title_context, _nudge_text  # noqa: E402

LINE = 150_000
CEILING = 200_000

#: the three dead pointer forms (issue #366, acceptance row 4), as literal substrings
DEAD_POINTERS = (
    'brain.md "Reset"',
    'brain.md` "Reset"',
    "brain.md` (Reset)",
)

#: phrases that tell how a reply opens or that a reply is a hand-off (#368 rule 1), plus the old first-prompt sentence
FORBIDDEN_IN_HOOK_TEXT = (
    "opens with",
    "every reply",
    "the reply is the hand-off",
    "Read docs/agents/brain.md",
    "end with",
)

POINTER = '(docs/agents/brain-reference.md, "The clear and the hand-off")'

#: rule 1 of #366, with {k} = 250 and {ceiling_k} = 200; #368 unit A replaces the 9 lines of hand-off format by POINTER
CEILING_TEXT = (
    "Context is 250k, over the context ceiling (200k). Answer the owner's message and think it through in full, "
    "as at any size; start no new work. Write the hand-off " + POINTER + ". Never clear yourself."
)

#: the five texts of #368 unit A, by name
FIVE_NAMES = ("line", "wait", "wait-over-ceiling", "ceiling", "wayfinder-close", "first-prompt")


def _five_texts(tmp_path: Path, monkeypatch) -> dict[str, str]:
    """One text per branch the hooks give the brain for #368 unit A (no live work unless named)."""
    texts = {
        "line": _nudge_text(160_000, LINE, CEILING, {})[1],
        "wait": _nudge_text(160_000, LINE, CEILING, {"subagent": 1, "shell": 0, "monitor": 0})[1],
        "wait-over-ceiling": _nudge_text(250_000, LINE, CEILING, {"subagent": 1, "shell": 0, "monitor": 0})[1],
        "ceiling": _nudge_text(250_000, LINE, CEILING, {})[1],
    }
    monkeypatch.setattr(hook, "_skip_reason", lambda rule, payload: None)
    monkeypatch.setattr(hook, "_gh_labels", lambda n, timeout=5: ["wayfinder:map"])
    monkeypatch.setattr(hook, "_log", lambda *args, **kwargs: None)
    closed = hook.handle_post_tool_use({"hook_event_name": "PostToolUse",
                                        "tool_response": {"stdout": "✓ Closed issue o/n#5"}})
    texts["wayfinder-close"] = closed["hookSpecificOutput"]["additionalContext"]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type": "user", "message": {"content": "hi"}}\n'
        '{"type": "custom-title", "customTitle": "%s", "sessionId": "s1"}\n' % hook.BRAIN_CHAT_TITLE,
        encoding="utf-8",
    )
    texts["first-prompt"] = _brain_title_context(str(transcript))
    return texts


def test_ceiling_text_is_rule_one_verbatim_and_has_no_dead_pointer():
    kind, text = _nudge_text(250_000, LINE, CEILING, {})
    assert kind == "ceiling"
    assert text == CEILING_TEXT
    assert "think it through in full" in text
    assert "start no new work" in text
    assert "brain-reference.md" in text
    assert "do nothing but" not in text
    assert 'brain.md "Reset"' not in text


def test_ceiling_text_points_at_the_clear_and_the_hand_off_section():
    text = _nudge_text(250_000, LINE, CEILING, {})[1]
    assert POINTER in text


def test_line_text_drops_the_brain_prefix_and_points_at_the_hand_off_section():
    kind, text = _nudge_text(160_000, LINE, CEILING, {})
    assert kind == "line"
    assert "If you are the brain" not in text
    assert POINTER in text
    assert 'brain.md "Reset"' not in text


def test_line_text_says_the_clear_is_due_and_no_long_work_starts():
    text = _nudge_text(160_000, LINE, CEILING, {})[1].lower()
    assert "over the context line" in text
    assert "clear is due at the next clean point" in text
    assert "start no long new work" in text
    assert "answer the owner in full" in text


def test_wait_text_over_the_ceiling_ends_with_the_owner_sentence():
    kind, text = _nudge_text(250_000, LINE, CEILING,
                             {"subagent": 1, "shell": 0, "monitor": 0})
    assert kind == "wait"
    assert text.endswith("Start nothing new; the owner's message is still answered in full.")


@pytest.mark.parametrize("name", FIVE_NAMES)
def test_hook_text_has_no_reply_opening_or_hand_off_instruction(tmp_path, monkeypatch, name):
    text = _five_texts(tmp_path, monkeypatch)[name]
    for phrase in FORBIDDEN_IN_HOOK_TEXT:
        assert phrase not in text, f"{name} text still says {phrase!r}: {text}"


def test_wayfinder_close_text_is_a_reminder_that_a_clear_is_due(tmp_path, monkeypatch):
    text = _five_texts(tmp_path, monkeypatch)["wayfinder-close"]
    assert text.startswith("A wayfinder ticket closed (#5)")
    assert "a clear is due at the next clean point" in text


def test_first_prompt_text_keeps_the_brain_title_and_drops_the_brain_md_read(tmp_path, monkeypatch):
    text = _five_texts(tmp_path, monkeypatch)["first-prompt"]
    assert "you are the brain" in text
    assert "docs/agents/brain.md" not in text


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
