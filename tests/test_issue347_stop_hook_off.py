"""#347 unit 2: the state-claim Stop hook is off in the home's settings and in the shipped template, and docs/agents/brain.md
says how to restore it.

Seams: `.claude/settings.json` (its `Stop` hook list, read as JSON), `docs/agents/brain.md` (read as text: the restore
line outside the marked reply-check block, and the marked block itself, compared with its version at e2ac3d8, the
reference the owner named), and the wiring check of `tests/test_issue320_every_rule_names_a_role.py` run on a copy of
the settings with the Stop entry put back. The hook file `tools/hooks/state_claim_check.py` and its tests stay.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / ".claude" / "settings.json"
BRAIN_DOC = ROOT / "docs" / "agents" / "brain.md"
RULES_320 = ROOT / "tests" / "test_issue320_every_rule_names_a_role.py"
BLOCK_START = "<!-- reply-check:start -->"
BLOCK_END = "<!-- reply-check:end -->"
REFERENCE_COMMIT = "e2ac3d8"
#: the Stop entry as it stood before #347 (in .claude/settings.json and config/core_templates/settings.hooks.json)
AS_IT_STOOD = {"Stop": [{"hooks": [{
    "type": "command",
    "command": 'python "$CLAUDE_PROJECT_DIR/tools/hooks/state_claim_check.py"',
    "timeout": 30,
}]}]}


def _marked_block(text: str) -> str:
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END) + len(BLOCK_END)
    return text[start:end]


def _outside_block(text: str) -> str:
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END) + len(BLOCK_END)
    return text[:start] + text[end:]


def _load_320_rules():
    spec = importlib.util.spec_from_file_location("issue320_rules", RULES_320)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_stop_hooks_name_no_state_claim_check():
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    stop_entries = settings.get("hooks", {}).get("Stop", [])
    commands = [h["command"] for entry in stop_entries for h in entry.get("hooks", [])]
    assert not any("state_claim_check.py" in command for command in commands), commands


def test_brain_doc_restore_line_names_both_switches_and_both_files():
    text = BRAIN_DOC.read_text(encoding="utf-8")
    restore_lines = [line for line in _outside_block(text).splitlines() if "REPLY_CHECK_ENABLED" in line]
    assert restore_lines, "no restore line outside the reply-check block names REPLY_CHECK_ENABLED"
    line = restore_lines[0]
    assert "state_claim_check.py" in line, line
    assert "`.claude/settings.json`" in line, line
    assert "`config/core_templates/settings.hooks.json`" in line, line
    span = re.search(r'`("Stop": [^`]*)`', line)
    assert span, line
    assert json.loads("{" + span.group(1) + "}") == AS_IT_STOOD


def test_reply_check_marked_block_is_unchanged_since_e2ac3d8():
    probe = subprocess.run(["git", "cat-file", "-e", REFERENCE_COMMIT], cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"commit {REFERENCE_COMMIT} is not in this repo (a public test copy has no such history)")
    reference = subprocess.run(
        ["git", "show", f"{REFERENCE_COMMIT}:docs/agents/brain.md"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    assert _marked_block(BRAIN_DOC.read_text(encoding="utf-8")) == _marked_block(reference)


def test_restored_stop_entry_in_a_copy_keeps_the_320_wiring_checks_green(tmp_path, monkeypatch):
    rules = _load_320_rules()
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    settings["hooks"]["Stop"] = AS_IT_STOOD["Stop"]
    copy = tmp_path / "settings.json"
    copy.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(rules, "WIRING_SOURCES", (copy, *rules.WIRING_SOURCES[1:]))
    assert "state_claim_check" in rules.wired_hooks()
    rules.test_every_wired_hook_is_a_file_of_the_hooks_folder()
    rules.test_every_hook_file_is_either_wired_or_a_listed_helper()
    rules.test_every_wired_hook_is_counted_in_the_discovery()
    rules.test_count_of_rules_without_a_role_is_zero()
