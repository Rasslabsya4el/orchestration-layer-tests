"""tools/hooks/worker_process_guard.py: refuses a Bash/PowerShell command that itself starts a
process through WMI or the task scheduler, or starts the real thing (the 11 forms below). The hook
run as a subprocess with a JSON `PreToolUse` payload on stdin, and a temp log file per test -- same
harness shape as tests/test_worker_git_guard.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from timing_bound import bound

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "worker_process_guard.py"

#: the 11 forms, plain.
FORM_COMMANDS = [
    'Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="cmd"}',
    'Invoke-WmiMethod -Class Win32_Process -Name Create -ArgumentList "cmd"',
    '$c=[wmiclass]"\\\\.\\root\\cimv2:Win32_Process"; $c.Create("cmd")',
    'wmic process call create "cmd.exe /c calc"',
    'schtasks /run /tn "agent-standin"',
    'schtasks /create /tn "agent-standin" /tr "cmd" /sc once /st 00:00',
    'Register-ScheduledTask -TaskName "x" -Action $a -Trigger $t',
    'Start-ScheduledTask -TaskName "x"',
    'powershell -File scripts/launch_hidden.ps1 -Command "cmd"',
    'python -m tools.agentops keepalive install',
    'python -m tools.agentops dispatch',
]

#: the same 11 forms, each inside a chain (`&&` or `;`).
CHAINED_FORM_COMMANDS = [
    'echo start && Invoke-CimMethod -ClassName Win32_Process -MethodName Create',
    'echo start ; Invoke-WmiMethod -Class Win32_Process -Name Create',
    'echo start && $c=[wmiclass]"Win32_Process"; $c.Create("cmd")',
    'echo start ; wmic process call create "cmd.exe"',
    'echo start && schtasks /run /tn "agent-standin"',
    'echo start ; schtasks /create /tn "agent-standin" /tr "cmd"',
    'echo start && Register-ScheduledTask -TaskName "x"',
    'echo start ; Start-ScheduledTask -TaskName "x"',
    'echo start && powershell -File scripts/launch_hidden.ps1',
    'echo start ; python -m tools.agentops keepalive install',
    'echo start && python -m tools.agentops dispatch',
]

REFUSED_COMMANDS = FORM_COMMANDS + CHAINED_FORM_COMMANDS

PASSED_COMMANDS = [
    'git commit -m "never Invoke-CimMethod Win32_Process Create"',
    'echo "wmic process call create is refused"',
    'grep -rn "schtasks /create" docs/',
    'cat > file <<\'EOF\'\nInvoke-CimMethod -ClassName Win32_Process -MethodName Create\nEOF',
    'Get-CimInstance Win32_Process',
    'schtasks /query',
    'python -m tools.agentops dispatch --help',
]

#: 2 dispatch launches the rule adds to the 22 (#36): an entry token with a flag before the word, and
#: a path to `__main__.py`.
DISPATCH_FORM_COMMANDS = [
    'python -m tools.agentops --x dispatch',
    'python tools/agentops/__main__.py dispatch',
]

#: 2 more entry forms: `-m agentops`, and a backslash path to `__main__.py` (Windows).
DISPATCH_ENTRY_FORM_COMMANDS = [
    'python -m agentops dispatch',
    r'python tools\agentops\__main__.py dispatch',
]

#: 4 commands that only mention `dispatch` or a path with `agentops` in it: they pass (#36, row 1).
NEW_PASSED_COMMANDS = [
    'python -m pytest tests -q -k dispatch',
    'git ls-files --eol tools/agentops/dispatch.py && python -m pytest tests -q -k dispatch',
    'python -m pytest tests -q -m "not slow and not pob" -k dispatch',
    'for f in merge core_update dispatch; do python - <<EOF\nimport pathlib\nprint(pathlib.Path("tools/agentops/$f.py").exists())\nEOF\ndone',
]


_HOOK_TIMEOUT: list[float] = []


def _hook_timeout() -> float:
    # one load-scaled bound per session (a spin per start would add ~0.15 s to each of the 22 starts);
    # the hook only reads stdin and matches strings, so a start past this bound is a real hang.
    if not _HOOK_TIMEOUT:
        _HOOK_TIMEOUT.append(bound(10.0, 66.0))
    return _HOOK_TIMEOUT[0]


def _run_hook(command: str, log_path: Path, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "agent_id": "agent-test-id",
        "agent_type": "worker",
    }
    import os

    env = dict(os.environ)
    env["AGENTOPS_HOOK_LOG"] = str(log_path)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=_hook_timeout(), env=env,
    )


def _deny_reason(result: subprocess.CompletedProcess) -> str | None:
    out = result.stdout.strip()
    if not out:
        return None
    obj = json.loads(out)
    return obj["hookSpecificOutput"]["permissionDecisionReason"]


# -- row 5: 22 of 22 refused (11 forms, plain and inside a chain) -------------------------------

def test_all_22_forms_refused(tmp_path):
    log_path = tmp_path / "guard.log"
    assert len(REFUSED_COMMANDS) == 22
    refused = 0
    for command in REFUSED_COMMANDS:
        result = _run_hook(command, log_path)
        reason = _deny_reason(result)
        assert result.returncode == 0, f"unexpected nonzero for: {command!r}"
        if reason is not None:
            refused += 1
            assert "owner's desktop" in reason
            assert "not measured" in reason
        else:
            raise AssertionError(f"expected refusal for: {command!r}")
    assert refused == 22


def test_refusal_names_the_workers_step(tmp_path):
    result = _run_hook("powershell -File scripts/launch_hidden.ps1 -Command \"cmd\"", tmp_path / "g.log")
    reason = _deny_reason(result)
    assert reason is not None
    assert "do not start it" in reason
    assert "exact command" in reason
    assert "ORCH" in reason
    assert "desktop object of its own" not in reason


# -- row 6: 7 of 7 passed -------------------------------------------------------------------------

def test_all_7_passed_commands_pass(tmp_path):
    log_path = tmp_path / "guard.log"
    assert len(PASSED_COMMANDS) == 7
    passed = 0
    for command in PASSED_COMMANDS:
        result = _run_hook(command, log_path)
        assert result.returncode == 0, f"unexpected nonzero for: {command!r}"
        assert result.stdout.strip() == "", f"unexpected output for: {command!r}"
        passed += 1
    assert passed == 7


# -- #36: the dispatch launch is refused through the entry forms, and only the real launch ---------

def _assert_all_refused(commands: list[str], log_path: Path) -> int:
    refused = 0
    for command in commands:
        result = _run_hook(command, log_path)
        reason = _deny_reason(result)
        assert result.returncode == 0, f"unexpected nonzero for: {command!r}"
        assert reason is not None, f"expected refusal for: {command!r}"
        assert "owner's desktop" in reason
        assert "not measured" in reason
        refused += 1
    return refused


def test_two_new_dispatch_forms_refused(tmp_path):
    assert len(DISPATCH_FORM_COMMANDS) == 2
    assert _assert_all_refused(DISPATCH_FORM_COMMANDS, tmp_path / "guard.log") == 2


def test_dispatch_entry_forms_refused(tmp_path):
    assert len(DISPATCH_ENTRY_FORM_COMMANDS) == 2
    assert _assert_all_refused(DISPATCH_ENTRY_FORM_COMMANDS, tmp_path / "guard.log") == 2


def test_four_new_commands_pass(tmp_path):
    log_path = tmp_path / "guard.log"
    assert len(NEW_PASSED_COMMANDS) == 4
    passed = 0
    for command in NEW_PASSED_COMMANDS:
        result = _run_hook(command, log_path)
        assert result.returncode == 0, f"unexpected nonzero for: {command!r}"
        assert result.stdout.strip() == "", f"unexpected output for: {command!r}"
        passed += 1
    assert passed == 4


# -- the same refused command through PowerShell tool_name ---------------------------------------

def test_powershell_tool_input_refused(tmp_path):
    log_path = tmp_path / "guard.log"
    result = _run_hook("wmic process call create \"cmd.exe\"", log_path, tool_name="PowerShell")
    reason = _deny_reason(result)
    assert reason is not None
    assert result.returncode == 0


# -- no `command`, and non-JSON input -> 0 refusals, exit 0, no output ---------------------------

def test_missing_command_field_no_refusal(tmp_path):
    log_path = tmp_path / "guard.log"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}
    import os

    env = dict(os.environ)
    env["AGENTOPS_HOOK_LOG"] = str(log_path)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=_hook_timeout(), env=env,
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
        capture_output=True, text=True, timeout=_hook_timeout(), env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# -- one log line per refusal, none for a passed command -----------------------------------------

def test_log_holds_one_line_per_refusal_only(tmp_path):
    log_path = tmp_path / "guard.log"
    for command in FORM_COMMANDS:
        _run_hook(command, log_path)

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


# -- one run of the script on one command ends well under a second -------------------------------

def test_single_run_is_fast(tmp_path):
    import time

    log_path = tmp_path / "guard.log"
    start = time.monotonic()
    _run_hook("schtasks /run /tn x", log_path)
    elapsed = time.monotonic() - start
    # bound = max(floor, factor x a CPU spin timed here): load that slows the spin slows the call (#824)
    bound_s = bound(1.0, 6.0)
    assert elapsed < bound_s, f"one hook run took {elapsed:.3f}s, bound {bound_s:.2f}s"
