"""Issue #159: reply-check point 1 tells the brain to put each ticket link on its own list line or table cell."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_reset_nudge.py"
REPLY_CHECK_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "brain_e2ac3d8.md"
SENTENCE = "никогда два и больше тикета в одном предложении"


def _hook():
    sys.path.insert(0, str(PROJECT_ROOT))
    spec = importlib.util.spec_from_file_location("brain_reset_nudge", HOOK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_per_line_sentence_present_once(monkeypatch):
    """Row 1: the sentence is in the block, exactly once."""
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(REPLY_CHECK_FIXTURE))
    block, why = _hook()._reply_check_block()
    assert why == ""
    assert block.count(SENTENCE) == 1


def test_block_under_the_hook_cap(monkeypatch):
    """Row 2: the hook's own size check passes (not 'too long') and the block stays within the cap."""
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(REPLY_CHECK_FIXTURE))
    hook = _hook()
    block, why = hook._reply_check_block()
    assert why == ""
    assert len(block) / 2.5 <= hook.REPLY_CHECK_MAX_TOKENS
