"""#276 unit 1: ``tools.agentops.jobcap`` puts a running process, and every descendant of it, under a Windows Job
Object with a process-memory limit. The stand-in child allocates at most 1 GiB, in 64 MiB steps, and the cap is
256 MiB, so it must end with a MemoryError after ~3 steps. Nothing here allocates more than 1 GiB.
Windows only (ctypes against kernel32); listed in tests/pc_only.txt."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tools.agentops import jobcap

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

MIB = 1 << 20
CAP = 256 * MIB
SLACK = 64 * MIB

# The stand-in: mode "alloc" waits for a "go" line, then allocates 16 x 64 MiB (1 GiB at most), touching every page,
# printing "step <i> <peak commit bytes>" after each; a MemoryError prints "memoryerror <i>" and exits 3.
# Mode "spawn" waits for "go", then runs mode "now" (allocates at once) as its own child.
# Mode "pre" starts a mode "alloc" grandchild first (prints its pid and "ready"), then waits for "go" and passes it on.
STAND_IN = r'''
import ctypes, ctypes.wintypes as w, subprocess, sys

class PMC(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
            "PeakPagefileUsage", "PrivateUsage")]

def peak_commit():
    k = ctypes.WinDLL("kernel32"); p = ctypes.WinDLL("psapi")
    k.GetCurrentProcess.restype = w.HANDLE
    p.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(PMC), w.DWORD]
    c = PMC(); c.cb = ctypes.sizeof(c)
    p.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakPagefileUsage

def alloc():
    keep = []
    for i in range(1, 17):
        try:
            b = bytearray(64 << 20)
            for off in range(0, len(b), 4096):
                b[off] = 1
            keep.append(b)
        except MemoryError:
            print("memoryerror", i, flush=True)
            sys.exit(3)
        print("step", i, peak_commit(), flush=True)
    print("done", flush=True)

NOWIN = 0x08000000

def wait_go():
    # anything but "go" (EOF when the test ended or died) ends the stand-in before it allocates
    if sys.stdin.readline() != "go\n":
        sys.exit(4)

mode = sys.argv[1]
if mode == "now":
    alloc()
elif mode == "alloc":
    wait_go()
    alloc()
elif mode == "limit":
    wait_go()
    sys.path.insert(0, sys.argv[2])
    from tools.agentops import jobcap
    import os
    print(jobcap.process_memory_limit(os.getpid()), flush=True)
elif mode == "spawn":
    wait_go()
    sys.exit(subprocess.call([sys.executable, __file__, "now"], stdout=sys.stdout, creationflags=NOWIN))
elif mode == "pre":
    g = subprocess.Popen([sys.executable, __file__, "alloc"], stdin=subprocess.PIPE, text=True, creationflags=NOWIN)
    print("grandchild", g.pid, flush=True)
    print("ready", flush=True)
    if sys.stdin.readline() == "go\n":
        g.stdin.write("go\n"); g.stdin.flush()
    g.stdin.close()  # without "go" the grandchild reads EOF and ends before it allocates
    sys.exit(g.wait())
'''


@pytest.fixture
def stand_in(tmp_path: Path):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    procs: list[subprocess.Popen] = []

    def start(mode: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, str(script), mode, str(Path(__file__).resolve().parents[1])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def _read_line(p: subprocess.Popen, timeout: float = 10) -> str:
    """One line of the stand-in's output, or a failure after ``timeout`` s (a hung stand-in must not hang the suite)."""
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(p.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not box:
        p.kill()
        pytest.fail(f"no line from the stand-in within {timeout} s")
    return box[0]


def _go_and_collect(p: subprocess.Popen) -> tuple[list[str], int]:
    """Send "go" and read the rest of the output; the stand-in must have ended within 10 s."""
    try:
        out, _ = p.communicate("go\n", timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        pytest.fail("the stand-in did not end within 10 s")
    return out.splitlines(), p.returncode


def _peak_commit(lines: list[str]) -> int:
    return max((int(ln.split()[2]) for ln in lines if ln.startswith("step ")), default=0)


def _assert_capped(lines: list[str], rc: int) -> None:
    assert "done" not in lines, lines
    assert any(ln.startswith("memoryerror") for ln in lines), lines
    assert rc == 3
    assert 0 < _peak_commit(lines) <= CAP + SLACK, lines


def test_child_is_stopped_at_the_cap(stand_in):
    p = stand_in("alloc")
    assert jobcap.cap_process_memory(p.pid, CAP) is None
    assert jobcap.process_memory_limit(p.pid) == CAP
    lines, rc = _go_and_collect(p)
    _assert_capped(lines, rc)


def test_grandchild_started_after_the_cap_is_capped(stand_in):
    p = stand_in("spawn")
    assert jobcap.cap_process_memory(p.pid, CAP) is None
    lines, rc = _go_and_collect(p)
    _assert_capped(lines, rc)


def test_grandchild_already_running_at_the_cap_is_capped(stand_in):
    p = stand_in("pre")
    grandchild = int(_read_line(p).split()[1])
    assert _read_line(p).strip() == "ready"
    assert jobcap.cap_process_memory(p.pid, CAP) is None
    assert jobcap.process_memory_limit(grandchild) == CAP
    lines, rc = _go_and_collect(p)
    _assert_capped(lines, rc)


def test_limit_holds_after_the_job_handle_closes(stand_in):
    """The cap needs no handle (the module keeps one only so the job's name stays findable)."""
    p = stand_in("alloc")
    assert jobcap.cap_process_memory(p.pid, CAP) is None
    jobcap._kernel32().CloseHandle(jobcap._HELD.pop(p.pid))
    lines, rc = _go_and_collect(p)
    _assert_capped(lines, rc)


def test_a_taken_job_name_still_caps_and_says_it_is_not_readable(stand_in):
    p = stand_in("alloc")
    k = jobcap._kernel32()
    stale = k.CreateJobObjectW(None, jobcap._job_name(p.pid))  # a stale job of a reused pid
    assert stale
    try:
        msg = jobcap.cap_process_memory(p.pid, CAP)
        assert isinstance(msg, str) and msg.startswith("capped, limit not readable:")
        assert jobcap.process_memory_limit(p.pid) is None
        lines, rc = _go_and_collect(p)
        _assert_capped(lines, rc)
    finally:
        k.CloseHandle(stale)


def test_uncapped_process_has_no_limit(stand_in):
    p = stand_in("alloc")
    assert jobcap.process_memory_limit(p.pid) is None


def test_the_capped_process_reads_its_own_limit(stand_in):
    p = stand_in("limit")
    assert jobcap.cap_process_memory(p.pid, CAP) is None
    lines, rc = _go_and_collect(p)
    assert lines == [str(CAP)] and rc == 0


@pytest.mark.parametrize("which, limit", [("missing", CAP), ("own", 0)])  # a pid in the id would differ per xdist worker
def test_failure_is_a_message_never_a_raise(which, limit):
    pid = 0x7FFFFFF0 if which == "missing" else os.getpid()
    msg = jobcap.cap_process_memory(pid, limit)
    assert isinstance(msg, str) and msg


def test_a_ctypes_error_is_a_message(monkeypatch, stand_in):
    p = stand_in("alloc")

    def boom(*a, **k):
        raise OSError("stubbed")
    monkeypatch.setattr(jobcap, "_open_process", boom)
    msg = jobcap.cap_process_memory(p.pid, CAP)
    assert isinstance(msg, str) and "stubbed" in msg


def test_off_windows_is_a_message_and_nothing_else(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    msg = jobcap.cap_process_memory(os.getpid(), CAP)
    assert isinstance(msg, str) and "not windows" in msg
    assert jobcap.process_memory_limit(os.getpid()) is None
