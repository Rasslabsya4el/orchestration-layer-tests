"""A memory cap for a process that is already running (#276): the process and every descendant of it go into a
Windows Job Object with ``JOB_OBJECT_LIMIT_PROCESS_MEMORY``. A process over the limit gets a failed allocation (a Python
``MemoryError``), so a runaway collector ends instead of taking the PC's memory. Standard library plus ctypes only.

Two public functions, Windows only (anywhere else they say so and change nothing):

* ``cap_process_memory(pid, limit_bytes) -> str | None``: None on success, one message on any failure; never raises.
* ``process_memory_limit(pid) -> int | None``: the limit a capped process runs under, for a live check.

The rules the code relies on:

* A process already in a job takes a second one (jobs nest; the tighter limit wins). No breakaway right is set, so a
  child cannot leave the job and every child inherits it.
* The limit does not need the job handle, but the job's name does: the name is gone when the last handle closes. The
  capped root gets a duplicate of the job handle (``_share_job``), so the name lives as long as that process does, not
  as long as the calling process. The module also keeps one handle per capped root in ``_HELD`` for the life of the
  calling process (the only one left when a duplicate is refused) and closes the handles of roots that have exited at
  the next cap.
* Windows gives no job from a process handle, so the job is named after the capped root's pid and
  ``process_memory_limit`` finds it by that name for the pid and each ancestor, confirmed by ``IsProcessInJob``. Only
  the calling process's own job is read without the name.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

#: a child started right after the cap is applied is looked for once more after this long
LATE_CHILD_WAIT_S = 0.3

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED = 0x1000
_PROCESS_DUP_HANDLE = 0x0040
_DUPLICATE_SAME_ACCESS = 0x0002
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPPROCESS = 0x2
_STILL_ACTIVE = 259
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_MAX_DEPTH = 32
_ERROR_ALREADY_EXISTS = 183

_api = None
#: root pid -> job handle, kept open so the job's name stays findable (see the module doc)
_HELD: dict[int, int] = {}


def _is_windows() -> bool:
    return sys.platform == "win32"


def _kernel32():
    """kernel32 with the argument types of the calls used here (a 64-bit handle must not be cut to an int)."""
    global _api
    if _api is not None:
        return _api
    from ctypes import wintypes as w

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, B, D, P = w.HANDLE, w.BOOL, w.DWORD, ctypes.c_void_p
    sigs = {
        "OpenProcess": (H, [D, B, D]), "CloseHandle": (B, [H]),
        "CreateJobObjectW": (H, [P, w.LPCWSTR]), "OpenJobObjectW": (H, [D, B, w.LPCWSTR]),
        "SetInformationJobObject": (B, [H, ctypes.c_int, P, D]),
        "QueryInformationJobObject": (B, [H, ctypes.c_int, P, D, P]),
        "AssignProcessToJobObject": (B, [H, H]), "IsProcessInJob": (B, [H, H, ctypes.POINTER(B)]),
        "CreateToolhelp32Snapshot": (H, [D, D]),
        "Process32FirstW": (B, [H, ctypes.POINTER(ProcessEntry)]),
        "Process32NextW": (B, [H, ctypes.POINTER(ProcessEntry)]),
        "GetProcessTimes": (B, [H, P, P, P, P]), "GetExitCodeProcess": (B, [H, ctypes.POINTER(D)]),
        "GetCurrentProcess": (H, []),
        "DuplicateHandle": (B, [H, H, H, ctypes.POINTER(H), D, B, D]),
    }
    for name, (res, args) in sigs.items():
        fn = getattr(k, name)
        fn.restype, fn.argtypes = res, args
    k.ExtendedLimit, k.ProcessEntry = ExtendedLimit, ProcessEntry
    _api = k
    return k


def _fail(step: str) -> OSError:
    return OSError(f"{step} failed: {ctypes.WinError(ctypes.get_last_error())}")


def _open_process(pid: int, access: int):
    handle = _kernel32().OpenProcess(access, False, pid)
    if not handle:
        raise _fail(f"OpenProcess({pid})")
    return handle


def _job_name(root_pid: int) -> str:
    return f"Global\\orchestration-layer-memcap-{root_pid}"


def _parents() -> dict[int, int]:
    """pid -> parent pid of every process, from one ToolHelp32 snapshot."""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        raise _fail("CreateToolhelp32Snapshot")
    out: dict[int, int] = {}
    try:
        entry = k.ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return out


def _created(pid: int) -> int | None:
    """The process's creation time (FILETIME ticks); None when it cannot be read."""
    k = _kernel32()
    try:
        handle = _open_process(pid, _PROCESS_QUERY_LIMITED)
    except OSError:
        return None
    try:
        times = (ctypes.c_uint64 * 4)()
        if not k.GetProcessTimes(handle, *(ctypes.byref(times, 8 * i) for i in range(4))):
            return None
        return times[0]
    finally:
        k.CloseHandle(handle)


def _descendants(root: int) -> list[int]:
    """Every process below ``root`` now. A child counts only when it was created after its parent, so a parent pid
    that Windows reused for another process does not pull a stranger in."""
    parents = _parents()
    found: list[int] = []
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        parent_time = _created(parent)
        for pid, ppid in parents.items():
            if ppid != parent or pid in found or pid == root:
                continue
            child_time = _created(pid)
            if parent_time is not None and child_time is not None and child_time < parent_time:
                continue
            found.append(pid)
            frontier.append(pid)
    return found


def _alive(handle) -> bool:
    from ctypes import wintypes as w
    code = w.DWORD()
    return bool(_kernel32().GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE


def _join(job, pid: int, errors: list[str], *, must: bool) -> None:
    """Put ``pid`` into ``job`` unless it is in already. A descendant that ended meanwhile is no failure."""
    from ctypes import wintypes as w
    k = _kernel32()
    try:
        handle = _open_process(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED)
    except OSError as exc:
        if must:
            errors.append(str(exc))
        return
    try:
        inside = w.BOOL()
        if k.IsProcessInJob(handle, job, ctypes.byref(inside)) and inside.value:
            return
        if not k.AssignProcessToJobObject(job, handle) and (must or _alive(handle)):
            errors.append(str(_fail(f"AssignProcessToJobObject({pid})")))
    finally:
        k.CloseHandle(handle)


def _release_ended_roots() -> None:
    """Close the held job handle of every capped root that has exited (its processes keep the job alive themselves)."""
    k = _kernel32()
    for root in list(_HELD):
        try:
            handle = _open_process(root, _PROCESS_QUERY_LIMITED)
        except OSError:
            handle = None
        try:
            if handle is not None and _alive(handle):
                continue
        finally:
            if handle is not None:
                k.CloseHandle(handle)
        k.CloseHandle(_HELD.pop(root))


def _share_job(job, pid: int) -> None:
    """Duplicate ``job`` into the process ``pid``, so the job's name lives as long as that process (#279). The copy is
    left open in ``pid`` and never closed here. A refusal (no PROCESS_DUP_HANDLE access, a failed duplicate) is
    swallowed: the cap holds without it, and the name then lives as long as the calling process's handle."""
    from ctypes import wintypes as w
    k = _kernel32()
    try:
        target = _open_process(pid, _PROCESS_DUP_HANDLE)
    except OSError:
        return
    try:
        copy = w.HANDLE()
        k.DuplicateHandle(k.GetCurrentProcess(), job, target, ctypes.byref(copy), 0, False, _DUPLICATE_SAME_ACCESS)
    except Exception:  # the cap is already in place: a copy that cannot be made is no failure of it
        pass
    finally:
        k.CloseHandle(target)


def cap_process_memory(pid: int, limit_bytes: int) -> str | None:
    """Cap the memory of the running process ``pid`` and of all its descendants at ``limit_bytes`` per process.

    Creates a Job Object (named after ``pid``, see the module doc) with ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` and no
    breakaway right, assigns ``pid``, then every descendant that exists, and once more after ``LATE_CHILD_WAIT_S`` for
    one that started late; a child started later still inherits the job. Returns None on success, otherwise one message
    (a descendant that could not join is named and the rest are capped anyway). A job whose name is taken or cannot be
    made is replaced by an unnamed one: the cap holds and the message starts ``capped, limit not readable:``, because
    ``process_memory_limit`` cannot find it. A named job also gets a copy of its handle inside ``pid`` (``_share_job``),
    so its name outlives the calling process; a copy that cannot be made changes nothing here. Never raises. Off
    Windows: a message."""
    if not _is_windows():
        return "jobcap: not windows, no memory cap applied"
    try:
        if limit_bytes <= 0:
            return f"jobcap: limit {limit_bytes} is not a positive byte count"
        k = _kernel32()
        _release_ended_roots()  # first: a held name of an exited root (and its reused pid) must be free
        note = ""
        ctypes.set_last_error(0)
        job = k.CreateJobObjectW(None, _job_name(pid))
        if job and ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            k.CloseHandle(job)  # a job of this name already exists: not ours (a reused pid), never assign into it
            job, note = None, "the job name is taken"
        elif not job:
            note = "the job name could not be created"
        if not job:
            job = k.CreateJobObjectW(None, None)  # still cap, with a job nothing can find by name
        if not job:
            raise _fail("CreateJobObjectW")
        try:
            info = k.ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = limit_bytes
            if not k.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                             ctypes.sizeof(info)):
                raise _fail("SetInformationJobObject")
            errors: list[str] = []
            _join(job, pid, errors, must=True)
        except BaseException:
            k.CloseHandle(job)
            raise
        if errors:
            k.CloseHandle(job)
            return "jobcap: " + errors[0]
        if not note:
            _HELD[pid] = job
            _share_job(job, pid)
        try:
            for walk in range(2):
                if walk:
                    time.sleep(LATE_CHILD_WAIT_S)
                for child in _descendants(pid):
                    _join(job, child, errors, must=False)
        finally:
            if note:
                k.CloseHandle(job)  # nothing finds it by name, the cap holds without the handle
        if errors:
            return "jobcap: " + "; ".join(errors)
        return f"capped, limit not readable: {note}" if note else None
    except Exception as exc:  # the caller starts a collector next: a cap that fails is a log line, never a crash
        return f"jobcap: {type(exc).__name__}: {exc}"


def _limit_of(job) -> int | None:
    k = _kernel32()
    info = k.ExtendedLimit()
    if not k.QueryInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                       ctypes.sizeof(info), None):
        return None
    if info.BasicLimitInformation.LimitFlags & _JOB_OBJECT_LIMIT_PROCESS_MEMORY:
        return int(info.ProcessMemoryLimit)
    return None


def process_memory_limit(pid: int) -> int | None:
    """The per-process memory limit, in bytes, of the job ``pid`` runs in. None means "no limit found or unknown": a
    process never capped here, one capped by an unnamed job, or one that cannot be opened all answer None alike.

    For the calling process this reads its own job (``QueryInformationJobObject`` with a NULL handle). For any other
    process Windows gives no way from a process to its job, so only a job made by ``cap_process_memory`` is found:
    through its name, for ``pid`` and each ancestor of it, confirmed by ``IsProcessInJob``. A process under another
    job's limit answers None (the true answer is "unknown"). Never raises."""
    if not _is_windows():
        return None
    try:
        from ctypes import wintypes as w
        k = _kernel32()
        if pid == os.getpid():
            own = _limit_of(None)
            if own is not None:
                return own
        parents = _parents()
        chain: list[int] = []
        current = pid
        while current and current not in chain and len(chain) < _MAX_DEPTH:
            chain.append(current)
            current = parents.get(current, 0)
        handle = _open_process(pid, _PROCESS_QUERY_LIMITED)
        try:
            for root in chain:
                job = k.OpenJobObjectW(_JOB_OBJECT_QUERY, False, _job_name(root))
                if not job:
                    continue
                try:
                    inside = w.BOOL()
                    if k.IsProcessInJob(handle, job, ctypes.byref(inside)) and inside.value:
                        found = _limit_of(job)
                        if found is not None:
                            return found
                finally:
                    k.CloseHandle(job)
        finally:
            k.CloseHandle(handle)
    except Exception:
        return None
    return None
