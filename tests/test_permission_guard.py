"""tools/hooks/permission_guard.py (#354, unit 1): the deny rows and allow rows of the spec (#354, #361, #362), each deny logging its `hit` and `rule` (#363),
through `guard()` in-process (log line included), one subprocess case per tool, a timing test.
No PoB, no network."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from timing_bound import bound
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOOK = ROOT / "tools" / "hooks" / "permission_guard.py"

from tools.hooks import permission_guard as pg  # noqa: E402

MAIN = r"C:\Coding\poe-junkroan-v2"
WT = MAIN + r"\.claude\worktrees\orch-360"

# (id, line, tool, input, cwd)
DENY = [
    ("D1", 1, "Bash", "rm -rf C:/Coding/poe-junkroan-v2", MAIN),
    ("D2", 1, "Bash", "rm -rf src/junkroan", MAIN),
    ("D3", 1, "Bash", "rm -r artifacts/benchmark", MAIN),
    ("D4", 1, "PowerShell", r"Remove-Item -Recurse -Force C:\Coding\poe-junkroan-v2\var", MAIN),
    ("D5", 1, "PowerShell", r"Remove-Item .claude\worktrees\orch-315 -Recurse", MAIN),
    ("D6", 1, "Bash", r"rmdir /s /q C:\Coding\poe-junkroan-v2\vendor", MAIN),
    ("D7", 1, "Bash", "del /s /q *.pob", MAIN),
    ("D8", 1, "Bash", "git clean -fdx", MAIN),
    ("D9", 1, "Bash", "git worktree remove .claude/worktrees/orch-315 --force", MAIN),
    ("D10", 1, "Bash", "rm -rf tmp_318_dry.out C:/Coding/poe-junkroan-v2/src", MAIN),
    ("D11", 2, "Bash", "rm C:/Windows/System32/drivers/etc/hosts", MAIN),
    ("D12", 2, "PowerShell", r"Remove-Item 'C:\Program Files\Path of Building Community' -Recurse", MAIN),
    ("D13", 2, "Bash", 'echo x > "C:/Program Files/x.txt"', MAIN),
    ("D14", 2, "PowerShell", r"Set-Content C:\ProgramData\x.txt 'x'", MAIN),
    ("D15", 2, "Bash", "mv C:/Users/user/Documents/notes.txt ./", MAIN),
    ("D16", 2, "Bash", r"reg add HKLM\SOFTWARE\x /v y /d z", MAIN),
    ("D17", 2, "PowerShell", r"Set-ItemProperty -Path HKCU:\Software\x -Name y -Value z", MAIN),
    ("D18", 2, "Bash", "format D: /q", MAIN),
    ("D19", 2, "Bash", "diskpart /s script.txt", MAIN),
    ("D20", 2, "Bash", "shutdown /r /t 0", MAIN),
    ("D21", 2, "PowerShell", "Restart-Computer -Force", MAIN),
    ("D22", 2, "Bash", "bcdedit /set safeboot minimal", MAIN),
    ("D23", 2, "Bash", "netsh advfirewall set allprofiles state off", MAIN),
    ("D24", 2, "PowerShell", "Stop-Service -Name Spooler", MAIN),
    ("D25", 2, "Bash", "sc delete junkroan", MAIN),
    ("D26", 2, "Bash", "schtasks /delete /tn junkroan-daemon /f", MAIN),
    ("D27", 2, "Bash", "net user agent /add", MAIN),
    ("D28", 2, "Bash", "winget uninstall Git.Git", MAIN),
    ("D29", 2, "Bash", "msiexec /x {GUID} /quiet", MAIN),
    ("D30", 3, "Bash", "git push --force origin main", MAIN),
    ("D31", 3, "Bash", "git push -f", MAIN),
    ("D32", 3, "Bash", "git push origin +main", MAIN),
    ("D33", 3, "Bash", "git push --force-with-lease origin issue-1", MAIN),
    ("D34", 3, "Bash", "git push origin --delete issue-352", MAIN),
    ("D35", 3, "Bash", "git push origin :issue-352", MAIN),
    ("D36", 3, "Bash", "git remote add upstream https://example.com/x.git", MAIN),
    ("D37", 3, "Bash", "git push backup main", MAIN),
    ("D38", 3, "Bash", "git reset --hard HEAD~3", MAIN),
    ("D39", 3, "Bash", "git checkout -- .", MAIN),
    ("D40", 3, "Bash", "git restore .", MAIN),
    ("D41", 3, "Bash", "git branch -D issue-352", MAIN),
    ("D42", 3, "Bash", "git rebase origin/main", MAIN),
    ("D43", 3, "Bash", "git -C C:/Coding/poe-junkroan-v2 reset --hard origin/main", WT),
    ("D44", 3, "Bash", "git filter-branch --all", MAIN),
    ("D45", 4, "Bash", "gh repo delete Rasslabsya4el/Poe-Junkroan --yes", MAIN),
    ("D46", 4, "Bash", "gh repo edit --visibility public", MAIN),
    ("D47", 4, "Bash", "gh issue delete 12 --yes", MAIN),
    ("D48", 4, "Bash", "gh release delete v1 -y", MAIN),
    ("D49", 4, "Bash", "gh secret set TOKEN", MAIN),
    ("D50", 4, "Bash", "gh auth logout", MAIN),
    ("D51", 4, "Bash", "gh api -X DELETE repos/Rasslabsya4el/Poe-Junkroan/issues/12/labels/bug", MAIN),
    ("D52", 4, "Bash", "gh api --method DELETE repos/x/y", MAIN),
    ("D53", 4, "Bash", "gh label delete bug --yes", MAIN),
    ("D53a", 4, "Bash", "gh api -X DELETE repos/x/y/issues/12", MAIN),
    ("D53b", 4, "Bash", "gh api -X DELETE repos/x/y/issues/comments/99", MAIN),
    ("D53c", 4, "Bash", "gh api -X DELETE repos/x/y/issues/383/sub_issues", MAIN),
    ("D53d", 4, "Bash", "gh api -X DELETE repos/x/y/issues/1077", MAIN),
    ("D53e", 4, "Bash", "gh api -X DELETE repos/x/y/issues/1077/dependencies/blocked_by", MAIN),
    ("D53f", 4, "Bash", "gh api -X DELETE repos/x/y/issues/12 -f note=repos/x/y/issues/1/dependencies/blocked_by/5", MAIN),
    ("D54", 5, "Bash", "taskkill /IM python.exe /F", MAIN),
    ("D55", 5, "Bash", "taskkill /PID 1234 /F", MAIN),
    ("D56", 5, "PowerShell", "Stop-Process -Name junkroan -Force", MAIN),
    ("D57", 5, "PowerShell", "Get-Process claude | Stop-Process", MAIN),
    ("D58", 5, "Bash", "kill -9 1234", MAIN),
    ("D59", 5, "Bash", "pkill -f run_queue", MAIN),
    ("D60", 5, "Bash", "wmic process where name='python.exe' delete", MAIN),
    ("D61", 6, "Edit", MAIN + r"\.claude\settings.json", MAIN),
    ("D62", 6, "Write", MAIN + r"\tools\hooks\permission_guard.py", MAIN),
    ("D63", 6, "Edit", MAIN + r"\config\orchestration.json", MAIN),
    ("D64", 6, "Write", r"C:\Users\user\.claude\settings.json", MAIN),
    ("D65", 6, "Write", WT + r"\..\..\.claude\settings.json", MAIN),
    ("D66", 6, "Bash", "echo '{}' > .claude/settings.json", MAIN),
    ("D67", 6, "Bash", "sed -i 's/a/b/' tools/hooks/tracker_guard.py", MAIN),
    ("D68", 6, "PowerShell", r"Set-Content C:\Users\user\.claude\settings.json '{}'", MAIN),
    ("D69", 6, "Bash", "cp x.json C:/Users/user/.claude/settings.json", WT),
    ("D70", 6, "Bash", "gh auth token", MAIN),
    ("D71", 6, "Bash", "cat .env", MAIN),
    ("D72", 6, "PowerShell", r"Get-Content C:\Users\user\.ssh\id_ed25519", MAIN),
    ("D73", 6, "Bash", r"type %APPDATA%\gh\hosts.yml", MAIN),
    ("D74", 6, "Bash", "git add .env", MAIN),
    ("D75", 6, "Bash", "cp x.json C:/Users/user/.claude/settings.json", MAIN),
    ("D76", 6, "PowerShell", "Copy-Item x.json -Destination C:/Users/user/.claude/settings.json", MAIN),
    ("D77", 6, "Bash", "echo x > .claude.json", MAIN),
    ("D78", 6, "PowerShell", r"Set-Content C:\Users\user\.claude.json x", MAIN),
    ("D79", 6, "Bash", "mv C:/Users/user/.claude/settings.json tmp_orch/x.json", MAIN),
    # #362: the own-worktree exemption stops at the worktree root, the repo root and everything outside
    ("D80", 1, "Bash", "rm -rf C:/Coding/poe-junkroan-v2/.claude/worktrees/orch-360", WT),
    ("D81", 1, "Bash", "rm -rf .", WT),
    ("D82", 1, "Bash", "rm -rf *", WT),
    ("D83", 1, "Bash", "rm -rf ../orch-315", WT),
    ("D84", 1, "Bash", "rm -rf C:/Coding/poe-junkroan-v2", WT),
    ("D85", 1, "Bash", "rm -rf C:/Coding/poe-junkroan-v2/src", WT),
    ("D86", 1, "Bash", "rm -rf .git", WT),
    ("D87", 1, "PowerShell", r"Remove-Item -Recurse -Force C:\Coding\poe-junkroan-v2\.claude\worktrees\orch-360", WT),
    ("D88", 1, "Bash", "rm -rf C:/Coding/poe-junkroan-v2/.claude/worktrees/orch-360", MAIN),
    ("D89", 1, "Bash", "cd src && rm -rf ../..", WT),
    ("D90", 1, "Bash", "rm -rf out/*", WT),
    ("D91", 1, "Bash", "rm -rf $UNSET_DIR/x", WT),
    ("D92", 1, "Bash", "rm -rf C:/Coding/other-repo/out", WT),
    ("D93", 1, "Bash", "rm -rf /c/Coding/poe-junkroan-v2/.claude/worktrees/orch-360", WT),
    ("D94", 1, "Bash", "rm -rf /tmp/../Coding/x", WT),
    ("D95", 2, "Bash", "cd C:/Users/user/Documents && rm -r stuff", WT),
    ("D96", 7, "Bash", "python -c \"import subprocess; subprocess.Popen(['python','-m','scripts.runs','daemon'], creationflags=0x00000008 | 0x00000200 | 0x08000000)\"", MAIN),
    ("D97", 7, "Bash", "cmd /k foo", MAIN),
    ("D98", 7, "PowerShell", "Start-Process powershell -File x.ps1", MAIN),
    ("D99", 7, "Bash", 'start "" foo.exe', MAIN),
    ("D100", 7, "Bash", "cmd /c start foo.exe", MAIN),
    ("D101", 7, "Bash", "python -c \"import subprocess; subprocess.Popen(['x'], creationflags=subprocess.CREATE_NEW_CONSOLE)\"", MAIN),
    ("D102", 1, "Bash", "rm -rf /tmp", WT),
    ("D103", 1, "Bash", "rm -rf /tmp/*", WT),
    ("D104", 1, "Bash", "rm -rf /tmp/hermes-5-home/../x", WT),
]

# the Zorblax worker's situation (#362): a git worktree of the repo Zorblax, refused by the guard at 2026-09-30 17:54
ZORBLAX_WT = r"C:\Coding\3go\3go - Zorblax cabinet dashboard\.claude\worktrees\agent-acda6f36ca1106278"

ALLOW = [
    ("A1", "Bash", "git pull", MAIN),
    ("A2", "Bash", "git fetch origin && git merge origin/main", MAIN),
    ("A3", "Bash", "git push origin issue-360", MAIN),
    ("A4", "Bash", "git push", MAIN),
    ("A5", "Bash", 'git commit -am "x"', MAIN),
    ("A6", "Bash", "git stash drop stash@{0}", MAIN),
    ("A7", "Bash", "git reset --hard origin/main", WT),
    ("A8", "Bash", "git rebase origin/main", WT),
    ("A9", "Bash", "git branch -D issue-360", WT),
    ("A10", "Bash", "git -C C:/Coding/poe-junkroan-v2/.claude/worktrees/orch-360 checkout -- .", MAIN),
    ("A11", "Bash", "git worktree add .claude/worktrees/orch-361 -b issue-361", MAIN),
    ("A12", "Bash", "git branch -d issue-352", MAIN),
    ("A13", "Bash", "gh issue comment 360 --body x", MAIN),
    ("A14", "Bash", "gh issue edit 360 --add-label ready-for-agent", MAIN),
    ("A14a", "Bash", "gh api -X DELETE repos/Rasslabsya4el/Poe-Junkroan/issues/383/sub_issue -F sub_issue_id=123", MAIN),
    ("A14b", "Bash", "gh api --method DELETE repos/Rasslabsya4el/Poe-Junkroan/issues/383/sub_issue -F sub_issue_id=123", MAIN),
    ("A14c", "Bash", "gh api -X DELETE repos/x/y/issues/1077/dependencies/blocked_by/123", MAIN),
    ("A14d", "Bash", "gh api --method DELETE repos/x/y/issues/1077/dependencies/blocked_by/123", MAIN),
    ("A15", "Bash", "gh api -X POST repos/Rasslabsya4el/Poe-Junkroan/issues/63/sub_issues -F sub_issue_id=1", MAIN),
    ("A16", "Bash", "gh pr create --fill", MAIN),
    ("A17", "Bash", "gh auth status", MAIN),
    ("A18", "Bash", "python scripts/runs.py mode pc normal", MAIN),
    ("A19", "Bash", "python scripts/runs.py submit --job x", MAIN),
    ("A20", "Bash", "python -m tools.agentops dispatch --ensure", MAIN),
    ("A21", "Bash", "python -m tools.agentops stop 1234 --why x", MAIN),
    ("A22", "Bash", "python -m tools.agentops guard install", MAIN),
    ("A23", "Bash", "python -m tools.agentops commit -m x docs/STATE.md", MAIN),
    ("A24", "Bash", "pip install -e .", MAIN),
    ("A25", "Bash", "npm install", MAIN),
    ("A26", "Bash", "winget install Git.Git", MAIN),
    ("A27", "Bash", "rm -rf tmp_318_dry.out tmp_orch/", MAIN),
    ("A28", "Bash", "rm -rf C:/Users/user/AppData/Local/Temp/claude/x/scratchpad/run1", MAIN),
    ("A29", "PowerShell", r"Remove-Item -Recurse $env:TEMP\claude-x", MAIN),
    ("A30", "Bash", "rm -rf src/junkroan/__pycache__ .pytest_cache", MAIN),
    ("A31", "Bash", "rm src/junkroan/old.py", MAIN),
    ("A32", "Bash", "pytest tests/test_x.py -q", MAIN),
    ("A33", "Bash", "curl -s https://www.poewiki.net/w/api.php", MAIN),
    ("A34", "Bash", "tasklist | findstr junkroan", MAIN),
    ("A35", "PowerShell", "Get-Process python", MAIN),
    ("A36", "Bash", "schtasks /query", MAIN),
    ("A37", "Bash", r"reg query HKLM\SOFTWARE\x", MAIN),
    ("A38", "Bash", "sc query junkroan", MAIN),
    ("A39", "Edit", WT + r"\tools\hooks\permission_guard.py", MAIN),
    ("A40", "Write", WT + r"\.claude\settings.json", MAIN),
    ("A41", "Edit", MAIN + r"\src\junkroan\pipeline.py", MAIN),
    ("A42", "Write", MAIN + r"\docs\STATE.md", MAIN),
    ("A43", "Write", r"C:\Users\user\.claude\projects\C--Coding-poe-junkroan-v2\memory\x.md", MAIN),
    ("A44", "Bash", "echo x > C:/Coding/poe-junkroan-v2/artifacts/runs/x.out", MAIN),
    ("A45", "Bash", "cat tools/hooks/tracker_guard.py", MAIN),
    ("A46", "Bash", "git add tools/hooks/permission_guard.py", WT),
    ("A48", "Bash", 'python -m tools.agentops guard check --claude-json tmp_orch/empty_claude.json >/dev/null; echo "guard exit (no key) $?";', MAIN),
    ("A49", "Bash", "cp ~/.claude/settings.json tmp_orch/user_settings_copy.json", MAIN),
    ("A50", "Bash", "cp C:/Users/user/.claude/settings.json C:/Users/user/AppData/Local/Temp/x.json", MAIN),
    ("A51", "PowerShell", "Copy-Item C:/Users/user/.claude/settings.json -Destination C:/Users/user/AppData/Local/Temp/x.json", MAIN),
    ("A52", "Bash", "echo x > my_claude.json", MAIN),
    # #362: the three commands of Zorblax's guard log (rows 3-5, session 3a54587e), from that worktree
    ("A53", "Bash", 'cd "C:/Coding/3go/3go - Zorblax cabinet dashboard/.claude/worktrees/agent-acda6f36ca1106278" '
                    "&& rm -r scratch_mut && python -m pytest tests -q", ZORBLAX_WT),
    ("A54", "Bash", "F=tests/fixtures/real-window-2026-08-06_2026-09-25; rm -rf .scratch256/out; cat > $F/README.md",
     ZORBLAX_WT),
    ("A55", "Bash", "rm -r .scratch256 2>/dev/null; git add tests/fixtures/real-window-2026-08-06_2026-09-25", ZORBLAX_WT),
    # the same, the session standing in a subfolder of its worktree, and with an absolute path
    ("A56", "Bash", "rm -rf scratch_mut", ZORBLAX_WT + r"\tests"),
    ("A58", "Bash", 'rm -rf "C:/Coding/3go/3go - Zorblax cabinet dashboard/.claude/worktrees/agent-acda6f36ca1106278/out/x"',
     ZORBLAX_WT),
    ("A59", "Bash", "rm -rf out/x ./build", WT),
    ("A60", "Bash", "cd tests && rm -r ../scratch", WT),
    ("A61", "PowerShell", "Remove-Item -Recurse -Force scratch", WT),
    ("A62", "Bash", "rmdir /s /q scratch", WT),
    ("A63", "Bash", "rm -rf C:/Coding/poe-junkroan-v2/.claude/worktrees/orch-360/scratch", WT),
    ("A64", "Bash", "rm -rf /c/Coding/poe-junkroan-v2/.claude/worktrees/orch-360/scratch", WT),
    # every spelling of the scratchpad / temp dir, from any cwd
    ("A65", "Bash", "rm -rf /c/Users/user/AppData/Local/Temp/claude/x/scratchpad/r", MAIN),
    ("A66", "Bash", r"rm -rf C:\Users\user\AppData\Local\Temp\claude\x\scratchpad\r", MAIN),
    ("A67", "Bash", "rm -rf c:/users/user/appdata/local/temp/claude/x/scratchpad/r", MAIN),
    ("A68", "Bash", "rm -rf C:/USERS/USER/APPDATA/LOCAL/TEMP/claude/x", MAIN),
    ("A69", "Bash", r"rm -rf %TEMP%\claude\x\scratchpad", MAIN),
    ("A70", "Bash", r"rm -rf %TMP%\claude-x", MAIN),
    ("A71", "Bash", "rm -rf $TMPDIR/claude/x/scratchpad", MAIN),
    ("A72", "Bash", "rm -rf ${TMPDIR}/claude/x", MAIN),
    ("A73", "Bash", "rm -rf $TEMP/claude-x", MAIN),
    ("A74", "PowerShell", r"Remove-Item -Recurse -Force $env:TMP\claude-x", MAIN),
    ("A75", "PowerShell", r"Remove-Item -Recurse -Force C:\Users\user\AppData\Local\Temp\claude-x", MAIN),
    ("A76", "PowerShell", r"Remove-Item -Recurse -Force 'C:\Users\user\AppData\Local\Temp\claude\x\scratchpad'", WT),
    ("A77", "Bash", "rm -rf /tmp/claude-x", WT),
    ("A78", "Bash", "rm -rf /c/Users/user/AppData/Local/Temp/claude/x/scratchpad/r", ZORBLAX_WT),
    ("A79", "Bash", r"rmdir /s /q C:\Users\user\AppData\Local\Temp\claude-x", MAIN),
    ("A80", "PowerShell", "Start-Process -WindowStyle Hidden powershell -ArgumentList '-File','x.ps1'", MAIN),
    ("A81", "Bash", "grep -rn DETACHED_PROCESS src", MAIN),
    ("A82", "PowerShell", 'powershell -File scripts/launch_hidden.ps1 -CommandLine "python -m tools.agentops dispatch"', MAIN),
    ("A83", "Bash", 'grep -n "Start-Process" docs/x.md', MAIN),
    ("A84", "Bash", "python -c \"import subprocess; subprocess.Popen(['x'], creationflags=0x08000000)\"", MAIN),
    ("A85", "PowerShell", "Start-Process -NoNewWindow python -ArgumentList 'x.py'", MAIN),
    ("A86", "Bash", "rm -rf /tmp/hermes-5-home", WT),
]

FILE_TOOLS = ("Edit", "Write", "MultiEdit")


def _payload(tool, text, cwd):
    key = "file_path" if tool in FILE_TOOLS else "command"
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: text},
            "cwd": cwd, "session_id": "s-test"}


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "permission_guard.log"
    monkeypatch.setenv("PERMISSION_GUARD_LOG", str(path))
    return path


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.parametrize("row", [pytest.param(r, id=r[0]) for r in DENY])
def test_deny_row(row, log):
    rid, line, tool, text, cwd = row
    out = pg.guard(_payload(tool, text, cwd))
    assert out is not None, rid
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"].startswith(f"permission guard, red line {line}:"), (
        rid, hso["permissionDecisionReason"])
    assert hso["permissionDecisionReason"].endswith("; " + pg.REFUSAL_SUFFIX), rid
    rows = _lines(log)
    assert len(rows) == 1
    logged = json.loads(rows[0])
    assert logged["line"] == line and logged["tool"] == tool and logged["session"] == "s-test"
    # #363: the log names the segment that hit and the red line's short name
    assert logged["rule"] == pg.RULES[line], rid
    assert logged["hit"] and len(logged["hit"]) <= 120, rid
    assert logged["hit"] in " ".join(text.split()), (rid, logged["hit"])


@pytest.mark.parametrize("row", [pytest.param(r, id=r[0]) for r in ALLOW])
def test_allow_row(row, log):
    rid, tool, text, cwd = row
    assert pg.decide(_payload(tool, text, cwd)) is None, rid
    assert pg.guard(_payload(tool, text, cwd)) is None
    assert _lines(log) == []


def test_a47_malformed_payloads(log):
    for bad in ({}, [], None, "x", {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": "x"},
                {"tool_name": "Bash", "tool_input": {"command": 5}}, {"tool_name": "Edit", "tool_input": {}}):
        assert pg.decide(bad) is None
        assert pg.guard(bad) is None
    assert _lines(log) == []


def test_reason_carries_at_most_120_chars_of_the_match(log):
    out = pg.guard(_payload("Bash", "rm -rf " + "x" * 500, MAIN))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) <= len("permission guard, red line 1: ") + 120 + len("; " + pg.REFUSAL_SUFFIX)
    logged = json.loads(_lines(log)[0])
    assert len(logged["text"]) <= 120 and len(logged["hit"]) <= 120


def test_chained_deny_logs_one_row_with_whole_command_and_hit_segment(log):
    cmd = "echo a && git push --force origin main"
    out = pg.guard(_payload("Bash", cmd, MAIN))
    assert out is not None
    rows = _lines(log)
    assert len(rows) == 1
    logged = json.loads(rows[0])
    assert logged["text"] == cmd
    assert logged["hit"] == "git push --force origin main"
    assert logged["line"] == 3 and logged["rule"] == pg.RULES[3]
    assert pg.REFUSAL_SUFFIX not in logged["hit"]


def test_quoted_redirect_and_heredoc_are_not_commands():
    msg = "git commit -m \"fix a > b\" -m 'rm -rf src'"
    assert pg.decide(_payload("Bash", msg, MAIN)) is None
    body = "git commit -m \"$(cat <<'EOF'\ntaskkill /F\nrm -rf src\n> C:/Windows/x\nEOF\n)\""
    assert pg.decide(_payload("Bash", body, MAIN)) is None
    assert pg.decide(_payload("Bash", "gh issue comment 1 --body 'git push --force'", MAIN)) is None
    assert pg.decide(_payload("Bash", "python x.py 2>&1 | tee /dev/null", MAIN)) is None


def test_chained_command_is_read_per_segment():
    assert "red line 5" in pg.decide(_payload("Bash", "echo hi && taskkill /F /PID 1", MAIN))
    assert "red line 3" in pg.decide(_payload("Bash", "cd C:/Coding/poe-junkroan-v2 && git reset --hard", WT))
    assert pg.decide(_payload("Bash", "cd .claude/worktrees/orch-360 && git reset --hard", MAIN)) is None


def _run_hook(payload_text, log):
    env = {**os.environ, "PERMISSION_GUARD_LOG": str(log)}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(HOOK)], input=payload_text, capture_output=True, text=True,
                       timeout=30, env=env)
    return r, time.perf_counter() - t0


@pytest.mark.parametrize("tool,text,line", [
    ("Bash", "git push --force origin main", 3),
    ("PowerShell", "Stop-Process -Name junkroan -Force", 5),
    ("Edit", MAIN + r"\.claude\settings.json", 6),
    ("Write", MAIN + r"\config\orchestration.json", 6),
])
def test_subprocess_denies_one_case_per_tool(tool, text, line, log):
    r, secs = _run_hook(json.dumps(_payload(tool, text, MAIN)), log)
    assert r.returncode == 0, r.stderr
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"red line {line}" in reason
    assert len(_lines(log)) == 1
    print(f"subprocess {tool}: {secs * 1000:.0f} ms")


# the command of the deny of 2026-09-30 17:57:11 (#363), its 4th segment swapped for a current deny row
# (D75): since #361 a copy FROM ~/.claude/settings.json passes, a copy onto it is still line 6
CHAINED_1757 = ("python -m tools.agentops guard check --claude-json tmp_orch/empty_claude.json ; "
                "echo rc=$? ; ls tmp_orch ; cp x.json C:/Users/user/.claude/settings.json ; echo done")


def test_subprocess_logs_the_segment_that_hit_not_the_head(log):
    r, _ = _run_hook(json.dumps(_payload("Bash", CHAINED_1757, MAIN)), log)
    assert r.returncode == 0, r.stderr
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == ("permission guard, red line 6: cp x.json C:/Users/user/.claude/settings.json; "
                      + pg.REFUSAL_SUFFIX)
    rows = _lines(log)
    assert len(rows) == 1
    logged = json.loads(rows[0])
    assert logged["hit"] == "cp x.json C:/Users/user/.claude/settings.json"
    assert logged["line"] == 6 and logged["rule"] == "the harness and secrets"
    assert logged["text"] == CHAINED_1757[:120]  # the head stays as before, it does not hold the hit
    assert logged["hit"] not in logged["text"]


@pytest.mark.parametrize("stdin", ["", "not json", "{}", "[1]", json.dumps(_payload("Bash", "git pull", MAIN))])
def test_subprocess_fails_open_and_passes(stdin, log):
    r, _ = _run_hook(stdin, log)
    assert r.returncode == 0 and r.stdout == ""
    assert _lines(log) == []


def test_decide_over_all_rows_under_50_ms():
    payloads = [_payload(t, x, c) for _, _, t, x, c in DENY] + [_payload(t, x, c) for _, t, x, c in ALLOW] + [{}]
    assert len(payloads) == len(DENY) + len(ALLOW) + 1
    pg.decide(payloads[0])  # warm regex caches
    # #381: the bar is the guard's own cost, not the machine's load. Load only ever adds time, so the minimum of 5
    # timings is the closest reading of the guard itself; a guard that really got slower still fails all 5.
    timings = []
    for _ in range(5):
        t0 = time.perf_counter()
        for p in payloads:
            pg.decide(p)
        timings.append((time.perf_counter() - t0) * 1000)
    ms = min(timings)
    print(f"decide over {len(payloads)} rows: min {ms:.1f} ms of {', '.join(f'{t:.1f}' for t in timings)}")
    # bound = max(floor, factor x a CPU spin timed here): load that slows the spin slows the call (#824)
    bound_ms = bound(0.05, 0.33) * 1000
    assert ms < bound_ms, f"min {ms:.1f} ms, bound {bound_ms:.0f} ms"


# #362: a worktree kept anywhere (told by its `.git` FILE, not by the name `.claude/worktrees`), and a junction in it
def _fs_worktree(tmp_path, monkeypatch):
    # tmp_path lies under %TEMP%, which is itself an exempt area: switch that off so only the worktree rule speaks
    monkeypatch.setattr(pg, "_SCRATCH_RE", re.compile(r"\A\Z"))
    monkeypatch.setattr(pg, "_TEMP_NORMS", ())
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    wt = tmp_path / "trees" / "issue-362"  # not under /.claude/worktrees/
    (wt / "out" / "deep").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: " + str(repo / ".git" / "worktrees" / "issue-362"), encoding="utf-8")
    return repo, wt


def _rm(text, cwd):
    return pg.decide(_payload("Bash", text, str(cwd)))


def test_a_worktree_kept_anywhere_is_told_by_its_git_file(tmp_path, monkeypatch):
    repo, wt = _fs_worktree(tmp_path, monkeypatch)
    assert pg._worktree_root(pg._norm(str(wt / "out" / "deep"))) == pg._norm(str(wt))
    assert pg._worktree_root(pg._norm(str(repo / "src"))) is None  # a repo root keeps a `.git` directory
    assert _rm("rm -rf out", wt) is None
    assert _rm("rm -rf out/deep", wt / "out") is None
    assert _rm(f'rm -rf "{wt}/out"', wt) is None
    assert "red line 1" in _rm("rm -rf .", wt)
    assert "red line 1" in _rm(f'rm -rf "{wt}"', wt)
    assert "red line 1" in _rm(f'rm -rf "{wt}"', repo)
    assert "red line 1" in _rm(f'rm -rf "{repo}"', wt)
    assert "red line 1" in _rm(f'rm -rf "{repo}/src"', wt)
    assert "red line 1" in _rm("rm -rf out", repo)  # the repo root's own `out` is not a worktree's
    assert "red line 1" in _rm("rm -rf .git", wt)


def test_a_junction_in_the_worktree_is_never_deleted_through(tmp_path, monkeypatch):
    repo, wt = _fs_worktree(tmp_path, monkeypatch)
    target = tmp_path / "shared_var"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    link = wt / "var"
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("cannot make a junction here: " + r.stdout + r.stderr)
    else:
        link.symlink_to(target, target_is_directory=True)
    assert "red line 1" in _rm("rm -rf var", wt)
    assert "red line 1" in _rm("rm -rf var/sub", wt)
    assert _rm("rm -rf out", wt) is None
    # a junction below the path named
    inner = wt / "out" / "deep" / "shared"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(inner), str(target)], capture_output=True, text=True)
    else:
        inner.symlink_to(target, target_is_directory=True)
    assert "red line 1" in _rm("rm -rf out", wt)
    assert "red line 1" in _rm("rm -rf out/deep", wt)
    assert (target / "keep.txt").exists()
    # drop the links the way docs/agents/machine.md says, non-recursive, so the test folder cleans up
    for lk in (link, inner):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rmdir", str(lk)], capture_output=True)
        else:
            lk.unlink()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows")
def test_449_rmdir_del_remove_item_on_a_real_junction_are_refused(tmp_path, monkeypatch):
    repo, wt = _fs_worktree(tmp_path, monkeypatch)
    target = tmp_path / "shared_var"
    target.mkdir()
    link = wt / "var"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("cannot make a junction here: " + r.stdout + r.stderr)
    (target / "somefile.txt").write_text("x", encoding="utf-8")
    plain = wt / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x", encoding="utf-8")
    try:
        rmdir = pg.decide(_payload("Bash", f'cmd /c rmdir "{link}"', str(wt)))
        dele = pg.decide(_payload("Bash", f'cmd /c del "{link}"', str(wt)))
        ri = pg.decide(_payload("PowerShell", f'Remove-Item "{link}"', str(wt)))
        print("decide():", rmdir, dele, ri)
        assert rmdir is None  # removes the link only
        assert dele and dele.startswith("permission guard, red line 1") and "cmd /c rmdir" in dele
        assert ri and ri.startswith("permission guard, red line 1")
        assert "red line 1" in pg.decide(_payload("Bash", f'cmd /c erase "{link}"', str(wt)))
        assert "red line 1" in pg.decide(_payload("Bash", f'cmd /c del "{link}/somefile.txt"', str(wt)))
        assert "red line 1" in pg.decide(_payload("PowerShell", "Remove-Item var/somefile.txt", str(wt)))
        assert "red line 1" in pg.decide(_payload("PowerShell", "ri var -Recurse", str(wt)))
        # a plain directory in the worktree keeps working as before
        assert pg.decide(_payload("PowerShell", f'Remove-Item "{plain}"', str(wt))) is None
        assert pg.decide(_payload("Bash", f'cmd /c del "{plain}\f.txt"', str(wt))) is None
    finally:
        os.rmdir(link)  # non-recursive, never through the junction


# #409: line 2 counts only the destination of a copy; a move still counts its source
_COPY_OUT = [
    ("c409_1", "Bash", 'cp "C:/Users/user/Downloads/postback (1).csv" .runtime/jimpartners/exports/x.csv'),
    ("c409_2", "Bash", "cp /c/Users/user/Downloads/a.csv .runtime/x/"),
    ("c409_3", "PowerShell", r'Copy-Item "$HOME\Downloads\a.csv" .runtime\x\ '.strip()),
]


@pytest.mark.parametrize("row", [pytest.param(r, id=r[0]) for r in _COPY_OUT])
def test_copy_out_of_downloads_is_allowed(row, log):
    rid, tool, text = row
    assert pg.decide(_payload(tool, text, MAIN)) is None, rid


@pytest.mark.parametrize("rid,text", [
    ("c409_4", "mv ~/Downloads/a.csv .runtime/x/"),
    ("c409_5", "cp a.csv ~/Downloads/b.csv"),
])
def test_move_from_or_copy_into_downloads_is_refused_by_the_machine_rule(rid, text, log):
    out = pg.guard(_payload("Bash", text, MAIN))
    assert out is not None, rid
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("permission guard, red line 2:"), rid
    assert json.loads(_lines(log)[0])["rule"] == pg.RULES[2], rid


# #465 row 0: measured before the fix, `decide()` on `cmd /c rmdir /s /q <protected>` returned BEFORE_VERDICT
# (see the test name); after the fix red line 1 refuses it, and so do the other cmd wrappers.
_PROT = MAIN + r"\vendor"


def test_cmd_c_rmdir_s_was_allowed_before_465_now_refused_by_line_1():
    v = pg.decide(_payload("Bash", f"cmd /c rmdir /s /q {_PROT}", MAIN))
    assert v and v.startswith("permission guard, red line 1")


@pytest.mark.parametrize("text", [
    f"cmd /c rmdir /s /q {_PROT}",
    f"cmd.exe /c rd /s /q {_PROT}",
    f"cmd /k rmdir /s {_PROT}",
    f'cmd /c "rmdir /s /q {_PROT}"',
    f"CMD.EXE /C rmdir /S /Q {_PROT}",
])
def test_465_cmd_wrapped_recursive_delete_is_refused_by_line_1(text):
    v = pg.decide(_payload("Bash", text, MAIN))
    assert v and v.startswith("permission guard, red line 1")


def test_465_cmd_dir_stays_allowed():
    assert pg.decide(_payload("Bash", "cmd /c dir", MAIN)) is None


# #99: the delete guard unwraps `bash|sh [-l]c`, `wsl [options] [--] <cmd>`, `pwsh|powershell -c|-Command` (and `cmd`),
# recursively, and judges the inner command with the rules of a bare one; inside wsl a path is a Linux path.
_REAL = "C:/Coding/x"
_SCRATCH = "C:/Users/user/AppData/Local/Temp/claude/x/scratchpad/r"
_WRAP = {
    "bash -c": 'bash -c "rm -rf {p}"',
    "bash -lc": 'bash -lc "rm -rf {p}"',
    "sh -c": "sh -c 'rm -rf {p}'",
    "pwsh -Command": "pwsh -Command Remove-Item -Recurse {p}",
    "powershell -c": 'powershell -c "Remove-Item -Recurse {p}"',
}


def _verdict(text, cwd=MAIN):
    return pg.decide(_payload("Bash", text, cwd))


@pytest.mark.parametrize("name", list(_WRAP))
def test_99_wrapped_delete_of_a_real_folder_is_refused(name):
    v = _verdict(_WRAP[name].format(p=_REAL))
    assert v and v.startswith("permission guard, red line 1"), (name, v)


def test_99_wsl_bash_lc_maps_mnt_c_to_the_windows_path_and_refuses():
    v = _verdict('wsl bash -lc "rm -rf /mnt/c/Coding/x"')
    assert v and v.startswith("permission guard, red line 1"), v


@pytest.mark.parametrize("name", list(_WRAP))
@pytest.mark.parametrize("p", [_SCRATCH, "/tmp/x"])
def test_99_wrapped_delete_of_the_scratch_passes(name, p):
    assert _verdict(_WRAP[name].format(p=p)) is None, (name, p)


@pytest.mark.parametrize("text", [
    'wsl bash -lc "rm -rf /tmp/x"',
    'wsl bash -lc "rm -rf /mnt/c/Users/user/AppData/Local/Temp/claude/x/scratchpad/r"',
    "wsl rm -rf /tmp/x",
    "wsl -d Ubuntu -- rm -rf /tmp/x",
    "wsl -e rm -rf /tmp/x",
])
def test_99_wsl_scratch_passes(text):
    assert _verdict(text) is None, text


@pytest.mark.parametrize("text", [
    'wsl bash -lc "rm -rf /var/x"',
    'wsl bash -lc "rm -rf /home/user/x"',
    "wsl rm -rf /var/x",
    "wsl -d Ubuntu -u root -- rm -rf /var/x",
    "wsl --exec rm -rf /var/x",
    'wsl bash -lc "rm -rf /c/Coding/x"',  # /c is a Linux folder in wsl, not C:
    'wsl bash -lc "rm -rf /tmp"',
    "wsl rm -rf /mnt/c/Coding/x",
    'wsl.exe bash -lc "cd /tmp && rm -rf /var/x"',
])
def test_99_wsl_non_throwaway_linux_path_is_refused(text):
    v = _verdict(text)
    assert v and v.startswith("permission guard, red line 1"), (text, v)


@pytest.mark.parametrize("text", [
    f'cmd /c bash -c "rm -rf {_REAL}"',
    f"cmd /c wsl bash -lc 'rm -rf /mnt/c/Coding/x'",
    f"bash -c 'bash -c \"rm -rf {_REAL}\"'",
    f'bash -c "sh -c \\"rm -rf {_REAL}\\""',
    f'bash -c "cd /tmp && rm -rf {_REAL}"',
    f'bash -c "echo hi; rm -rf {_REAL}"',
    f'bash -c "git status | cat; powershell -c \\"Remove-Item -Recurse {_REAL}\\""',
    f'bash --login -c "rm -rf {_REAL}"',
    f'bash -o pipefail -c "rm -rf {_REAL}"',
    f'powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -Recurse -Force {_REAL}"',
    f'pwsh -NoLogo -NoProfile -c "Remove-Item {_REAL} -Recurse"',
    f'bash -c "rm -rf C:/Coding/x" extra0 extra1',
])
def test_99_nested_and_option_variants_are_refused(text):
    v = _verdict(text)
    assert v and v.startswith("permission guard, red line 1"), (text, v)


def test_99_inner_command_gets_every_red_line_not_only_the_delete():
    assert "red line 5" in _verdict('bash -c "taskkill /F /PID 1"')
    assert "red line 3" in _verdict("wsl bash -lc 'git push --force origin main'")
    assert "red line 2" in _verdict('sh -c "rm C:/Windows/x"')


@pytest.mark.parametrize("text", [
    "bash -c 'echo \"it; rm -rf /tmp/y'",  # inner quote never closes
    'bash -c "rm -rf /tmp/y',  # the wrapper's own quote never closes
    'wsl bash -lc "echo \'x; rm -rf /tmp/y"',
    "powershell -c \"Remove-Item 'x -Recurse /tmp/y\"",
])
def test_99_unparsable_wrapper_holding_a_delete_word_is_refused_naming_the_wrapper(text):
    v = _verdict(text)
    assert v and v.startswith("permission guard, red line 1"), (text, v)
    assert "wrapper" in v and re.search(r"bash|wsl|powershell", v), v


def test_99_unparsable_wrapper_without_a_delete_word_passes():
    assert _verdict('bash -c "echo hi') is None
    assert _verdict("bash -c 'echo it\"s fine'") is None


@pytest.mark.parametrize("text", [
    f"echo bash -c rm -rf {_REAL}",
    f'echo "bash -c \'rm -rf {_REAL}\'"',
    f'git commit -m "bash -c rm -rf {_REAL}"',
    f'git commit -m "wsl bash -lc \'rm -rf /var/x\'"',
    f"grep -rn 'bash -c' tools",
    f"grep -rn 'powershell -c Remove-Item -Recurse {_REAL}' tools",
    "gh issue comment 99 --body 'sh -c \"rm -rf /var/x\"'",
    "python -m pytest tests/test_x.py -k 'bash -c'",
    "bash script.sh",
    "bash",
    "sh",
    "wsl",
    "wsl --list --verbose",
    "wsl --shutdown",
    "wsl ls /var/x",
    "wsl -d Ubuntu -- ls /var",
    'bash -c "ls /var; cat /etc/hosts"',
    'bash -c "python -m pytest tests -q"',
    'bash -c "echo rm -rf /var/x"',
    'bash -c "rm -f /var/x"',  # not recursive: a bare `rm -f` passes too
    'wsl bash -lc "rm /var/x.txt"',
    "powershell -File run.ps1",
    'pwsh -NoProfile -Command "Get-ChildItem C:/Coding -Recurse"',
    'powershell -c "Write-Output \'Remove-Item -Recurse C:/Coding/x\'"',
    "cmd /c dir",
    'bash -c "cd C:/Coding && ls"',
])
def test_99_wrapper_words_as_arguments_or_harmless_inner_commands_pass(text):
    assert _verdict(text) is None, text


def test_99_wrapper_depth_is_bounded():
    text = 'rm -rf C:/Coding/x'
    for _ in range(12):
        text = 'bash -c "' + text.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"') + '"'
    v = _verdict(text)
    assert v and v.startswith("permission guard, red line 1"), v


def test_99_deny_logs_the_whole_wrapper_as_the_hit(log):
    text = 'bash -lc "rm -rf C:/Coding/x"'
    out = pg.guard(_payload("Bash", text, MAIN))
    assert out is not None
    row = json.loads(_lines(log)[0])
    assert row["line"] == 1 and row["rule"] == pg.RULES[1] and row["hit"] in text


@pytest.mark.parametrize("text", [
    "wsl bash -lc 'rm -rf $TMPDIR/x'",  # a Linux variable is not the Windows temp
    "wsl bash -lc 'rm -rf ~/x'",
    "wsl rm -rf $HOME/x",
])
def test_99_wsl_variables_are_not_windows_variables(text):
    v = _verdict(text)
    assert v and v.startswith("permission guard, red line 1"), (text, v)
