"""Issue #199 unit 1: tracker_guard refuses a core-issue create whose body has no `Flow fix:` line,
or says `Flow fix: yes` without the label `flow-fix`. Tested through tracker_guard.run(payload);
no PoB, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.hooks import tracker_guard  # noqa: E402

AREAS = ["area:orchestration", "area:builds", "area:runs", "area:crafting"]
TITLE = "Flasks are active only at 100 % Flask uptime in the heavy tool"
HUMAN = "\n".join([
    "## For the human",
    "- **What:** in day-1 builds every flask is switched on, even at 0 % uptime.",
    "- **Goal:** a flask is active only at 100 % uptime, in every build the owner runs.",
    "- **Plan:** the flag comes from the same calculation the heavy tool shows.",
    "- **Result:** all 5 flasks switch off; shown by a test.",
    "- **Undo:** two-way door (one revert); touches every build that wears flasks.",
    "",
])
CORE = "--label core-issue --label area:orchestration"
CORE_FLOW = "--label core-issue --label flow-fix --label area:orchestration"
PLAIN = "--label area:orchestration"


def agent_block(*extra):
    return "\n".join(["## For the agent", "Spec lives in `tools/hooks/tracker_guard.py:131`.", *extra, ""])


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "orch.json"
    data = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    settings.write_text(json.dumps({**data, "areas": AREAS}), encoding="utf-8")
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(settings))
    monkeypatch.setenv("TRACKER_GUARD_LOG", str(tmp_path / "log.jsonl"))
    return tmp_path


def _body_path(env, text, name="body.md"):
    path = env / name
    path.write_text(text, encoding="utf-8")
    return path.as_posix()


def _run(env, command):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}, "cwd": str(env)}
    out = tracker_guard.run(payload)
    return None if out is None else out["hookSpecificOutput"]["permissionDecisionReason"]


def _create(env, body_text, labels):
    return f'gh issue create --title "{TITLE}" --body-file "{_body_path(env, body_text)}" {labels}'


def test_core_create_without_flow_fix_line_is_refused(env):
    reason = _run(env, _create(env, HUMAN + agent_block(), CORE))
    assert reason is not None and "Flow fix:" in reason


def test_core_create_with_flow_fix_no_passes(env):
    body = HUMAN + agent_block("Flow fix: no (the change touches one hook only)")
    assert _run(env, _create(env, body, CORE)) is None


def test_core_create_flow_fix_yes_without_label_is_refused(env):
    body = HUMAN + agent_block("Flow fix: yes")
    reason = _run(env, _create(env, body, CORE))
    assert reason is not None and "flow-fix" in reason


def test_core_create_flow_fix_yes_with_label_passes(env):
    body = HUMAN + agent_block("Flow fix: yes")
    assert _run(env, _create(env, body, CORE_FLOW)) is None


def test_non_core_create_without_flow_fix_line_is_unchanged(env):
    assert _run(env, _create(env, HUMAN + agent_block(), PLAIN)) is None


def test_core_body_edit_without_flow_fix_line_is_unchanged(env):
    body_path = _body_path(env, HUMAN + agent_block())
    command = f'gh issue edit 199 --body-file "{body_path}"'
    assert _run(env, command) is None


def test_core_create_flow_fix_yes_with_reason_needs_the_label(env):
    body = HUMAN + agent_block("Flow fix: yes (the merge line runs fewer tests)")
    reason = _run(env, _create(env, body, CORE))
    assert reason is not None and "flow-fix" in reason
    assert _run(env, _create(env, body, "-l core-issue,flow-fix --label area:orchestration")) is None
