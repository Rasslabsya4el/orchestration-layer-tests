"""#461 unit 2: a hook on an ordinary Bash, Read or Edit call does not import the dispatcher's module chain.

Every registered hook runs as its own Python process per tool call, so a hook that imports
``tools.agentops.dispatch`` (and with it ``core_issue``, ``core_update``, ``merge``) at import time adds ~150 ms to
every call of every session. The seam is the hook script run as ``__main__`` on the payload a tool kind carries
(`tools.agentops.hook_timing` builds the same payloads); the check lists the heavy modules loaded when it ends.
The hooks come from the repo's own ``.claude/settings.json``, so a hook added later is covered.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import hook_timing

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / ".claude" / "settings.json"
#: the chain `scripts/cost_levers.py` pulls in through `tools.agentops.dispatch`.
HEAVY = ("tools.agentops.dispatch", "cost_levers", "tools.agentops.core_issue", "tools.agentops.core_update",
         "tools.agentops.merge")
RUNNER = (
    "import json, runpy, sys\n"
    "try:\n"
    "    runpy.run_path(sys.argv[1], run_name='__main__')\n"
    "except SystemExit:\n"
    "    pass\n"
    "sys.stderr.write(' LOADED:' + json.dumps([m for m in sys.argv[2:] if m in sys.modules]))\n"
)


def _registered() -> list[tuple[str, str, str]]:
    """(script path, event, tool kind) for each core hook script the settings register for a measured tool kind."""
    found = []
    for hook in hook_timing.read_hooks(SETTINGS):
        match = re.search(r"tools/hooks/(\w+\.py)", hook.command)
        if not match:
            continue
        for kind in hook_timing.TOOL_KINDS:
            if hook.takes(kind):
                found.append((match.group(1), hook.event, kind))
    return sorted(set(found))


@pytest.mark.parametrize(("script", "event", "kind"), _registered())
def test_hook_does_not_import_dispatch_chain(script, event, kind, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    hook_timing._write_transcript(transcript)
    payload = hook_timing._payload(kind, event, ROOT, transcript)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(ROOT), PYTHONPATH=str(ROOT))
    for name in hook_timing._REDIRECTED:
        env[name] = str(tmp_path / name.lower())
    done = subprocess.run(
        [sys.executable, "-c", RUNNER, str(ROOT / "tools" / "hooks" / script), *HEAVY],
        input=json.dumps(payload), capture_output=True, text=True, cwd=ROOT, env=env, timeout=60,
    )
    assert "LOADED:" in done.stderr, done.stderr[-500:]
    loaded = json.loads(done.stderr.rsplit("LOADED:", 1)[1])
    assert loaded == [], f"{script} on {kind} {event} imported {loaded}"


def test_a_session_the_rule_does_not_bind_is_still_refused_when_the_root_lookup_fails(monkeypatch):
    """The decision `value_text_guard` made before #461 and keeps: a file write whose repo-root lookup raises is
    refused with the install step, whatever the session is (the lookup stays ahead of the bound-session shortcut)."""
    import tools.agentops.worktree as worktree
    from tools.hooks import value_text_guard

    def broken(root):
        raise RuntimeError("no git here")

    monkeypatch.setattr(worktree, "main_checkout_root", broken)
    payload = {"tool_name": "Write", "cwd": str(ROOT), "agent_type": "worker",
               "tool_input": {"file_path": str(ROOT / "docs" / "STATE.md"), "content": "plain text"}}
    reason = value_text_guard.run(payload)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "cannot run" in reason and "refused" in reason and "RuntimeError" in reason
