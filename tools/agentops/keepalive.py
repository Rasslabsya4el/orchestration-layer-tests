"""`python -m tools.agentops keepalive install | remove | status` (issue #201): a scheduled task
of the host that runs `dispatch --ensure` every 5 min and at logon, with no window, so the
dispatcher comes back on its own after a reboot, a crash or a logoff -- nobody has to remember to
restart it. Windows only (`schtasks`); on another platform every subcommand says so and exits
non-zero.

The task name is derived from the repo directory (`task_name`), so two checkouts on one host never
collide. `schtasks` is never called directly by this module's tests or by a worker: `runner` is
injected everywhere a real `schtasks` call would happen, so a fake stands in for the host's task
scheduler in every test. Stdlib only, like every module the core ships with.

A user who is not a Windows administrator can register the clock trigger but not the logon one
(issue #230): `schtasks /Create` answers "Access is denied" for a task whose XML holds a
`LogonTrigger`, measured on this host with two real, non-elevated probe calls of the module's
own shape (one task with only `/SC ONLOGON`, refused this way; one with only `/SC MINUTE`, not).
`install` tries both triggers first and, only on that exact refusal, retries with the clock
trigger alone, still exits 0, and names in its message what it left out and why. A refusal for any
other reason still fails `install`, as before.

The action's executable and its arguments are two separate XML elements, `Command` and
`Arguments` (issue #238): an `Exec` action whose `Command` holds both glued into one string never
launches on this host -- the task sits `Running` forever with no process to show for it, and every
later run is refused. See `_task_xml`'s own docstring for the measured evidence and the Microsoft
docs it rests on.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .util import no_window_flags
from .worktree import ROOT

TASK_NAME_PREFIX = "agentops-dispatch"
#: the schedule every installed task carries; fixed, so `status` never has to parse it back out of
#: a real `schtasks` query -- this module only ever creates one shape of task.
SCHEDULE_DESCRIPTION = "every 5 min and at logon"
#: the schedule left when the logon trigger is refused to a non-administrator (issue #230).
SCHEDULE_DESCRIPTION_CLOCK_ONLY = "every 5 min"
#: the exact refusal `install` treats as "this user may not register a logon trigger", not a
#: generic `schtasks` failure -- matched case-insensitively against stdout/stderr together.
ACCESS_DENIED = "access is denied"

Runner = Callable[[list], subprocess.CompletedProcess]


class KeepaliveError(RuntimeError):
    """Not on Windows, or `schtasks` itself refused."""


def task_name(root: Path = ROOT) -> str:
    return f"{TASK_NAME_PREFIX}-{root.name}"


def _default_runner(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, creationflags=no_window_flags())


def _pythonw(python_exe: str | None = None) -> str:
    """No console window: `pythonw.exe` beside the running interpreter when it exists, else the
    interpreter itself (a visible window is the worse fallback, never a crash)."""
    exe = Path(python_exe or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(exe)


def _task_xml(exe: str, arguments: str, working_dir: str, *, include_logon: bool = True) -> str:
    """A time trigger repeating every 5 min without end, plus a logon trigger by default -- the two
    triggers `schtasks /Create` cannot both take on its own command line, only through `/XML`.
    `include_logon=False` (issue #230) drops the `LogonTrigger` block for the fallback registered
    when Windows refuses it to a non-administrator.

    `exe` and `arguments` are two separate XML elements (issue #238), never one string glued
    together: measured on this host, an `<Exec>` action whose `<Command>` holds the executable
    *and* its arguments in one string, with no `<Arguments>` element, never launches at all --
    `schtasks /Run` leaves the task `Running` (result 267009) forever, no process of that name
    ever appears in the process table, and a later run is refused (`MultipleInstancesPolicy
    IgnoreNew` sees the never-finished instance as still live) -- while the exact same command
    split into `<Command>` (the executable alone) and `<Arguments>` (the rest) starts and finishes
    normally (`Ready`, result 0) within seconds. Microsoft's own docs draw the same line: the
    `Command` element is "the path to the executable file" (`ExecAction.Path`), and "the arguments
    associated with the command-line operation" belong in the separate `Arguments` element
    (`ExecAction.Arguments`) -- https://learn.microsoft.com/en-us/windows/win32/taskschd/execaction-path,
    https://learn.microsoft.com/en-us/windows/win32/taskschd/execaction-arguments."""
    logon_trigger = """
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>""" if include_logon else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>{logon_trigger}
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _looks_glued(exe: str, arguments: str) -> bool:
    """True when `exe` (the XML `Command` element) appears to hold not just the executable path
    but its arguments glued on too, with `arguments` (the XML `Arguments` element) empty -- the
    exact shape issue #238 measured never launches at all (`_task_xml`'s own docstring): a
    `<Command>` holding the executable *and* its arguments in one string, no `<Arguments>`."""
    if arguments.strip():
        return False  # split correctly: Arguments carries something of its own
    stripped = exe.strip()
    if stripped.startswith('"'):
        end = stripped.find('"', 1)
        if end == -1:
            return True  # unterminated quote: malformed, refuse rather than guess
        return bool(stripped[end + 1:].strip())
    return " " in stripped


def _validate_split_action(exe: str, arguments: str) -> None:
    """#246 acceptance row 6: refuse a task action that is not split into `Command` (the
    executable alone) and `Arguments` (everything else) -- before `schtasks` is ever called, not
    after. Raises `KeepaliveError`, never silently registers the glued shape #238 found dead."""
    if _looks_glued(exe, arguments):
        raise KeepaliveError(
            f"keepalive: refusing to register a task whose Command holds a glued command line "
            f"with no separate Arguments (Command={exe!r}, Arguments={arguments!r}) -- issue "
            f"#238 measured this shape never launches (schtasks leaves it Running forever); "
            f"split the executable and its arguments into two separate values first"
        )


def _create(name: str, exe: str, arguments: str, working_dir: str, runner: Runner, *,
           include_logon: bool) -> subprocess.CompletedProcess:
    """One `schtasks /Create` call with a fresh temp XML, deleted again whether it succeeds or not.
    Validates the action is split into `Command`/`Arguments` (#246 row 6) before `runner` -- and
    so `schtasks` -- is ever invoked."""
    _validate_split_action(exe, arguments)
    xml = _task_xml(exe, arguments, working_dir, include_logon=include_logon)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
        fh.write(xml)
        xml_path = fh.name
    try:
        return runner(["schtasks", "/Create", "/TN", name, "/XML", xml_path, "/F"])
    finally:
        try:
            Path(xml_path).unlink()
        except OSError:
            pass


def install(*, root: Path = ROOT, runner: Runner | None = None,
           python_exe: str | None = None) -> tuple[bool, str]:
    """Registers the task; `(ok, message)`. Never touches a real task in a test: `runner` is
    always a fake there. Resolved at call time (not a default-argument binding), so a test that
    patches the module's own `_default_runner` reaches the CLI path too.

    Tries both triggers first. When Windows refuses exactly because of the `LogonTrigger` (a
    non-administrator, issue #230: `schtasks` answers "Access is denied"), retries with the clock
    trigger alone and still returns `ok=True`, naming in the message what was left out and why. Any
    other refusal still fails `install`."""
    if sys.platform != "win32":
        return False, "keepalive install: Windows only (schtasks)"
    runner = runner or _default_runner
    name = task_name(root)
    exe = f'"{_pythonw(python_exe)}"'
    arguments = "-m tools.agentops dispatch --ensure"
    try:
        result = _create(name, exe, arguments, str(root), runner, include_logon=True)
    except KeepaliveError as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, f"installed {name} ({SCHEDULE_DESCRIPTION})"
    refusal = (result.stderr or result.stdout).strip()
    if ACCESS_DENIED in refusal.lower():
        retry = _create(name, exe, arguments, str(root), runner, include_logon=False)
        if retry.returncode == 0:
            return True, (
                f"installed {name} ({SCHEDULE_DESCRIPTION_CLOCK_ONLY}); left out: start at logon "
                f"(schtasks refused it: {refusal} -- this user is not an administrator)"
            )
        retry_refusal = retry.stderr or retry.stdout
        return False, f"keepalive install: schtasks refused ({retry.returncode}): {retry_refusal}"
    return False, f"keepalive install: schtasks refused ({result.returncode}): {refusal}"


def status(*, root: Path = ROOT, runner: Runner | None = None) -> tuple[int, str]:
    """`(count, message)`: `count` is 1 when the task exists, else 0. Names the clock trigger alone
    when the installed task's own XML holds no `LogonTrigger` (issue #238, #230's fallback): the
    installed task's real XML is read back through `/Query .../XML`, never assumed from
    `SCHEDULE_DESCRIPTION` alone -- a fixed message would still say "and at logon" for a task a
    non-administrator's `install` registered without one."""
    if sys.platform != "win32":
        return 0, "keepalive status: Windows only (schtasks)"
    runner = runner or _default_runner
    name = task_name(root)
    result = runner(["schtasks", "/Query", "/TN", name, "/XML"])
    if result.returncode != 0:
        return 0, "0 tasks"
    schedule = SCHEDULE_DESCRIPTION if "<LogonTrigger>" in result.stdout else SCHEDULE_DESCRIPTION_CLOCK_ONLY
    return 1, f"1 task: {name} ({schedule})"


def remove(*, root: Path = ROOT, runner: Runner | None = None) -> tuple[bool, str]:
    """Removes the task; `(ok, message)`. `ok` is True whether it existed or not -- "removed" is
    the wanted end state either way."""
    if sys.platform != "win32":
        return False, "keepalive remove: Windows only (schtasks)"
    runner = runner or _default_runner
    name = task_name(root)
    result = runner(["schtasks", "/Delete", "/TN", name, "/F"])
    if result.returncode != 0:
        return False, f"keepalive remove: schtasks refused ({result.returncode}): {result.stderr or result.stdout}"
    return True, f"removed {name}"


# -- CLI ------------------------------------------------------------------------------------------

def add_subparser(sub) -> None:
    keepalive = sub.add_parser("keepalive", help="a scheduled task that restarts the dispatcher (issue #201)")
    keepalive_sub = keepalive.add_subparsers(dest="keepalive_command", required=True)
    keepalive_sub.add_parser("install", help="register the scheduled task")
    keepalive_sub.add_parser("remove", help="remove the scheduled task")
    keepalive_sub.add_parser("status", help="show whether the scheduled task is registered")


def run_keepalive_command(args) -> int:
    if args.keepalive_command == "install":
        ok, message = install()
        print(message)
        return 0 if ok else 1
    if args.keepalive_command == "remove":
        ok, message = remove()
        print(message)
        return 0 if ok else 1
    if args.keepalive_command == "status":
        _count, message = status()
        print(message)
        return 0 if sys.platform == "win32" else 1
    return 2


__all__ = [
    "TASK_NAME_PREFIX",
    "SCHEDULE_DESCRIPTION",
    "KeepaliveError",
    "task_name",
    "install",
    "status",
    "remove",
    "add_subparser",
    "run_keepalive_command",
]
