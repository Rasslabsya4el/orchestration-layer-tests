"""#1145: tools/hooks/agent_role_prefix.py, a PreToolUse hook on `Agent` / `Task`, puts the role
(`subagent_type`) in front of the call's `description` -- the app shows a subagent row by that
description. The host lets a hook rewrite tool input (`hookSpecificOutput.updatedInput` with
`permissionDecision: allow`), so the hook rewrites; it never refuses. The hook runs as a subprocess
with a JSON `PreToolUse` payload on stdin, like the other guards."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "agent_role_prefix.py"
ROLES = sorted(p.stem for p in (PROJECT_ROOT / ".claude" / "agents").glob("*.md"))


def _run(payload, raw: str | None = None):
    stdin = raw if raw is not None else json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=stdin, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    return json.loads(result.stdout) if result.stdout.strip() else None


def _call(tool_name: str, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}


def _rewritten(out) -> dict:
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "allow"
    return spec["updatedInput"]


def test_roles_are_the_agent_files():
    assert {"worker", "worker-strong", "researcher-find", "researcher-how", "reviewer-standards", "reviewer-spec"} <= set(ROLES)


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_worker_description_gets_the_role_in_front(tool):
    out = _run(_call(tool, subagent_type="worker", description="Home keeps branch main", prompt="p"))
    updated = _rewritten(out)
    assert updated["description"] == "worker: Home keeps branch main"
    # updatedInput replaces the whole input: every other field travels unchanged
    assert updated["prompt"] == "p"
    assert updated["subagent_type"] == "worker"


@pytest.mark.parametrize("role", ROLES)
def test_every_role_of_claude_agents_is_prefixed(role):
    out = _run(_call("Agent", subagent_type=role, description="Some ticket title", prompt="p"))
    assert _rewritten(out)["description"] == f"{role}: Some ticket title"


@pytest.mark.parametrize("role", ROLES)
def test_already_prefixed_is_left_alone(role):
    assert _run(_call("Agent", subagent_type=role, description=f"{role}: Some title", prompt="p")) is None


def test_prefix_of_another_role_is_not_taken_for_the_own():
    # `worker-strong: x` does not start with `worker:`
    out = _run(_call("Agent", subagent_type="worker", description="worker-strong: x", prompt="p"))
    assert _rewritten(out)["description"] == "worker: worker-strong: x"


def test_no_subagent_type_is_left_alone():
    assert _run(_call("Agent", description="Home keeps branch main", prompt="p")) is None
    assert _run(_call("Agent", subagent_type="", description="Home keeps branch main")) is None


def test_other_tools_are_left_alone():
    assert _run(_call("Bash", subagent_type="worker", description="x", command="ls")) is None


def test_missing_or_empty_description_is_left_alone():
    assert _run(_call("Agent", subagent_type="worker", prompt="p")) is None
    assert _run(_call("Agent", subagent_type="worker", description="", prompt="p")) is None


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"tool_name": "Agent", "tool_input": 5}'])
def test_broken_input_fails_open(raw):
    assert _run(None, raw=raw) is None


def test_hook_is_registered_and_shipped_with_the_core():
    for rel in (".claude/settings.json", "config/core_templates/settings.hooks.json"):
        entries = json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        hits = [e for e in entries if any("agent_role_prefix.py" in h["command"] for h in e["hooks"])]
        assert len(hits) == 1, rel
        assert hits[0]["matcher"] == "Agent|Task"
    manifest = (PROJECT_ROOT / "tools" / "agentops" / "core_manifest.txt").read_text(encoding="utf-8")
    assert "tools/hooks/agent_role_prefix.py" in manifest.splitlines()
