"""One shared reservation of test-suite workers: the merge suite and the full run claim their pytest
workers from the same file, so two suites starting at once never run more workers than the cap together.

A claim is ``(owner, pid, workers, ts)``; it ends when its owner releases it, its pid is gone, or its lease
(``LEASE_S``) runs out. A caller with no headroom polls for it up to ``WAIT_S``, then takes one worker (a
suite never blocks for ever). Stdlib only; the file is read and written under an OS lock.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .util import pid_alive

ROOT = Path(__file__).resolve().parents[2]
LEASE_S = 5400.0   # a claim older than this is dead whatever its pid says (a suite never runs this long)
WAIT_S = 600.0     # a caller with no headroom polls this long, then takes one worker
POLL_S = 5.0
_LOCK_WAIT_S = 10.0

if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def reservation_file() -> Path:
    """``AGENTOPS_SUITE_RESERVATIONS`` (tests) or ``var/suite_workers.json`` (``var`` is shared by every worktree)."""
    override = os.environ.get("AGENTOPS_SUITE_RESERVATIONS")
    return Path(override) if override else ROOT / "var" / "suite_workers.json"


def _locked(path: Path, change: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any]]) -> Any:
    """Read the file under ``<file>.lock``, let ``change(rows) -> (rows, result)`` edit it, write it back atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                _lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"cannot lock {path}")
                time.sleep(0.02)
        try:
            try:
                rows = json.loads(path.read_text(encoding="utf-8-sig"))
                rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            except Exception:
                rows = []
            rows, result = change(rows)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            os.replace(tmp, path)
            return result
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass
    finally:
        os.close(fd)


def reserve(wanted: int, cap: int, owner: str, *, path: Path | None = None, pid: int | None = None,
            now: float | None = None, alive: Callable[[int], bool] | None = None,
            force: bool = False) -> tuple[int, int]:
    """Claim ``min(wanted, cap - what the other live owners hold)`` workers for ``owner`` (its own earlier
    claim is replaced). Returns ``(granted, held_by_others)``; granted 0 = no headroom (nothing is recorded).
    ``force`` grants at least 1 even with no headroom (the one-worker floor, recorded)."""
    pid = os.getpid() if pid is None else pid
    stamp = time.time() if now is None else now
    is_alive = alive or pid_alive

    def change(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], tuple[int, int]]:
        live = [r for r in rows if r.get("owner") != owner
                and stamp - float(r.get("ts", 0)) < LEASE_S and is_alive(int(r.get("pid", 0)))]
        held = sum(int(r.get("workers", 0)) for r in live)
        granted = max(0, min(int(wanted), int(cap) - held))
        if force:
            granted = max(1, granted)
        if granted > 0:
            live.append({"owner": owner, "pid": pid, "workers": granted, "ts": stamp})
        return live, (granted, held)

    return _locked(path or reservation_file(), change)


def release(owner: str, *, path: Path | None = None) -> None:
    """Drop ``owner``'s claim (its suite ended)."""
    _locked(path or reservation_file(), lambda rows: ([r for r in rows if r.get("owner") != owner], None))


def reserve_waiting(wanted: int, cap: int, owner: str, *, wait_s: float = WAIT_S, poll_s: float = POLL_S,
                    sleep: Callable[[float], None] | None = None,
                    clock: Callable[[], float] = time.monotonic, **kwargs: Any) -> tuple[int, int, float]:
    """``reserve`` that polls every ``poll_s`` for headroom up to ``wait_s``, then takes the one-worker floor.
    A caller that wants one worker (memory cut it) never waits. Returns ``(granted, held_by_others, waited_s)``."""
    start = clock()
    while True:
        waited = clock() - start
        last = waited >= wait_s or int(wanted) <= 1
        granted, held = reserve(wanted, cap, owner, force=last, **kwargs)
        if granted > 0:
            return granted, held, waited
        (sleep or time.sleep)(poll_s)
