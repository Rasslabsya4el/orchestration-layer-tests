"""Small, dependency-free helpers shared by the agentops bridge. Standard library only.

Ported from `orchlib/util.py` (the owner's separate plugin repo, its own location one of
the paths in `config/orchestration.json`), trimmed to what the bridge needs: JSON
read/write with the Windows retry-on-PermissionError behaviour, a cross-process file
lock, and a human duration formatter (durations written for a human, never a bare count
of seconds) that does not depend on the product's own report-formatting module, so this
package stays copyable into a repo that has no such module.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .core_format import human_duration  # noqa: F401  (re-export: one duration helper, #483)


class AgentOpsError(RuntimeError):
    """A bridge-level failure (opencode not found, server not running, a bad reply)."""


#: D110: checked in this order for a session's own id when the caller does not name one
#: (`dispatch.find_own_session_id`, `core_issue.run_issue_command`); Claude Code does not
#: document one today (checked #191), but a future one wins at once. Shared here (not in
#: `dispatch.py`) so `core_issue.py` can import it without a circular import (#286).
SESSION_ID_ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

#: #195: the three variables a map build sets for its own suite run (`test_select.build_map`). A suite a run
#: starts inside a test or inside another build never inherits them: the nested temp repo cannot import the plugin.
BUILD_ENV_VARS = ("PYTEST_ADDOPTS", "SUITE_READS_DIR", "TESTMON_DATAFILE")


def nested_env(extra: dict | None = None) -> dict[str, str]:
    """A copy of this process' environment without ``BUILD_ENV_VARS``, plus ``extra`` (#195)."""
    return {**{k: v for k, v in os.environ.items() if k not in BUILD_ENV_VARS}, **(extra or {})}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def no_window_flags() -> int:
    """`creationflags` for every `subprocess` call that starts a console program on Windows
    (#216): a caller with no console of its own (a `pythonw.exe`-launched dispatcher, a
    `--bg` Claude Code session) never flashes a console window up on the owner's desktop.
    `0` (a no-op) on every other platform, matching `cloud_lane.py`'s pre-#216 pattern."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def walk_paths(root: Path) -> Iterator[Path]:
    """Every entry under `root`, files and directories, sorted per directory. A junction or a symlink is
    yielded once and never descended (#360: a link under its own target loops every recursive walk)."""
    stack = [Path(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False) and not os.path.isjunction(entry.path):
                subdirs.append(path)
        stack.extend(reversed(subdirs))


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87

    def _open_process(pid: int) -> tuple[int, int]:
        """``(handle, error)``: ``error`` is ``GetLastError()`` when ``OpenProcess`` failed
        (``handle`` falsy), else 0. A thin wrapper so a test can stub ``OpenProcess`` itself."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return (handle, 0 if handle else ctypes.get_last_error())

    def _exit_code(handle: int) -> int:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return _STILL_ACTIVE  # could not read it: the safe side
        return code.value

    def _filetime_to_epoch(filetime: "wintypes.FILETIME") -> float:
        """A Windows ``FILETIME`` (100 ns ticks since 1601-01-01) as a Unix epoch in seconds."""
        ticks = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
        return (ticks - 116444736000000000) / 10_000_000

    def _process_creation_time(pid: int) -> float | None:
        """The process' own creation time (Unix epoch, seconds), or ``None`` when it cannot be
        read (no such process, access denied, or ``GetProcessTimes`` itself failing) -- the
        caller treats ``None`` as "assume live", the same safe side ``pid_alive`` takes."""
        handle, error = _open_process(pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = _kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel_time), ctypes.byref(user_time),
            )
            if not ok:
                return None
            return _filetime_to_epoch(creation)
        finally:
            _kernel32.CloseHandle(handle)


def pid_alive(pid: int | None) -> bool:
    """True while ``pid`` is a live process.

    On Windows this reads the process' own exit state, not just whether a handle could be
    opened: a handle can outlive the process it names while this or another process still
    holds it (``OpenProcess`` succeeding says nothing about whether the process has since
    exited). A failed ``OpenProcess`` with error 5 (access denied) means the process exists
    but could not be opened: alive. Error 87 (invalid parameter) means no such process: dead.
    Any other error is read as alive -- the safe side, since a lock is never deleted beside a
    process that might still be live.
    """
    if not pid:
        return False
    if os.name == "nt":
        handle, error = _open_process(int(pid))
        if not handle:
            if error == _ERROR_INVALID_PARAMETER:
                return False
            return True  # access denied (still exists), or any other error: the safe side
        try:
            return _exit_code(handle) == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def parse_started(started: str | None) -> float | None:
    """``started``'s own format (``now()``, local naive, ``%Y-%m-%d %H:%M:%S``) as a Unix
    epoch in seconds; ``None`` when missing or unparsable. Shared by every lock that writes
    ``started`` this way (`merge.py`'s age-ceiling message, `holder_is_live` below)."""
    if not started:
        return None
    try:
        return time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def holder_is_live(pid: int | None, started: str | None, *, slack: float = 2.0) -> bool:
    """True when a lock's holder is genuinely alive, not a reused pid.

    A holder counts as live only if ``pid_alive(pid)`` is true **and** the process of that
    pid was created no later than ``started`` plus ``slack`` seconds (``started``'s own clock
    has 1 s steps, and it is written just after the process began, so 2 s of slack is the
    default). A process created later than that holds someone else's pid: the lock is stale
    even though the pid itself is alive. Falls back to ``pid_alive`` alone -- the safe side,
    same reading as before this existed -- when: `started` is missing or does not parse
    (rows unrelated to this ticket keep writing it, but a lock is never invalidated for
    lacking it); the creation time cannot be read (access denied, gone by the time it is
    queried); or on a non-Windows platform, where nothing here can read a creation time.
    """
    if not pid_alive(pid):
        return False
    if os.name != "nt":
        return True
    started_epoch = parse_started(started)
    if started_epoch is None:
        return True
    created = _process_creation_time(int(pid))
    if created is None:
        return True
    return created <= started_epoch + slack


def read_pid_lock(path: Path) -> dict | None:
    """Read a lock file written as JSON ``{"pid": ..., "started": ...}``; ``None`` when missing,
    unreadable or not valid JSON. Shared by every lock that stores a holder this way (the merge
    lock in `merge.py`, the dispatcher lock in `dispatch.py`)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def read_json(path: Path, default=None, retry_seconds: float = 3.0):
    """Read a JSON file; retried for a few seconds against Windows' transient PermissionError
    while another process is mid-replace (ported from `orchlib/util.py`, seen live there)."""
    deadline = time.time() + retry_seconds
    while True:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)
        except ValueError as exc:
            raise AgentOpsError(f"broken JSON in {path}: {exc}") from exc


def write_json(path: Path, data, retry_seconds: float = 5.0) -> None:
    """Write through a temporary file and `os.replace`, retried the same way as `read_json`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # a name of its own per call: two writers sharing one `.tmp` truncate each other's file and one
    # `os.replace` then installs an empty target (#565)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    deadline = time.time() + retry_seconds
    pause = 0.02
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.time() >= deadline:
                tmp.unlink(missing_ok=True)  # a unique name is never reused, so never left behind
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.5)


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line, creating the parent directory. Used for `agentops.jsonl`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


@contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Cross-process lock: an exclusive lock file next to the data file."""
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:  # left by a crashed process
                    lock.unlink()
            except OSError:
                pass
            if time.time() > deadline:
                raise AgentOpsError(f"could not acquire lock {lock} within {timeout}s")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
