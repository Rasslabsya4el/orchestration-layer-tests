"""Issue #104 unit 2 (wiring): the SubagentStop hook is registered for worker and worker-strong, in the home's
settings and in the shipped template, and both it and its test are core manifest lines.

Static reads only: the seams are `.claude/settings.json`, `config/core_templates/settings.hooks.json` and
`tools/agentops/core_manifest.txt`. The hook's own verdicts are tested in tests/test_issue104_subagent_skill_check.py.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"
TEMPLATE_PATH = PROJECT_ROOT / "config" / "core_templates" / "settings.hooks.json"
MANIFEST_PATH = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"
HOOK_REL = "tools/hooks/subagent_skill_check.py"
TEST_REL = "tests/test_issue104_subagent_skill_check.py"
MATCHER = "worker|worker-strong"
COMMAND = 'python "$CLAUDE_PROJECT_DIR/tools/hooks/subagent_skill_check.py"'


def _subagent_stop_entries(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]["SubagentStop"]


def test_settings_registers_the_check_for_worker_and_worker_strong():
    entries = _subagent_stop_entries(SETTINGS_PATH)
    hits = [e for e in entries if any(HOOK_REL in h["command"] for h in e["hooks"])]
    assert len(hits) == 1
    assert hits[0]["matcher"] == MATCHER
    assert hits[0]["hooks"][0]["type"] == "command"
    assert hits[0]["hooks"][0]["command"] == COMMAND
    assert hits[0]["hooks"][0]["timeout"] == 30


def test_template_registers_the_same_check():
    entries = _subagent_stop_entries(TEMPLATE_PATH)
    hits = [e for e in entries if any(HOOK_REL in h["command"] for h in e["hooks"])]
    assert len(hits) == 1
    assert hits[0]["matcher"] == MATCHER
    assert hits[0]["hooks"][0]["command"] == COMMAND
    assert hits[0]["hooks"][0]["timeout"] == 30


def test_hook_and_its_test_are_core_manifest_lines():
    manifest = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    assert HOOK_REL in manifest
    assert TEST_REL in manifest
    assert (PROJECT_ROOT / HOOK_REL).is_file()
    assert (PROJECT_ROOT / TEST_REL).is_file()
