"""The hold engine suspend what this installation's pipeline runs while the machine mode is
`play`, resume it on `normal`. Nothing is killed; nothing here names a repo -- a repo is told apart by its root.

`keeper_pass(mode, root)` is idempotent: in `play` it suspends every target not yet suspended (a child that appeared
in a frozen tree included); in `normal` it resumes everything the record holds. The record
(`<root>/var/agentops/hold_record.json`) lists what THIS installation suspended as pid + create_time, so a reused
pid is never resumed (or suspended again) wrongly.

Targets = the whole tree of every `background` session whose cwd is under `root`, plus the detached core launches
found by their record files: the merge lock holder, the line checks, the map run's lock (full_run.lock), the opencode server.
Never: the caller and its ancestors, the dispatcher loop, the runs daemon loop, any session that is not
`background` (an interactive one), and their subtrees.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
RECORD_REL = Path("var") / "agentops" / "hold_record.json"
# a process whose command line holds all words of one entry is a loop that must keep running; a repo adds its own
# loops as a list of word lists in `<root>/config/hold_protected.json`
PROTECTED_CMDLINES = (("agentops", "dispatch"),)
PROTECTED_REL = Path("config") / "hold_protected.json"
# the keeper thread of a dispatcher: one pass every KEEPER_SECONDS (a mode read + a process-table scan, quick);
# the `claude agents --json --all` listing, which may take up to a minute, is re-read on its own slower cadence
KEEPER_SECONDS = 3.0
SESSIONS_REFRESH_SECONDS = 10.0
# a repo's own job runners name the pids of what they run in `<root>/var/agentops/hold_roots/<name>.json` (a list)
ROOTS_REL = Path("var") / "agentops" / "hold_roots"


class PsutilTable:
    """The real process table (psutil; `suspend()` is NtSuspendProcess on Windows)."""

    def __init__(self) -> None:
        import psutil
        self._ps = psutil

    def create_time(self, pid: int) -> float | None:
        try:
            return self._ps.Process(pid).create_time()
        except self._ps.Error:
            return None

    def children(self, pid: int) -> list[int]:
        try:
            return [c.pid for c in self._ps.Process(pid).children()]
        except self._ps.Error:
            return []

    def parent(self, pid: int) -> int | None:
        try:
            return self._ps.Process(pid).ppid() or None
        except self._ps.Error:
            return None

    def cmdline(self, pid: int) -> str:
        try:
            return " ".join(self._ps.Process(pid).cmdline())
        except self._ps.Error:
            return ""

    def all_pids(self) -> list[int]:
        return self._ps.pids()

    def suspend(self, pid: int) -> None:
        try:
            self._ps.Process(pid).suspend()
        except self._ps.Error:
            pass

    def resume(self, pid: int) -> None:
        try:
            self._ps.Process(pid).resume()
        except self._ps.Error:
            pass


def _under(cwd, root: Path) -> bool:
    if not cwd:
        return False
    c = str(Path(cwd).resolve()).casefold().replace("\\", "/").rstrip("/")
    r = str(Path(root).resolve()).casefold().replace("\\", "/").rstrip("/")
    return c == r or c.startswith(r + "/")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_root_pids(root: Path, name: str, pids: list[int]) -> None:
    """A job runner names the pids of its running jobs under `name`; `record_roots` holds them."""
    path = root / ROOTS_REL / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sorted({int(p) for p in pids})), encoding="utf-8")
    os.replace(tmp, path)


def extra_roots(root: Path) -> list[int]:
    out: list[int] = []
    for f in sorted((root / ROOTS_REL).glob("*.json")):
        data = _read_json(f)
        if isinstance(data, list):
            out += [p for p in data if isinstance(p, int) and p > 0]
    return out


def protected_cmdlines(root: Path) -> tuple:
    data = _read_json(root / PROTECTED_REL)
    extra = tuple(tuple(str(w).casefold() for w in e) for e in data if isinstance(e, list) and e)         if isinstance(data, list) else ()
    return PROTECTED_CMDLINES + extra


def record_roots(root: Path) -> list[int]:
    """Pids named by this repo's own record / lock files of detached core launches."""
    pids: list[int] = extra_roots(root)
    locks = root / "var" / "locks"
    checks = locks / "merge.lock.checks"
    files = [locks / "merge.lock", root / "var" / "agentops" / "full_run.lock",
             root / "var" / "agentops" / "server.json"]
    if checks.is_dir():
        files += sorted(checks.glob("*.json"))
    for f in files:
        data = _read_json(f)
        if not isinstance(data, dict):
            continue
        if f.parent == checks and data.get("state") != "running":
            continue
        pid = data.get("pid")
        if isinstance(pid, int) and pid > 0:
            pids.append(pid)
    return pids


def session_pids(sessions: list[dict], root: Path) -> tuple[list[int], list[int]]:
    """(background pids whose cwd is under root, every other session's pid)."""
    bg, other = [], []
    for s in sessions:
        pid = s.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if s.get("kind") == "background":
            if _under(s.get("cwd"), root):
                bg.append(pid)
        else:
            other.append(pid)
    return bg, other


def protected_pids(table, caller: int, other_sessions: list[int], root: Path = ROOT) -> set[int]:
    prot = set(other_sessions)
    pid: int | None = caller
    while pid and pid not in prot:
        prot.add(pid)
        pid = table.parent(pid)
    words_list = protected_cmdlines(root)
    for pid in table.all_pids():
        cmd = table.cmdline(pid).casefold()
        if any(all(w in cmd for w in words) for words in words_list):
            prot.add(pid)
    return prot


def targets(root: Path, *, table, sessions: list[dict], caller: int | None = None) -> list[tuple[int, float]]:
    """(pid, create_time) of every process to hold, whole trees, parents before children."""
    bg, other = session_pids(sessions, root)
    prot = protected_pids(table, caller if caller is not None else os.getpid(), other, root)
    out: dict[int, float] = {}
    for top in bg + record_roots(root):
        stack = [top]
        while stack:
            pid = stack.pop(0)
            if pid in prot or pid in out:
                continue
            ct = table.create_time(pid)
            if ct is None:
                continue
            out[pid] = ct
            stack.extend(table.children(pid))
    return list(out.items())


def _record_path(root: Path) -> Path:
    return root / RECORD_REL


def read_record(root: Path) -> list[dict]:
    data = _read_json(_record_path(root))
    return [e for e in data if isinstance(e, dict) and "pid" in e and "create_time" in e] if isinstance(data, list) else []


def _write_record(root: Path, entries: list[dict]) -> None:
    path = _record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries), encoding="utf-8")
    os.replace(tmp, path)


def _same(table, entry: dict) -> bool:
    ct = table.create_time(entry["pid"])
    return ct is not None and abs(ct - entry["create_time"]) < 0.01


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class _RootLock:
    """One lock per installation root, across threads AND processes (the dispatcher's keeper thread, a tick, the
    `machine play|normal` command): NtSuspendProcess counts, so two passes racing on the record would suspend a
    process twice and resume it once. The cross-process part is an exclusive lock on one byte of a lock file."""

    def __init__(self, root: Path, thread_lock: threading.Lock) -> None:
        self._path = Path(root) / "var" / "agentops" / "hold.lock"
        self._thread_lock = thread_lock
        self._fh = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a+b")
            deadline = time.monotonic() + 60.0
            while True:
                try:
                    _lock_byte(self._fh)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()  # closing drops the byte lock
            finally:
                self._fh = None
        self._thread_lock.release()

    def __exit__(self, *exc) -> None:
        self._release()


def _lock_byte(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _root_lock(root: Path) -> _RootLock:
    key = str(Path(root).resolve()).casefold()
    with _LOCKS_GUARD:
        return _RootLock(root, _LOCKS.setdefault(key, threading.Lock()))


def keeper_pass(mode: str, root: Path = ROOT, *, table=None, sessions: Callable[[], list[dict]] | None = None,
                caller: int | None = None) -> dict:
    """One idempotent pass. Returns {"suspended": [pid...], "resumed": [pid...]}."""
    if mode not in ("play", "normal"):
        raise ValueError(f"mode must be play or normal, got {mode!r}")
    with _root_lock(root):
        return _pass_locked(mode, root, table, sessions, caller)


def _pass_locked(mode: str, root: Path, table, sessions, caller) -> dict:
    table = table or PsutilTable()
    record = read_record(root)
    done: dict[str, list[int]] = {"suspended": [], "resumed": []}
    if mode == "normal":
        for e in record:  # parents first, the reverse of the suspend order
            if _same(table, e):
                table.resume(e["pid"])
                done["resumed"].append(e["pid"])
        _write_record(root, [])
        return done
    held = [e for e in record if _same(table, e)]  # a dead or reused pid drops out of the record
    known = {e["pid"] for e in held}
    found = targets(root, table=table, sessions=(sessions or _DEFAULT_SESSIONS.get)(), caller=caller)
    fresh = [(p, ct) for p, ct in found if p not in known]
    for pid, ct in reversed(fresh):  # children first
        table.suspend(pid)
        done["suspended"].append(pid)
    _write_record(root, held + [{"pid": p, "create_time": ct} for p, ct in fresh])
    return done


class SessionCache:
    """The last `claude agents --json --all` listing. While a refresher thread runs, `get()` never waits for a
    listing: it returns the last one (empty before the first) and wakes the refresher. With no refresher thread (a
    one-off pass, the dispatcher tick without a keeper) it lists synchronously once the cache is older than
    `refresh_seconds`, as every pass did before."""

    def __init__(self, lister: Callable[[], list[dict]], *, refresh_seconds: float = SESSIONS_REFRESH_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._lister = lister
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._sessions: list[dict] | None = None
        self._at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh(self) -> None:
        sessions = self._lister()
        self._sessions, self._at = list(sessions), self._clock()

    def _stale(self) -> bool:
        return self._sessions is None or self._clock() - self._at > self._refresh_seconds

    def get(self) -> list[dict]:
        if self._thread is not None and self._thread.is_alive():
            if self._stale():
                self._wake.set()
            return list(self._sessions or [])
        if self._stale():
            self.refresh()
        return list(self._sessions or [])

    def start(self, wanted: Callable[[], bool], log: Callable[[str], None] = lambda m: None) -> None:
        """The refresher: list again every `refresh_seconds`, or at once when woken, while `wanted()`."""
        def loop() -> None:
            while not self._stop.is_set():
                self._wake.wait(self._refresh_seconds)
                self._wake.clear()
                if self._stop.is_set():
                    return
                if not wanted():
                    continue
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001 -- a failed listing keeps the last one
                    log(f"keeper: session listing failed: {exc!r}")
        self._thread = threading.Thread(target=loop, name="hold-sessions", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)


def _list_sessions() -> list[dict]:
    from .dispatch import ClaudeCli
    return ClaudeCli(cwd=ROOT, timeout=60.0).list_sessions()


_DEFAULT_SESSIONS = SessionCache(lambda: _list_sessions())


class Keeper:
    """The keeper pass on its own cadence inside one dispatcher process: a daemon thread reads the
    machine mode file and runs one pass every `interval` seconds, whatever the dispatcher tick is doing. A pass
    never overlaps another of the same root (`_root_lock`), the mode is read inside that lock, and the slow session
    listing is a second thread whose last result a pass uses."""

    def __init__(self, root: Path = ROOT, *, interval: float = KEEPER_SECONDS,
                 mode_reader: Callable[[], str] | None = None, table=None,
                 lister: Callable[[], list[dict]] | None = None, sessions_refresh: float = SESSIONS_REFRESH_SECONDS,
                 log: Callable[[str], None] = lambda m: None) -> None:
        self.root = Path(root)
        self.interval = interval
        self._mode_reader = mode_reader
        self._table = table
        self._log = log
        self.sessions = SessionCache(lister or (lambda: _list_sessions()), refresh_seconds=sessions_refresh)
        self.mode = "normal"  # the last mode a pass read
        self._unreadable_logged = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive():
            return
        self._stop.clear()
        self.sessions.start(wanted=lambda: self.mode == "play", log=self._log)
        self._thread = threading.Thread(target=self._loop, name="hold-keeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.sessions.stop()
        if self._thread is not None:
            self._thread.join(timeout)

    def run_once(self) -> dict | None:
        """One pass (None: nothing to do, or no readable mode). Never raises."""
        from . import machine_mode
        read = self._mode_reader or machine_mode.current_mode
        try:
            with _root_lock(self.root):
                got = machine_mode.read_gate(read)  # no mode to keep, no pass
                if got.mode is None:
                    if not self._unreadable_logged:
                        self._unreadable_logged = True
                        self._log(f"keeper: machine mode unreadable: {got.error}")
                    return None
                self._unreadable_logged = False
                mode = got.mode
                self.mode = mode
                if mode == "normal" and not read_record(self.root):
                    return None  # nothing held: no file write every few seconds
                done = _pass_locked(mode, self.root, self._table, self.sessions.get, None)
        except Exception as exc:  # noqa: BLE001
            self._log(f"keeper pass failed: {exc!r}")
            return None
        if done["suspended"] or done["resumed"]:
            self._log(f"keeper ({mode}): suspended {len(done['suspended'])}, resumed {len(done['resumed'])}")
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval)
