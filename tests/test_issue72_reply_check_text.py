"""Issue #72, unit 1 (brain-gate-word): the reply-check text carries no word Poe's `_Avoid_` list rejects.

Poe's `tests/test_glossary_avoid.py` reads every `docs/agents/**/*.md` and fails on the word "gate"
(its GLOSSARY `_Avoid_` list). Two checks here, at the two seams the issue names:

(a) no file under `docs/agents/**/*.md` holds the whole word "gate" (case-insensitive);
(b) the reply-check block is still extracted by the nudge hook's function, non-empty, so the hook
    (`tools/hooks/brain_reset_nudge.py`, `_reply_check_block`) keeps injecting it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_AGENTS = PROJECT_ROOT / "docs" / "agents"
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_reset_nudge.py"
REPLY_CHECK_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "brain_e2ac3d8.md"
FORBIDDEN = re.compile(r"\bgate\b", re.IGNORECASE)


def test_no_agent_document_holds_the_word_gate():
    """(a) the same word Poe's `_Avoid_` check rejects: 0 hits in every docs/agents/**/*.md."""
    hits = []
    for path in sorted(DOCS_AGENTS.glob("**/*.md")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{number}")
    assert hits == [], f"the word 'gate' is in: {hits}"


def _load_hook_module():
    """Import the hook file by path (it is a script; its `main()` runs only under `__main__`)."""
    sys.path.insert(0, str(PROJECT_ROOT))
    spec = importlib.util.spec_from_file_location("brain_reset_nudge", HOOK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reply_check_block_is_still_extracted_by_the_hook(monkeypatch):
    """(b) the hook's own function reads the block between the markers of the reply-check fixture."""
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(REPLY_CHECK_FIXTURE))
    hook = _load_hook_module()
    block, why = hook._reply_check_block()
    assert why == ""
    assert block is not None and block.strip() != ""
