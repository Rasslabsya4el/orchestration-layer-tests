"""#236: `launch_detached_dispatcher` started its child with `CREATE_NEW_CONSOLE |
CREATE_NEW_PROCESS_GROUP` and, later, `CREATE_BREAKAWAY_FROM_JOB`. A process group is not a job
object: on Windows, a child is a member of the same job object as its parent by default unless it
breaks away, and `CREATE_BREAKAWAY_FROM_JOB` itself only works when the enclosing job carries
`JOB_OBJECT_LIMIT_BREAKAWAY_OK`.

#248 measured live that the dispatcher's real starters (a Claude Code session, the Task
Scheduler's `pythonw.exe`) sit in a job that does NOT carry `JOB_OBJECT_LIMIT_BREAKAWAY_OK`: 3 of
3 live starts still showed `IsProcessInJob` true on the dispatcher. The fix drops
`CreateProcess`/`CREATE_BREAKAWAY_FROM_JOB` entirely and starts the dispatcher through WMI
`Win32_Process.Create` instead (`_launch_via_wmi` in `dispatch.py`) -- the same mechanism
`scripts/launch_hidden.ps1` already uses. The new process is a child of `WmiPrvSE.exe` (the WMI
provider host), never of the caller, so it carries none of the caller's job-object membership at
all, breakaway flag or not.

Row 1 (#248): a stand-in process started through `_launch_via_wmi` (never a real dispatcher) --
`IsProcessInJob(handle, NULL)` must read false.

Row 2 (#248): the starter itself sits in a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and NO
`JOB_OBJECT_LIMIT_BREAKAWAY_OK` (built here with ctypes, same job-object calls as before); the
starter calls `_launch_via_wmi` to spawn the stand-in, then exits; this test closes its own handle
to the job (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` fires); the stand-in must still be alive 10 s
later -- because it was never a member of that job to begin with.
"""

from __future__ import annotations

import base64
import ctypes
import json
import re
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects only")

ROOT = Path(__file__).resolve().parents[1]

# #343: the ctypes.WinDLL / WINFUNCTYPE loads below run at import, before any skipif mark, so off Windows
# this module must skip before them, or collection fails on the Linux map-build runner.
if sys.platform != "win32":
    pytest.skip("Windows job objects only", allow_module_level=True)

# -- minimal ctypes bindings for the Job Object calls this test needs --------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
user32.CreateDesktopW.restype = wintypes.HANDLE

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GENERIC_ALL = 0x10000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_SUSPENDED = 0x00000004


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_kill_on_close_job() -> int:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def _assign_process_to_job(job: int, proc: subprocess.Popen) -> None:
    ok = kernel32.AssignProcessToJobObject(job, int(proc._handle))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _resume_process(proc: subprocess.Popen) -> None:
    status = ntdll.NtResumeProcess(int(proc._handle))
    if status != 0:
        raise OSError(f"NtResumeProcess failed: 0x{status:08x}")


def _pid_alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        return False
    exit_code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    STILL_ACTIVE = 259
    return exit_code.value == STILL_ACTIVE


def _is_process_in_job(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    result = wintypes.BOOL()
    ok = kernel32.IsProcessInJob(handle, None, ctypes.byref(result))
    kernel32.CloseHandle(handle)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(result.value)


def _kill_pid(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


_STANDIN_COMMAND = '"{python}" -c "import time; time.sleep(120)  # PKG_248_STANDIN"'


# -- #257 row 4: a desktop object of the trial's own, watched against the owner's real desktop --
# (`ab-mini-c`'s `scripts/probe_job_breakaway.py`, `OwnerDesktopWatch`/`process_table`/
# `descendants`/`_all_top_level_windows`, taken by hand.) Measured live on this branch (before this
# design): a plain `python.exe -c "..."` started straight through `_launch_via_wmi` left 2 hidden
# windows on the OWNER's desktop (`ConsoleWindowClass`, `IME`) even though this test's own caller
# never touched that desktop -- WMI ignores `Win32_ProcessStartup.WinstationDesktop` outright, so
# a WMI-started console-subsystem process lands wherever WMI's own session does, not on any
# desktop this test names. The fix (ab-mini-c's own, matched here): WMI starts a `pythonw.exe`
# launcher instead -- a GUI-subsystem exe, so it opens no console/IME window on ANY desktop no
# matter where WMI puts it -- and that launcher alone (via a plain `CreateProcessW` from a live
# process, which DOES honour `STARTUPINFO.lpDesktop`, unlike WMI) starts the real, console-having
# stand-in explicitly on this test's own isolated desktop.

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _create_isolated_desktop() -> tuple[int, str]:
    """A brand-new desktop, never `WinSta0\\Default` (the owner's). Raises `OSError` and starts
    nothing further on failure -- this test never falls back to the real desktop."""
    name = f"pkg-257-{uuid.uuid4().hex}"
    handle = user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not handle:
        raise OSError(f"CreateDesktopW failed (error {ctypes.get_last_error()})")
    return handle, name


def _close_desktop(handle: int) -> None:
    user32.CloseDesktop(handle)


def _pythonw() -> str:
    """The windowless (`pythonw.exe`) sibling of `sys.executable`. Never falls back to
    `sys.executable` (that would hand `python.exe` -- a console program -- to the WMI start, the
    exact thing #261's refusal exists to catch): a missing `pythonw.exe` skips clearly instead."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if not candidate.exists():
        pytest.skip(f"no pythonw.exe next to {sys.executable}; the windowless WMI launcher needs it")
    return str(candidate)


def _process_table() -> dict[int, tuple[int, str]]:
    """`{pid: (ppid, exe)}` from one Toolhelp snapshot."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    out: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _descendants(roots: set[int], table: dict[int, tuple[int, str]]) -> set[int]:
    found = set(roots)
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _exe) in table.items():
            if ppid in found and pid not in found:
                found.add(pid)
                grew = True
    return found


def _all_top_level_windows() -> dict[int, tuple[str, int, bool]]:
    """`{hwnd: (class, owner pid, visible)}` of every top-level window on this thread's desktop
    (the owner's, for the watch thread below -- it never calls `SetThreadDesktop`)."""
    found: dict[int, tuple[str, int, bool]] = {}

    def _cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found[hwnd] = (buf.value, pid.value, bool(user32.IsWindowVisible(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


@dataclass
class _OwnerDesktopWatch:
    """Samples the *owner's* desktop (this thread's own -- pytest never runs on the isolated one)
    every `interval` s while running, and keeps every top-level window (any class, visible or
    not) it sees there that is new since `start()` and owned by a process descended from a WMI
    provider host (`WmiPrvSE.exe`) born during the trial -- a WMI-started process is a child of
    that host, never of the caller, so this is the only reliable way to find "the trial's own"
    processes without a job or a process-group to ask."""
    interval: float = 0.05
    roots: set[int] = field(default_factory=set)
    trial_pids: set[int] = field(default_factory=set)
    windows: dict[int, tuple[str, int, bool]] = field(default_factory=dict)
    _baseline: set[int] = field(default_factory=set)
    _baseline_pids: set[int] = field(default_factory=set)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._baseline = set(_all_top_level_windows())
        self._baseline_pids = set(_process_table())
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.interval)
        self._tick()

    def _tick(self) -> None:
        table = _process_table()
        wmi_hosts = {pid for pid, (_pp, exe) in table.items() if exe.lower() == "wmiprvse.exe"}
        self.roots |= {pid for pid, (pp, _exe) in table.items()
                       if pp in wmi_hosts and pid not in self._baseline_pids}
        self.trial_pids |= _descendants(set(self.roots), table)
        for hwnd, info in _all_top_level_windows().items():
            if hwnd not in self._baseline and info[1] in self.trial_pids:
                self.windows.setdefault(hwnd, info)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


#: run by WMI (`pythonw`, GUI subsystem: no console, no window, wherever WMI puts it). Starts the
#: real (console-having) stand-in explicitly on the desktop named in argv[1] via `CreateProcessW`
#: -- the line to run is read from the file argv[2] -- and writes what it got to the json file
#: argv[3]. Self-contained (no import of this test module: a subprocess can't import a `test_*`
#: file as a package-less module reliably).
_LAUNCHER_SCRIPT = textwrap.dedent(
    """
    import ctypes, json, sys
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CREATE_NEW_CONSOLE = 0x00000010
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESHOWWINDOW = 0x00000001

    desktop_name, line_file, result_file = sys.argv[1], sys.argv[2], sys.argv[3]
    line = open(line_file, encoding="utf-8").read()
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    startup.lpDesktop = "WinSta0\\\\" + desktop_name
    startup.dwFlags = STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    info = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(None, line, None, None, False,
                                 CREATE_NEW_CONSOLE | CREATE_UNICODE_ENVIRONMENT, None, None,
                                 ctypes.byref(startup), ctypes.byref(info))
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump({"pid": info.dwProcessId if ok else None,
                   "error": 0 if ok else ctypes.get_last_error()}, f)
    """
)


def _windowless_launcher_command(tmp_path: Path, desktop_name: str, line: str) -> tuple[str, Path]:
    """Writes `_LAUNCHER_SCRIPT` and `line` (the command it will run via `CreateProcessW`) under
    `tmp_path`, and returns `(the windowless command line for _launch_via_wmi, the result file the
    launcher writes its {"pid", "error"} to)`. #273: shared by both #248's row-1 test
    (`test_wmi_standin_is_not_in_any_job`) and row-2 test (`_run_row2_trial`) so the launcher is
    never copied -- every program `_launch_via_wmi` sees from this test module is `pythonw`
    (`_pythonw()`), never `python.exe`/`sys.executable`."""
    launcher_path = tmp_path / "launcher.py"
    launcher_path.write_text(_LAUNCHER_SCRIPT, encoding="utf-8")
    line_file = tmp_path / "line.txt"
    line_file.write_text(line, encoding="utf-8")
    result_file = tmp_path / "result.json"
    command = f'"{_pythonw()}" "{launcher_path}" "{desktop_name}" "{line_file}" "{result_file}"'
    return command, result_file


def test_wmi_standin_is_not_in_any_job(tmp_path: Path):
    """#248 row 1, #257 acceptance row 4: `_launch_via_wmi` -- the real function, real 60 s
    timeout, real `powershell -EncodedCommand` call -- starts a process that is `IsProcessInJob`
    false, live, with a sampler watching the OWNER's real desktop the whole time: 0 windows of any
    class (visible or hidden) land there, owned by any process the trial started. The stand-in
    itself runs on a desktop object of this test's own (see the module comment above)."""
    from tools.agentops.dispatch import _launch_via_wmi

    desktop, desktop_name = _create_isolated_desktop()
    watch = _OwnerDesktopWatch()
    watch.start()

    launcher_command, result_file = _windowless_launcher_command(
        tmp_path, desktop_name, _STANDIN_COMMAND.format(python=sys.executable),
    )

    child_pid = None
    try:
        wmi_pid = _launch_via_wmi(launcher_command, tmp_path)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not result_file.exists():
            time.sleep(0.1)
        assert result_file.exists(), "the launcher never wrote its result in time"
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert result["pid"], f"the launcher's own CreateProcessW failed: {result}"
        child_pid = result["pid"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _pid_alive(child_pid):
            time.sleep(0.1)
        assert not _is_process_in_job(child_pid), (
            "the WMI-started stand-in must carry no job membership"
        )
        time.sleep(3.0)  # let the sampler catch a window that only shows up a tick after spawn
    finally:
        watch.stop()
        _close_desktop(desktop)
        if child_pid is not None:
            _kill_pid(child_pid)

    assert watch.windows == {}, f"windows landed on the owner's desktop: {watch.windows}"


#: run inside the job (the "starter"). It calls `_launch_via_wmi` with the windowless
#: `pythonw` launcher command built by `_windowless_launcher_command` (already substituted in by
#: `_run_row2_trial`) -- never a raw `python.exe`/`sys.executable` command line, so #261's refusal
#: (only `pythonw.exe`/`pythonw` may go to the WMI start under pytest) never fires. The launcher
#: itself then writes the REAL stand-in's `{"pid", "error"}` to `result_file`; this script polls
#: for that and relays the pid to `pid_file` for the outer test to check.
PARENT_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time
    sys.path.insert(0, {root!r})
    from tools.agentops.dispatch import _launch_via_wmi

    _launch_via_wmi({launcher_command!r}, {cwd!r})

    result_file = {result_file!r}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not os.path.exists(result_file):
        time.sleep(0.1)
    if os.path.exists(result_file):
        data = json.loads(open(result_file, encoding="utf-8").read())
    else:
        data = {{"pid": None, "error": "the launcher never wrote its result in time"}}

    with open({pid_file!r}, "w", encoding="utf-8") as f:
        json.dump({{"pid": data.get("pid")}}, f)
    """
)


def _run_row2_trial(tmp_path: Path, *, tag: str) -> tuple[int, bool, bool]:
    """Spawns a stand-in "starter" inside a fresh kill-on-close job (no breakaway-ok); the starter
    launches the shared windowless `pythonw` launcher through `_launch_via_wmi`, which places the
    real stand-in on a desktop object of this trial's own (`_windowless_launcher_command`, #273).
    Waits for the stand-in's pid, closes the job, and returns `(child_pid,
    in_any_job_before_close, alive_10s_after_close)`."""
    out_path = tmp_path / "dispatch.out"
    pid_file = tmp_path / "child_pid.json"
    desktop, desktop_name = _create_isolated_desktop()
    launcher_command, result_file = _windowless_launcher_command(
        tmp_path, desktop_name, _STANDIN_COMMAND.format(python=sys.executable),
    )
    parent_script = PARENT_SCRIPT.format(
        root=str(ROOT), cwd=str(tmp_path), pid_file=str(pid_file),
        launcher_command=launcher_command, result_file=str(result_file),
    )
    script_path = tmp_path / "parent.py"
    script_path.write_text(parent_script, encoding="utf-8")

    job = _create_kill_on_close_job()
    parent_proc = None
    child_pid = None
    try:
        parent_proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(ROOT),
            creationflags=CREATE_SUSPENDED,
        )
        _assign_process_to_job(job, parent_proc)
        _resume_process(parent_proc)

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.1)
        assert pid_file.exists(), f"[{tag}] the stand-in starter never wrote the child pid in time"

        child_pid = json.loads(pid_file.read_text(encoding="utf-8"))["pid"]
        assert child_pid, f"[{tag}] the windowless launcher's own CreateProcessW failed"
        parent_proc.wait(timeout=10)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _pid_alive(child_pid):
            time.sleep(0.1)
        assert _pid_alive(child_pid), f"[{tag}] the stand-in child exited before the job closed"
        in_any_job = _is_process_in_job(child_pid)

        # Close this test's only handle to the job: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE fires.
        kernel32.CloseHandle(job)
        job = None

        time.sleep(10)
        return child_pid, in_any_job, _pid_alive(child_pid)
    finally:
        if child_pid is not None and _pid_alive(child_pid):
            _kill_pid(child_pid)
        if parent_proc is not None and parent_proc.poll() is None:
            parent_proc.kill()
            parent_proc.wait(timeout=10)
        if job is not None:
            kernel32.CloseHandle(job)
        _close_desktop(desktop)


@pytest.mark.slow
def test_wmi_standin_survives_starters_job_closing_3_of_3(tmp_path_factory):
    """#248 row 2, #257 acceptance row 5: 3 of 3 trials, a starter in a
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job with NO `JOB_OBJECT_LIMIT_BREAKAWAY_OK` -- the exact
    case where the old `CREATE_BREAKAWAY_FROM_JOB` fix still failed live (#248's own measurement).
    `IsProcessInJob` false 3 of 3, alive 10 s after the starter's job closed 3 of 3.
    ~35 s (3 trials x ~10-12 s each)."""
    for i in range(3):
        trial_dir = tmp_path_factory.mktemp(f"row2_{i}")
        child_pid, in_any_job, alive = _run_row2_trial(trial_dir, tag=f"row2-{i}")
        assert not in_any_job, f"row 2 trial {i}: the stand-in was in a job"
        assert alive, f"row 2 trial {i}: child died 10s after the starter's job closed"


# -- #257 acceptance rows 1-3: the `powershell` call itself (`_launch_via_wmi`), no real process --

def _decoded_script(command: list) -> str:
    encoded = command[command.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_a_timed_out_wmi_call_raises_oserror_with_the_real_60s_timeout(tmp_path: Path):
    """Row 1: an injected `run` raising `subprocess.TimeoutExpired` turns into `OSError`; the real
    call (no `timeout=` override) carries `timeout=60`."""
    from tools.agentops.dispatch import _launch_via_wmi

    seen_kwargs = {}

    def fake_run(command, **kwargs):
        seen_kwargs.update(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    with pytest.raises(OSError):
        _launch_via_wmi("x", tmp_path, run=fake_run, env={})

    assert seen_kwargs.get("timeout") == 60


def test_ensure_exits_non_zero_when_the_wmi_start_fails(tmp_path: Path, capsys):
    """Row 1: `dispatch --ensure` ends with a non-zero exit code when the launch raises `OSError`,
    with one line naming it on stderr."""
    from tools.agentops.dispatch import run_ensure

    def failing_launch(*, out_path):
        raise OSError("_launch_via_wmi: WMI Win32_Process.Create timed out after 60s")

    rc = run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "dispatch.out",
                    launch=failing_launch, root=tmp_path)

    assert rc != 0
    err = capsys.readouterr().err
    assert "timed out after 60s" in err


def test_launch_via_wmi_command_has_no_file_and_carries_noninteractive_encodedcommand(tmp_path: Path):
    """Row 2: the command list holds `-NonInteractive` and `-EncodedCommand`, and no `-File`."""
    from tools.agentops.dispatch import _launch_via_wmi

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="rc=0 pid=1", stderr="")

    _launch_via_wmi("x", tmp_path, run=fake_run, env={})

    command = calls[0]
    assert command[0] == "powershell"
    assert "-NonInteractive" in command
    assert "-EncodedCommand" in command
    assert "-File" not in command


_PS_LITERAL_RE = re.compile(r"CurrentDirectory = '((?:[^']|'')*)'")


@pytest.mark.parametrize("root", [
    r"D:\Work\3x\3x - Other dashboard",
    r"D:\Work\3x\it's a 'root'",
])
def test_launch_via_wmi_carries_the_whole_root_through_the_encoded_command(root: str):
    """Row 3: the built line for the Other root, and for a root holding a `'`, decoded back from
    `-EncodedCommand`: both paths whole, the exact string asserted (never truncated at the
    quote)."""
    from tools.agentops.dispatch import _launch_via_wmi

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="rc=0 pid=1", stderr="")

    command_line = f'cmd /c cd /d "{root}" && dir'
    _launch_via_wmi(command_line, Path(root), run=fake_run, env={})

    script = _decoded_script(calls[0])
    match = _PS_LITERAL_RE.search(script)
    assert match is not None, f"no CurrentDirectory literal found in {script!r}"
    decoded_root = match.group(1).replace("''", "'")
    assert decoded_root == root, f"the root came through as {decoded_root!r}, not whole"
