"""``python -m tools.agentops map-run --commit <sha> [--detach]`` (#41): after a green landing, the whole test map is
rebuilt at the landed commit in the standing full-run worktree, so the map's ``map_commit`` moves to the tested tree.
This is the safety run (#195): at most one run is live; a landing that arrives while one runs leaves one pending sha
in ``safety_pending`` (the newest wins), and the live run picks it up when it ends. Every outcome is one line in the
merge log, after the ``map-run | `` prefix: ``map built | <testmondata> | <sha12> | <n> tests`` with
``safety run: green | <sha12>`` on green; ``safety run: red <ids> | <sha12>`` on a red suite (the red marker
``safety_red.json`` is written and holds the merge line); ``safety run: superseded | <sha12>`` for a pending sha a newer
landing replaced; or ``map failed | <sha12> | <first line of the error>`` for anything that is not a red suite.
#195: a build that saved its full output (``build_map``) appends `` | output <path>`` to its built, red and failed lines.
The core's module and subcommand are ``map_run`` / ``map-run`` (#53): ``full_run`` / ``full-run`` is a target's own name."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from .merge import (ROOT, MergeError, SAFETY_RED_FILE, _first_line, _merge_log_path, machine_holds,
                    resolve_suite_cmd, resolve_suite_shared, safety_red_path)
from .suite_worktree import FULL_WORKTREE, ensure_suite_worktree
from .test_select import MapBuildError, build_map, map_dir, read_meta
from .util import now, pid_alive

MAP_RUN_LOCK = "full_run.lock"  # the file name stays: hold.py and merge_status.py (and a target's own full_run) read it
SAFETY_PENDING = "safety_pending"  # the one sha a landing left while a run was live; the newest landing wins
#: ``-p no:cacheprovider`` crashes testmon; the map build drops that flag and nothing else of the merge suite's.
_NO_CACHE = re.compile(r"\s+-p\s+no:cacheprovider(?=\s|$)")
_FAILED_ID = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)  # pytest's short summary line: ``FAILED <nodeid> - why``
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SUITE_EXITED = "suite exited"  # the MapBuildError text of a red suite (build_map); other build errors are not red
MAX_IDS_SHOWN = 5


def map_suite_cmd(suite_cmd: str) -> str:
    return _NO_CACHE.sub("", suite_cmd)


def failed_test_ids(output: str) -> list[str]:
    """The ``FAILED <nodeid>`` ids in pytest's output, in order, without repeats; none when no line is parseable."""
    return list(dict.fromkeys(_FAILED_ID.findall(_ANSI.sub("", output))))


def _ids_text(ids: list[str]) -> str:
    """``a,b,c,d,e`` up to five ids, then ``... and <k> more``; ``(no ids)`` when there are none."""
    if not ids:
        return "(no ids)"
    more = len(ids) - MAX_IDS_SHOWN
    shown = ",".join(ids[:MAX_IDS_SHOWN])
    return shown + (f" and {more} more" if more > 0 else "")


def resolve_map_build_bound() -> float:
    """The map build's suite-run bound in seconds (#201), from the settings file's ``map_build_bound_minutes``.
    A key the file leaves out gets its ``core_settings.DEFAULTS`` value (120 min); an invalid value (zero, negative,
    not a finite number, a bool or a string) falls back to that same default. A settings file that cannot load raises
    (not swallowed): ``run_map_run`` logs it as its one ``map failed`` line."""
    from .core_settings import DEFAULTS, load as load_settings
    default = DEFAULTS["map_build_bound_minutes"]
    value = load_settings().get("map_build_bound_minutes", default)
    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    return float(value if valid else default) * 60


def _take_lock(mdir: Path) -> Path | None:
    """The map dir's lock file, created by this process; None while another live map run holds it.
    A lock whose pid is dead (or unreadable) is stale and taken over."""
    mdir.mkdir(parents=True, exist_ok=True)
    lock = mdir / MAP_RUN_LOCK
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = None
            if pid_alive(pid):
                return None
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return lock
    return None


def _log(log_path: Path, what: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | map-run | {what}\n")


def _post_pending(mdir: Path, sha: str, log_path: Path) -> None:
    """Leave ``sha`` as the one pending sha: a replaced older one is logged superseded (#195)."""
    mdir.mkdir(parents=True, exist_ok=True)
    pending = mdir / SAFETY_PENDING
    try:
        older = pending.read_text(encoding="utf-8").strip()
    except OSError:
        older = ""
    if older and older != sha:
        _log(log_path, f"safety run: superseded | {older[:12]}")
    tmp = mdir / f"{SAFETY_PENDING}.{os.getpid()}.tmp"
    tmp.write_text(sha, encoding="utf-8")
    os.replace(tmp, pending)


def _pop_pending(mdir: Path) -> str | None:
    """Take the pending sha out by a rename, so a landing posting meanwhile is never deleted with it."""
    taking = mdir / f"{SAFETY_PENDING}.taking"
    try:
        os.replace(mdir / SAFETY_PENDING, taking)
    except FileNotFoundError:
        return None
    try:
        return taking.read_text(encoding="utf-8").strip() or None
    finally:
        taking.unlink(missing_ok=True)


def _output_part(path) -> str:
    """`` | output <path>`` for a build's saved output (#195); nothing when the build wrote none."""
    return f" | output {path}" if path else ""


def _write_red_marker(mdir: Path, marker: dict) -> None:
    mdir.mkdir(parents=True, exist_ok=True)
    tmp = mdir / f"{SAFETY_RED_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(marker), encoding="utf-8")
    os.replace(tmp, mdir / SAFETY_RED_FILE)


def _safety_run(sha: str, *, root: Path, mdir: Path, suite_cmd: str, shared, log_path: Path) -> int:
    """One build at ``sha``: a green map is written and logged; a red suite logs its ids and writes the marker."""
    short = sha[:12]
    try:
        suite_dir = ensure_suite_worktree(root, sha, shared=shared, lock=None, name=FULL_WORKTREE)
        before = (read_meta(mdir) or {}).get("last_green_commit")
        meta = build_map(suite_dir, sha, mdir, map_suite_cmd(suite_cmd), bound=resolve_map_build_bound())
    except MapBuildError as exc:
        text = str(exc)
        saved = _output_part(getattr(exc, "output", None))
        if not text.startswith(_SUITE_EXITED):
            _log(log_path, f"map failed | {short} | {_first_line(text) or type(exc).__name__}{saved}")
            return 1
        ids = failed_test_ids(text)
        _write_red_marker(mdir, {"sha": sha, "ids": ids, "map_commit": before})
        _log(log_path, f"safety run: red {_ids_text(ids)} | {short}{saved}")
        return 1
    except Exception as exc:  # noqa: BLE001 -- a failed build is one log line, never a silent crash
        _log(log_path, f"map failed | {short} | {_first_line(str(exc)) or type(exc).__name__}"
             + _output_part(getattr(exc, "output", None)))  # #195: a failure after the write names its file
        return 1
    safety_red_path(root).unlink(missing_ok=True)  # a green run clears the red marker
    _log(log_path, f"map built | {mdir / 'testmondata'} | {short} | {meta['total']} tests"
         + _output_part(meta.get("output")))
    _log(log_path, f"safety run: green | {short}")
    return 0


def run_map_run(sha: str, *, root: Path, suite_cmd: str, shared, log_path: Path) -> int:
    """Rebuild the whole map at ``sha`` from the full-run worktree; 0 when the run ends or a live run takes the sha
    (#195: pending). The map is always the one under ``root`` (the main checkout); the worktree is only where the suite
    runs. Never raises: each outcome is one log line. The live run drains the pending sha after its own build."""
    mdir = map_dir(root)
    if machine_holds():
        _log(log_path, f"map skipped | {sha[:12]} | machine mode play")
        return 0
    _post_pending(mdir, sha, log_path)  # posted before the lock is tried: a run ending meanwhile sees it
    rc = 0
    while True:
        try:
            lock = _take_lock(mdir)
        except OSError as exc:
            _log(log_path, f"map failed | {sha[:12]} | {_first_line(str(exc)) or type(exc).__name__}")
            return 1
        if lock is None:
            return rc  # a live run holds the map: it picks the pending sha up
        try:
            nxt = _pop_pending(mdir)
            while nxt is not None:
                rc = _safety_run(nxt, root=root, mdir=mdir, suite_cmd=suite_cmd, shared=shared, log_path=log_path)
                nxt = _pop_pending(mdir)
        finally:
            lock.unlink(missing_ok=True)
        if not (mdir / SAFETY_PENDING).exists():  # a landing that posted while the lock was released runs too
            return rc


def run_map_run_command(args) -> int:
    """The ``map-run`` subcommand: the merge suite of the settings file, the main checkout (``--cwd``) by default."""
    log_path = _merge_log_path()
    try:
        suite_cmd = resolve_suite_cmd(None)
    except MergeError as exc:
        _log(log_path, f"map failed | {args.commit[:12]} | {_first_line(str(exc))}")
        return 1
    root = args.cwd if args.cwd is not None else ROOT
    return run_map_run(args.commit, root=root, suite_cmd=suite_cmd, shared=resolve_suite_shared(),
                       log_path=log_path)
