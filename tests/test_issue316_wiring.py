"""Issue #316 unit 2 (wiring): the SessionStart hook tools/hooks/brain_state_inject.py is registered for
startup, clear and compact, in the home's settings and in the shipped template, and the hook and its
unit-1 test are core manifest lines.

Static reads only: the seams are `.claude/settings.json`, `config/core_templates/settings.hooks.json` and
`tools/agentops/core_manifest.txt`. The hook's own verdicts are tested in tests/test_issue316_brain_state_inject.py.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"
TEMPLATE_PATH = PROJECT_ROOT / "config" / "core_templates" / "settings.hooks.json"
MANIFEST_PATH = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"
HOOK_REL = "tools/hooks/brain_state_inject.py"
TEST_REL = "tests/test_issue316_brain_state_inject.py"
COMMAND = 'python "$CLAUDE_PROJECT_DIR/tools/hooks/brain_state_inject.py"'
SOURCES = {"startup", "clear", "compact"}


def _session_start_hits(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    return [e for e in entries if any(HOOK_REL in h["command"] for h in e["hooks"])]


def _assert_registered(path: Path) -> None:
    hits = _session_start_hits(path)
    assert len(hits) == 1
    entry = hits[0]
    assert SOURCES <= set(entry["matcher"].split("|"))
    assert len(entry["hooks"]) == 1
    hook = entry["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"] == COMMAND
    assert hook["timeout"] == 15


def test_settings_registers_the_hook_for_startup_clear_and_compact():
    _assert_registered(SETTINGS_PATH)


def test_template_registers_the_same_hook():
    _assert_registered(TEMPLATE_PATH)


def test_hook_and_its_test_are_core_manifest_lines():
    manifest = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    assert HOOK_REL in manifest
    assert TEST_REL in manifest
    # the hook sits next to brain_reset_nudge.py in the manifest's sorted tools/hooks block
    assert manifest.index(HOOK_REL) == manifest.index("tools/hooks/brain_reset_nudge.py") + 1
