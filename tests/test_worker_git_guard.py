"""tools/hooks/worker_git_guard.py (issue #215). No network, no live session: the hook run as a
subprocess with a JSON `PreToolUse` payload on stdin, and a temp log file per test."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from timing_bound import bound
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "worker_git_guard.py"

REFUSED_COMMANDS = [
    "git stash",
    'git stash push -u -m "x"',
    "git stash save",
    "git stash pop",
    "git stash apply 2a00665",
    "git stash drop stash@{0}",
    "git stash clear",
    "git -C C:/wt stash push",
    "cd wt && git stash",
    "git status; git stash pop",
]

PASSED_COMMANDS = [
    "git stash list",
    "git stash list --format='%H %gs'",
    "git stash show -p",
    'git commit -m "never git stash"',
    'grep -rn "git stash" docs/',
    "cat > file <<'EOF'\ngit stash pop\nEOF",
]


def _run_hook(command: str, log_path: Path, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "agent_id": "agent-test-id",
        "agent_type": "worker",
    }
    env = {"AGENTOPS_HOOK_LOG": str(log_path)}
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, env=full_env,
    )


def _deny_reason(result: subprocess.CompletedProcess) -> str | None:
    out = result.stdout.strip()
    if not out:
        return None
    obj = json.loads(out)
    return obj["hookSpecificOutput"]["permissionDecisionReason"]


# -- required case 1: 10 commands refused, each with the reason of item 2 -----------------------

def test_ten_refused_commands(tmp_path):
    log_path = tmp_path / "guard.log"
    for command in REFUSED_COMMANDS:
        result = _run_hook(command, log_path)
        reason = _deny_reason(result)
        assert reason is not None, f"expected refusal for: {command!r}"
        assert result.returncode == 0
        assert "checkout --detach" in reason
        assert "checkout <your branch>" in reason


# -- required case 2: 6 commands passed, no output, exit 0 --------------------------------------

def test_six_passed_commands(tmp_path):
    log_path = tmp_path / "guard.log"
    for command in PASSED_COMMANDS:
        result = _run_hook(command, log_path)
        assert result.returncode == 0, f"unexpected nonzero for: {command!r}"
        assert result.stdout.strip() == "", f"unexpected output for: {command!r}"


# -- required case 3: the same refused command through PowerShell tool_name ---------------------

def test_powershell_tool_input_refused(tmp_path):
    log_path = tmp_path / "guard.log"
    result = _run_hook("git stash", log_path, tool_name="PowerShell")
    reason = _deny_reason(result)
    assert reason is not None
    assert result.returncode == 0


# -- required case 4: no `command`, and non-JSON input -> 0 refusals, exit 0, no output ----------

def test_missing_command_field_no_refusal(tmp_path):
    log_path = tmp_path / "guard.log"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {},
    }
    import os

    env = dict(os.environ)
    env["AGENTOPS_HOOK_LOG"] = str(log_path)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_non_json_input_no_refusal(tmp_path):
    log_path = tmp_path / "guard.log"
    import os

    env = dict(os.environ)
    env["AGENTOPS_HOOK_LOG"] = str(log_path)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input="not json at all",
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# -- required case 5: after the 11 refusals of rows 1 and 3 the log holds 11 lines; after row 2 it
# holds no more ------------------------------------------------------------------------------------

def test_log_holds_one_line_per_refusal_only(tmp_path):
    log_path = tmp_path / "guard.log"
    for command in REFUSED_COMMANDS:
        _run_hook(command, log_path)
    _run_hook("git stash", log_path, tool_name="PowerShell")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 11
    for line in lines:
        row = json.loads(line)
        assert row["agent_id"] == "agent-test-id"
        assert row["agent_type"] == "worker"

    for command in PASSED_COMMANDS:
        _run_hook(command, log_path)

    lines_after = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == 11


# -- required case 7: the whole run of the script on one command ends well under a second -------

def test_single_run_is_fast(tmp_path):
    log_path = tmp_path / "guard.log"
    start = time.monotonic()
    _run_hook("git stash", log_path)
    elapsed = time.monotonic() - start
    # bound = max(floor, factor x a CPU spin timed here): load that slows the spin slows the call (#824)
    bound_s = bound(1.0, 6.0)
    assert elapsed < bound_s, f"one hook run took {elapsed:.3f}s, bound {bound_s:.2f}s"
