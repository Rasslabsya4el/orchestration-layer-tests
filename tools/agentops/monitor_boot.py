"""#340 unit 1: the monitor server starts at logon, with no console window (Windows Task Scheduler).

`install()` registers one task that runs `pythonw -m tools.agentops.monitor_server` at logon, from the repo folder; the
same shape as `keepalive.py` (a `/XML` task, `pythonw` beside the interpreter, `Hidden`), with a logon trigger only.
The action is the windowless interpreter: `detach.py` (its docstring) starts its own children the same way, and
`scripts/launch_hidden.ps1` goes through WMI, which this unit does not use. `uninstall()` removes it, `status()` says
whether it is there. Every call to `schtasks` goes through `runner`, injected (a fake in every test); no test installs
the real task. Windows only; elsewhere every call says so and changes nothing.

Every `schtasks` call is bounded by SCHTASKS_TIMEOUT_S; a call that does not answer in time is an error line of its
verb, never a traceback. The CLI: `monitor-boot install | status | uninstall` (`add_subparser`, `run_command`).

The task never ends by itself: `ExecutionTimeLimit` is `PT0S` (Task Scheduler's default stops a task after 72 hours).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from tools.agentops import core_settings
from tools.agentops.util import no_window_flags

ROOT = core_settings.ROOT
TASK_NAME_PREFIX = "agentops-monitor"
MODULE = "tools.agentops.monitor_server"
SCHEDULE_DESCRIPTION = "at logon"
#: the longest one `schtasks` call may take; past it the verb's answer is an error line, not a hang
SCHTASKS_TIMEOUT_S = 60

Runner = Callable[[list], subprocess.CompletedProcess]


class _SchtasksTimeout(Exception):
    """A `schtasks` call that did not answer within SCHTASKS_TIMEOUT_S; its message names the verb."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_only(verb: str) -> str | None:
    """`None` on Windows; else the refusal line of `monitor-boot <verb>` (such a call changes nothing)."""
    if _is_windows():
        return None
    return f"monitor boot {verb}: Windows only (schtasks)"


def task_name(root: Path = ROOT) -> str:
    return f"{TASK_NAME_PREFIX}-{root.name}"


def _default_runner(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, creationflags=no_window_flags(),
                          timeout=SCHTASKS_TIMEOUT_S)


def _schtasks(runner: Runner, args: list, verb: str) -> subprocess.CompletedProcess:
    """One `schtasks` call; a timeout raises `_SchtasksTimeout`, whose line names the verb."""
    try:
        return runner(args)
    except subprocess.TimeoutExpired as exc:
        raise _SchtasksTimeout(f"monitor boot {verb}: schtasks timed out after {SCHTASKS_TIMEOUT_S} s") from exc


def _pythonw(python_exe: str | None = None) -> str:
    """`pythonw.exe` beside the running interpreter when it exists, else the interpreter itself."""
    exe = Path(python_exe or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(exe)


def _task_xml(exe: str, arguments: str, working_dir: str) -> str:
    """A logon trigger only. `exe` (the `Command`, quoted) and `arguments` (the `Arguments`) are two elements, as
    issue #238 measured a glued command line never launches (see keepalive's `_task_xml`)."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _create(name: str, exe: str, arguments: str, working_dir: str, runner: Runner) -> subprocess.CompletedProcess:
    """One `schtasks /Create` with a temp XML file, deleted again whether the call succeeds or not."""
    xml = _task_xml(exe, arguments, working_dir)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
        fh.write(xml)
        xml_path = fh.name
    try:
        return _schtasks(runner, ["schtasks", "/Create", "/TN", name, "/XML", xml_path, "/F"], "install")
    finally:
        Path(xml_path).unlink(missing_ok=True)


def install(*, root: Path = ROOT, runner: Runner | None = None, python_exe: str | None = None) -> tuple[bool, str]:
    """Registers the logon task; `(ok, message)`."""
    refusal = _windows_only("install")
    if refusal:
        return False, refusal
    runner = runner or _default_runner
    name = task_name(root)
    exe = f'"{_pythonw(python_exe)}"'
    try:
        result = _create(name, exe, f"-m {MODULE}", str(root), runner)
    except _SchtasksTimeout as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, f"installed {name} ({SCHEDULE_DESCRIPTION})"
    refusal = (result.stderr or result.stdout or "").strip()
    return False, f"monitor boot install: schtasks refused ({result.returncode}): {refusal}"


def status(*, root: Path = ROOT, runner: Runner | None = None) -> tuple[int, str]:
    """`(count, message)`: count is 1 when the task is registered, else 0."""
    refusal = _windows_only("status")
    if refusal:
        return 0, refusal
    runner = runner or _default_runner
    name = task_name(root)
    try:
        result = _schtasks(runner, ["schtasks", "/Query", "/TN", name, "/XML"], "status")
    except _SchtasksTimeout as exc:
        return 0, str(exc)
    if result.returncode != 0:
        return 0, "0 tasks"
    return 1, f"1 task: {name} ({SCHEDULE_DESCRIPTION})"


def uninstall(*, root: Path = ROOT, runner: Runner | None = None) -> tuple[bool, str]:
    """Removes the task; `(ok, message)`."""
    refusal = _windows_only("uninstall")
    if refusal:
        return False, refusal
    runner = runner or _default_runner
    name = task_name(root)
    try:
        result = _schtasks(runner, ["schtasks", "/Delete", "/TN", name, "/F"], "uninstall")
    except _SchtasksTimeout as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, f"monitor boot uninstall: schtasks refused ({result.returncode}): {result.stderr or result.stdout}"
    return True, f"removed {name}"


# -- CLI ------------------------------------------------------------------------------------------

def add_subparser(sub) -> None:
    boot = sub.add_parser("monitor-boot", help="the monitor server's logon task (#340): install | status | uninstall")
    boot_sub = boot.add_subparsers(dest="monitor_boot_command", required=True)
    boot_sub.add_parser("install", help="register the logon task that runs the monitor server")
    boot_sub.add_parser("status", help="show whether the logon task is registered")
    boot_sub.add_parser("uninstall", help="remove the logon task")


def run_command(args) -> int:
    if args.monitor_boot_command == "install":
        ok, message = install()
        print(message)
        return 0 if ok else 1
    if args.monitor_boot_command == "uninstall":
        ok, message = uninstall()
        print(message)
        return 0 if ok else 1
    if args.monitor_boot_command == "status":
        count, message = status()
        print(message)
        return 0 if count == 1 else 1
    return 2


__all__ = [
    "TASK_NAME_PREFIX",
    "MODULE",
    "SCHEDULE_DESCRIPTION",
    "SCHTASKS_TIMEOUT_S",
    "task_name",
    "install",
    "status",
    "uninstall",
    "add_subparser",
    "run_command",
]
