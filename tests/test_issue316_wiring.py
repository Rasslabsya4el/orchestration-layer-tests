"""Issue #316 wiring: the brain-state hook tools/hooks/brain_state_inject.py is registered for
SessionStart (startup, clear, compact), for UserPromptSubmit and for PreToolUse (both without a
matcher), in the home's settings and in the shipped template; the hook and its unit-1 test are core
manifest lines.

Static reads only: the seams are `.claude/settings.json`, `config/core_templates/settings.hooks.json`
and `tools/agentops/core_manifest.txt`. The hook's own verdicts are tested in
tests/test_issue316_brain_state_inject.py.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"
TEMPLATE_PATH = PROJECT_ROOT / "config" / "core_templates" / "settings.hooks.json"
MANIFEST_PATH = PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt"
HOOK_REL = "tools/hooks/brain_state_inject.py"
RESET_NUDGE_REL = "tools/hooks/brain_reset_nudge.py"
TEST_REL = "tests/test_issue316_brain_state_inject.py"
COMMAND = 'python "$CLAUDE_PROJECT_DIR/tools/hooks/brain_state_inject.py"'
RULES_COMMAND = COMMAND + " rules"  # #348: the second mode, registered next to the hand-off command
SOURCES = {"startup", "clear", "compact"}


def _entries(path: Path, event: str) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["hooks"][event]


def _carries_hook(entry: dict) -> bool:
    return any(HOOK_REL in h["command"] for h in entry["hooks"])


def _hits(path: Path, event: str) -> list[dict]:
    return [e for e in _entries(path, event) if _carries_hook(e)]


def _assert_hook_entry(entry: dict) -> None:
    assert [h["command"] for h in entry["hooks"]] == [COMMAND, RULES_COMMAND]
    for hook in entry["hooks"]:
        assert hook["type"] == "command"
        assert hook["timeout"] == 15


def _assert_registered(path: Path) -> None:
    hits = _hits(path, "SessionStart")
    assert len(hits) == 1
    entry = hits[0]
    assert SOURCES <= set(entry["matcher"].split("|"))
    _assert_hook_entry(entry)


def _assert_registered_without_matcher(path: Path, event: str) -> None:
    hits = _hits(path, event)
    assert len(hits) == 1
    entry = hits[0]
    assert "matcher" not in entry
    _assert_hook_entry(entry)


def _assert_user_prompt_submit_before_reset_nudge(path: Path) -> None:
    entries = _entries(path, "UserPromptSubmit")
    hook_index = next(i for i, e in enumerate(entries) if _carries_hook(e))
    nudge_index = next(
        i for i, e in enumerate(entries) if any(RESET_NUDGE_REL in h["command"] for h in e["hooks"])
    )
    assert hook_index < nudge_index


def _assert_pre_tool_use_last(path: Path) -> None:
    entries = _entries(path, "PreToolUse")
    assert _carries_hook(entries[-1])


def test_settings_registers_the_hook_for_startup_clear_and_compact():
    _assert_registered(SETTINGS_PATH)


def test_template_registers_the_same_hook():
    _assert_registered(TEMPLATE_PATH)


def test_settings_registers_the_hook_on_user_prompt_submit_without_matcher():
    _assert_registered_without_matcher(SETTINGS_PATH, "UserPromptSubmit")


def test_template_registers_the_hook_on_user_prompt_submit_without_matcher():
    _assert_registered_without_matcher(TEMPLATE_PATH, "UserPromptSubmit")


def test_settings_user_prompt_submit_hook_comes_before_the_reset_nudge():
    _assert_user_prompt_submit_before_reset_nudge(SETTINGS_PATH)


def test_template_user_prompt_submit_hook_comes_before_the_reset_nudge():
    _assert_user_prompt_submit_before_reset_nudge(TEMPLATE_PATH)


def test_settings_registers_the_hook_on_pre_tool_use_without_matcher():
    _assert_registered_without_matcher(SETTINGS_PATH, "PreToolUse")


def test_template_registers_the_hook_on_pre_tool_use_without_matcher():
    _assert_registered_without_matcher(TEMPLATE_PATH, "PreToolUse")


def test_settings_pre_tool_use_hook_is_the_last_entry():
    _assert_pre_tool_use_last(SETTINGS_PATH)


def test_template_pre_tool_use_hook_is_the_last_entry():
    _assert_pre_tool_use_last(TEMPLATE_PATH)


def test_hook_and_its_test_are_core_manifest_lines():
    manifest = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    assert HOOK_REL in manifest
    assert TEST_REL in manifest
    # the hook sits next to brain_reset_nudge.py in the manifest's sorted tools/hooks block
    assert manifest.index(HOOK_REL) == manifest.index(RESET_NUDGE_REL) + 1
