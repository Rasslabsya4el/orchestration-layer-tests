"""#348 unit 1 (files in place): the
core manifest lists docs/agents/brain-reference.md, the brain row's model cell still parses, and the
rule file the hook injects at a chat start stays under the 10,000-character hook cap.

Seams (pre-agreed in #348, rule 6): the core
manifest file, tools/hooks/brain_reset_nudge._core_brain_row(), and the length of the head line plus
docs/agents/brain.md as the rules mode of the start hook would send it.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.hooks import brain_reset_nudge as nudge  # noqa: E402

BRAIN_MD = PROJECT_ROOT / "docs" / "agents" / "brain.md"
MANIFEST_PATH = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"

HEAD_LINE = ("This chat is the brain; its rule file docs/agents/brain.md follows, "
             "read from the working copy")
CAP = 10000
BRAIN_CELL = "Fable 5.1 high (fallback Opus 5.5 xhigh)"


def test_core_manifest_lists_brain_reference_md():
    lines = [line.strip() for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()]
    assert "docs/agents/brain-reference.md" in lines


def test_core_brain_row_model_cell_is_unchanged():
    row = nudge._core_brain_row()
    assert row is not None
    assert row[1] == BRAIN_CELL


def test_head_line_plus_repo_brain_md_is_under_the_hook_cap():
    text = BRAIN_MD.read_text(encoding="utf-8")
    assert len(HEAD_LINE + "\n" + text) <= CAP
