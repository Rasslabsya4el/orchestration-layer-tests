"""#280 unit 1 (own-out): the launched dispatcher opens `dispatch.out` itself.

Row 1: the dispatcher is launched with no `>>` redirect by cmd; while it runs, a second
`open(out, "a")` from another process succeeds and its line is in the file.
Row 2: a line the dispatcher prints to stdout (or writes to stderr) is in the file within 2 s
while the dispatcher still runs.

The launch is the production `_launch_detached`. Only its WMI transport (`_launch_via_wmi`'s
`powershell` call) is swapped for a hidden `Popen` of the same command line, so no test starts a
console program through WMI (#261). The scratch script stands in for the dispatcher and calls
`_attach_own_out` the way the real `cli.py dispatch` does. Windows only.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the dispatcher's own out is Windows-only (#280)")

#: the scratch "dispatcher": attaches its own out file, then prints a ready line, waits for the GO
#: file, prints a stdout line and a stderr line, and waits for the STOP file. Every wait is bounded.
_SCRIPT = '''
import sys, time
from pathlib import Path
sys.path.insert(0, @ROOT@)
from tools.agentops.dispatch import _attach_own_out
_attach_own_out(sys.argv[sys.argv.index("--own-out") + 1])
GO = Path(@GO@)
STOP = Path(@STOP@)

def wait_for(path, limit):
    end = time.monotonic() + limit
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.02)

print("ready-line", flush=True)
wait_for(GO, 60)
print("stdout-line", flush=True)
sys.stderr.write("stderr-line\\n")
wait_for(STOP, 60)
'''


def _script_text(tmp_path: Path) -> tuple[str, Path, Path]:
    go = tmp_path / "go.flag"
    stop = tmp_path / "stop.flag"
    text = (_SCRIPT.replace("@ROOT@", repr(str(ROOT)))
            .replace("@GO@", repr(str(go)))
            .replace("@STOP@", repr(str(stop))))
    return text, go, stop


def _wait_for_text(path: Path, needle: str, limit: float) -> bool:
    """Polls `path` for `needle` until `limit` seconds pass; True when it is found."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
            return True
        time.sleep(0.05)
    return False


def _start_dispatcher(tmp_path: Path, monkeypatch):
    """Launches the scratch dispatcher through the production `_launch_detached(own_out=True)`.
    Returns (process, out_path, go_file, stop_file, recorded command lines)."""
    from tools.agentops import dispatch

    text, go, stop = _script_text(tmp_path)
    script = tmp_path / "scratch_dispatcher.py"
    script.write_text(text, encoding="utf-8")
    out = tmp_path / "artifacts" / "runs" / "dispatch.out"
    started: list[subprocess.Popen] = []
    commands: list[str] = []

    def hidden_start(command_line, cwd, *, run=None, timeout=60, env=None):
        commands.append(command_line)
        proc = subprocess.Popen(
            command_line, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        started.append(proc)
        return proc.pid

    monkeypatch.setattr(dispatch, "_launch_via_wmi", hidden_start)
    pid = dispatch._launch_detached([str(script)], cwd=tmp_path, out_path=out,
                                    python_exe=sys.executable, env={}, own_out=True)
    assert pid == started[0].pid
    return started[0], out, go, stop, commands


def _stop_dispatcher(proc: subprocess.Popen, stop: Path) -> None:
    """Lets the scratch script end by its own STOP file; kills the scratch process if it does not."""
    stop.touch()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_row1_launch_has_no_cmd_redirect_and_the_dispatcher_out_is_open_while_it_runs(tmp_path: Path, monkeypatch):
    """Row 1: cmd carries no `>>` for the dispatcher; while it runs the test process opens the
    same file in append mode, and that line is in the file."""
    proc, out, go, stop, commands = _start_dispatcher(tmp_path, monkeypatch)
    try:
        assert ">>" not in commands[0], "the dispatcher is launched with no cmd `>>` redirect"
        assert "--own-out" in commands[0]
        assert _wait_for_text(out, "ready-line", limit=60), "the dispatcher never wrote its ready line"
        assert proc.poll() is None, "the dispatcher must still be running"

        with open(out, "a", encoding="utf-8") as handle:
            handle.write("test-process-line-row1\n")

        assert "test-process-line-row1" in out.read_text(encoding="utf-8")
        assert proc.poll() is None, "the dispatcher must still be running after the second open"
    finally:
        _stop_dispatcher(proc, stop)


def test_row2_stdout_and_stderr_lines_reach_the_file_within_2_s_while_it_runs(tmp_path: Path, monkeypatch):
    """Row 2: a line the dispatcher prints to stdout, and one it writes to stderr, are in the file
    within 2 s of the moment the test releases the dispatcher to print them, while it still runs."""
    proc, out, go, stop, _commands = _start_dispatcher(tmp_path, monkeypatch)
    try:
        assert _wait_for_text(out, "ready-line", limit=60), "the dispatcher never wrote its ready line"
        go.touch()
        assert _wait_for_text(out, "stdout-line", limit=2.0), "stdout line not in dispatch.out within 2 s"
        assert _wait_for_text(out, "stderr-line", limit=2.0), "stderr line not in dispatch.out within 2 s"
        assert proc.poll() is None, "the dispatcher must still be running when its lines arrive"
    finally:
        _stop_dispatcher(proc, stop)
