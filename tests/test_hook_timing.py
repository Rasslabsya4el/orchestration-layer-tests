"""#461 unit 1: `agentops hook-timing` times every hook a settings file registers, the way Claude Code runs it.

The seam is `tools.agentops.hook_timing.main(argv)` (the command's whole behaviour: read the settings, start each
tool kind's hooks at once, report, exit 1 over the bound). Hooks here are throwaway scripts in a temp project; N = 3
and small payloads keep each run short. The real 3 x / 5 x bounds are used everywhere: a group over the bound is
measured once more inside the command, so one machine hiccup does not decide a pass.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from tools.agentops import hook_timing

ROOT = Path(__file__).resolve().parents[1]
PY = shlex.quote(Path(sys.executable).as_posix())

FAST = "import sys\nsys.stdin.read()\n"
SLOW = "import sys, time\nsys.stdin.read()\ntime.sleep(1)\n"
EXIT_ONE = "import sys\nsys.stdin.read()\nraise SystemExit(1)\n"
MISSING_IMPORT = "import sys\nsys.stdin.read()\nimport no_such_module_for_461\n"
TRACEBACK_EXIT_TWO = ("import sys, traceback\nsys.stdin.read()\n"
                      "try:\n    raise RuntimeError('boom')\nexcept RuntimeError:\n    traceback.print_exc()\n    sys.exit(2)\n")
DENY = "import sys\nsys.stdin.read()\nsys.exit(2)\n"


def _project(tmp_path: Path, hooks: dict[str, tuple], python: str = PY) -> Path:
    """A temp project whose settings register `hooks` = {script name: (matcher, script body[, command template])}
    under PreToolUse; the template's `{py}` is the interpreter word and `{script}` the script's quoted path."""
    (tmp_path / ".claude").mkdir()
    entries = []
    for name, (matcher, body, *template) in hooks.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
        command = (template[0] if template else "{py} {script}").format(py=python, script=f'"$CLAUDE_PROJECT_DIR/{name}"')
        entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": entries}}), encoding="utf-8")
    return settings


def _bare_median(out: str) -> float:
    return float(out.split("bare start median ")[1].split(" ")[0])


def test_a_hook_that_sleeps_one_second_fails_the_check_and_is_named(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST), "sleeping_hook.py": ("Bash", SLOW)})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 1, out
    failing = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert len(failing) == 1 and failing[0].startswith("FAIL Bash PreToolUse"), failing
    assert "sleeping_hook.py" in failing[0] and "fast_hook.py" not in failing[0], failing
    assert "re-measured once" in out  # the bound held in the second pass too


def test_fast_hooks_pass_the_real_bound_and_the_report_has_median_p95_and_the_bare_start(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash|Edit|Read", FAST)})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "bare" in out and "median" in out and "p95" in out and "fast_hook.py" in out
    assert not any(line.startswith("FAIL") for line in out.splitlines())


def test_a_slow_hook_whose_matcher_does_not_take_the_three_kinds_is_not_started(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST), "agent_only.py": ("Agent|Task", SLOW)})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "agent_only.py" not in out


def test_hooks_of_a_kind_are_those_whose_matcher_takes_it_in_the_repos_own_settings():
    settings = ROOT / ".claude" / "settings.json"
    hooks = hook_timing.read_hooks(settings)
    bash = {h.script for h in hooks if h.event == "PreToolUse" and h.takes("Bash")}
    read = {h.script for h in hooks if h.event == "PreToolUse" and h.takes("Read")}
    assert {"permission_guard.py", "value_text_guard.py", "tracker_guard.py"} <= bash
    assert "permission_guard.py" not in read and "value_text_guard.py" in read
    assert any(h.event == "PostToolUse" and h.takes("Bash") for h in hooks)
    assert any(h.event == "PermissionRequest" and h.takes("Bash") for h in hooks)


def test_the_subcommand_is_registered_in_the_agentops_cli(tmp_path, capsys):
    from tools.agentops import cli
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST)})
    assert cli.main(["hook-timing", "--settings", str(settings), "--runs", "3"]) == 0
    assert "fast_hook.py" in capsys.readouterr().out


# -- the bare start is the hook's own command line, with its script swapped for an empty program ------------------

def test_the_bare_start_keeps_the_first_word_the_prefix_and_the_tail_and_swaps_only_the_script(tmp_path):
    empty = shlex.quote((tmp_path / "empty.py").as_posix())
    empty_js = shlex.quote((tmp_path / "empty.js").as_posix())
    cases = {
        'python "$CLAUDE_PROJECT_DIR/tools/hooks/x.py" rules || exit 2': f"python {empty} || exit 2",
        '$CLAUDE_PROJECT_DIR/.venv/bin/python "$CLAUDE_PROJECT_DIR/tools/hooks/x.py"':
            f"$CLAUDE_PROJECT_DIR/.venv/bin/python {empty}",
        'cd "$CLAUDE_PROJECT_DIR" && python x/y.py --flag a': f'cd "$CLAUDE_PROJECT_DIR" && python {empty}',
        '"C:\\Python312\\python.exe" "$CLAUDE_PROJECT_DIR/a.py"': f'"C:\\Python312\\python.exe" {empty}',
        "FOO=1 node hooks/a.js; echo done": f"FOO=1 node {empty_js} ; echo done",
    }
    for command, want in cases.items():
        assert hook_timing.bare_command(command, tmp_path) == want, command


def test_a_first_word_that_is_an_env_var_path_gets_a_real_bare_start(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOOK_TIMING_PY", Path(sys.executable).as_posix())
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST, '"$HOOK_TIMING_PY" {script}')})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert _bare_median(out) > 10, out  # the quoted-literal bug ran `'$HOOK_TIMING_PY' -c pass`: not found, a few ms


def test_a_first_word_behind_a_cd_prefix_is_measured_like_the_hook_runs_it(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST, 'cd "$CLAUDE_PROJECT_DIR" && {py} {script}')})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert _bare_median(out) > 10, out


@pytest.mark.skipif(os.name != "nt", reason="a Windows-style interpreter path")
def test_a_windows_style_first_word_gets_a_real_bare_start(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST, '"' + sys.executable + '" {script}')})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert _bare_median(out) > 10, out


def test_a_bare_start_that_cannot_run_is_an_error_not_a_tiny_baseline(tmp_path, capsys):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST, '"$NO_SUCH_INTERPRETER_461" {script}')})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    assert code == 2
    assert "bare start" in capsys.readouterr().err


# -- a hook that crashes or hangs does not pass ----------------------------------------------------------------

@pytest.mark.parametrize("name, body", [("exits_one_hook.py", EXIT_ONE), ("missing_import_hook.py", MISSING_IMPORT),
                                        ("traceback_exit_two_hook.py", TRACEBACK_EXIT_TWO)])
def test_a_hook_that_crashes_at_once_fails_the_check_and_is_named(tmp_path, capsys, name, body):
    settings = _project(tmp_path, {"fast_hook.py": ("Bash", FAST), name: ("Bash", body)})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    out = capsys.readouterr().out
    assert code == 1, out
    failing = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert len(failing) == 1 and name in failing[0] and "fast_hook.py" not in failing[0], out


def test_a_hook_that_denies_with_exit_two_is_a_decision_not_a_crash(tmp_path, capsys):
    settings = _project(tmp_path, {"deny_hook.py": ("Bash", DENY)})
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    assert code == 0, capsys.readouterr().out


def test_a_hook_past_the_timeout_is_an_error_naming_it_and_its_process_tree_is_killed(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hook_timing, "HOOK_TIMEOUT_S", 1)
    pid_file = tmp_path / "grandchild.pid"
    body = ("import subprocess, sys, time\nsys.stdin.read()\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\ntime.sleep(120)\n")
    settings = _project(tmp_path, {"hanging_hook.py": ("Bash", body)})
    start = time.monotonic()
    code = hook_timing.main(["--settings", str(settings), "--runs", "3"])
    took = time.monotonic() - start
    out = capsys.readouterr().out
    assert code == 1 and out.startswith("FAIL") and "hanging_hook.py" in out and "did not finish" in out, out
    assert took < 30, took  # bounded: the wait, the kill and the reap
    try:
        import psutil
    except ImportError:
        return
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not psutil.pid_exists(pid), "the hook's child outlived the kill"
