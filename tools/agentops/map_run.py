"""``python -m tools.agentops map-run --commit <sha> [--detach]`` (#41): after a green landing, the whole test map is
rebuilt at the landed commit in the standing full-run worktree, so the map's ``map_commit`` moves to the tested tree.
This is the safety run (#195): at most one run is live; a landing that arrives while one runs leaves one pending sha
in ``safety_pending`` (the newest wins), and the live run picks it up when it ends. Every outcome is one line in the
merge log, after the ``map-run | `` prefix: ``map built | <testmondata> | <sha12> | <n> tests`` with
``safety run: green | <sha12>`` on green; ``safety run: red <ids> | <sha12>`` on a red suite (the red marker
``safety_red.json`` is written and holds the merge line); ``safety run: superseded | <sha12>`` for a pending sha a newer
landing replaced; ``map skipped | <sha12> | not on main`` for a sha not reachable from origin/main (#252, nothing built);
or ``map failed | <sha12> | <first line of the error>`` for anything that is not a red suite.
#195: a build that saved its full output (``build_map``) appends `` | output <path>`` to its built, red and failed lines.
#343: before it builds, the run asks ``heavy_place.decide("map-build", ...)`` where: ``map-build on github`` (the map is
built on GitHub and installed; red there holds the merge line as a red PC suite does; a GitHub that cannot run it is
never red: the PC builds, ``map-build on pc (github fallback: <reason>)``), ``map-build on pc (<reason>)``, or
``map-build on skip (coalesced)`` (a build started within ``map_build_min_interval_minutes``: the sha stays pending, the
newest wins, and one detached delayed re-run, ``map-run --after-seconds``, builds it once the interval has passed).
A coalesced sha is always built: the re-run's mark (``map_rerun.json``) holds its pid, start time and ``due_at``, and a
waiter whose process is gone, whose pid was reused, or that is overdue by ``RERUN_STALE_S`` is replaced by the sweep that
ends every ``run_map_run``; a waiter that wakes with the machine in play arms its successor (``MAX_WAKE_RETRIES``).
The core's module and subcommand are ``map_run`` / ``map-run`` (#53): ``full_run`` / ``full-run`` is a target's own name."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import heavy_place, map_github
from .core_format import human_duration
from .merge import (ROOT, MergeError, SAFETY_RED_FILE, _first_line, _merge_log_path, machine_holds,
                    resolve_suite_cmd, resolve_suite_shared, safety_red_path)
from .suite_worktree import FULL_WORKTREE, ensure_suite_worktree
from .test_copy_check import git_env
from .test_select import MapBuildError, build_map, map_dir, read_meta
from .util import no_window_flags, now, pid_alive
from .worktree import git

MAP_RUN_LOCK = "full_run.lock"  # the file name stays: hold.py and merge_status.py (and a target's own full_run) read it
SAFETY_PENDING = "safety_pending"  # the one sha a landing left while a run was live; the newest landing wins
#: ``-p no:cacheprovider`` crashes testmon; the map build drops that flag and nothing else of the merge suite's.
_NO_CACHE = re.compile(r"\s+-p\s+no:cacheprovider(?=\s|$)")
_FAILED_ID = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)  # pytest's short summary line: ``FAILED <nodeid> - why``
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SUITE_EXITED = "suite exited"  # the MapBuildError text of a red suite (build_map); other build errors are not red
MAX_IDS_SHOWN = 5
RERUN_MARK = "map_rerun.json"  # the delayed re-run waiting: pid, start, sha, due_at, retries; at most one live
MAX_RERUN_WAIT_S = 2 * 3600  # a delayed re-run never waits longer than this, whatever the setting says
RERUN_MARGIN_S = 5  # past the interval's end, so the re-run's own check finds it passed
RERUN_STALE_S = 10 * 60  # a mark older than its due time by this much is not a live waiter, whatever its pid says
WAKE_RETRY_S = 5 * 60  # a waiter that wakes while the machine is in play arms its successor this far ahead
MAX_WAKE_RETRIES = 3  # then ``map pending | <sha> | machine busy``; the next landing's sweep starts a waiter again
GIT_TIMEOUT_S = 60


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


def sha_on_main(root: Path, sha: str) -> bool:
    """Whether ``sha`` may be built (#252): reachable from ``origin/main`` in ``root``. A repo with no ``origin/main``
    ref (a test repo) is checked by its object alone. An absent object, or a commit off main, is False."""
    if git("rev-parse", "--verify", "--quiet", "refs/remotes/origin/main", cwd=root).returncode == 0:
        return git("merge-base", "--is-ancestor", sha, "origin/main", cwd=root).returncode == 0
    return git("cat-file", "-e", f"{sha}^{{commit}}", cwd=root).returncode == 0


def _post_pending(mdir: Path, sha: str, log_path: Path, cwd_arg: str | None = None) -> None:
    """Leave ``sha`` as the one pending sha: a replaced older one is logged superseded (#195). The superseded line also
    names the poster (ppid, cwd, the ``--cwd`` argument) so a stray sha can be traced (#252)."""
    mdir.mkdir(parents=True, exist_ok=True)
    pending = mdir / SAFETY_PENDING
    try:
        older = pending.read_text(encoding="utf-8").strip()
    except OSError:
        older = ""
    if older and older != sha:
        _log(log_path, f"safety run: superseded | {older[:12]} | poster ppid={os.getppid()} "
             f"cwd={os.getcwd()} --cwd={cwd_arg if cwd_arg is not None else 'default'}")
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


def load_place_settings() -> dict:
    """The installation's settings file (raises when it cannot load: the place is then the PC, ``github settings
    unreadable``)."""
    from .core_settings import load
    return load()


def _github_branch(sha: str) -> str:
    """The branch name the map build takes on the test copy (or the repo's own origin)."""
    return f"map-build/{sha[:12]}"


@contextlib.contextmanager
def github_ref(cwd, branch: str, sha: str, settings: dict, log: Callable[[str], None] | None = None):
    """A local branch ``branch`` at ``sha`` for the send (the merge suite's way, ``merge_github.run``); the repo's own
    origin gets it pushed, so it is deleted there too afterwards. Clean-up is best effort and never changes a result;
    a delete that failed is one ``log`` line."""
    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True, env=git_env(),
                       creationflags=no_window_flags(), timeout=GIT_TIMEOUT_S)

    run("update-ref", f"refs/heads/{branch}", sha)
    try:
        yield
    finally:
        steps = [("update-ref", "-d", f"refs/heads/{branch}")]
        if map_github.is_own(settings):
            steps.insert(0, ("push", "origin", "--delete", branch))
        for args in steps:
            try:
                run(*args)
            except Exception as exc:  # noqa: BLE001
                if log is not None:
                    log(f"map-build branch not deleted | {branch} | {_first_line(str(exc)) or type(exc).__name__}")


def _spawn_rerun(sha: str, wait_seconds: float, cwd_arg: str | None) -> int:
    """The delayed re-run: ``map-run --commit <sha> --after-seconds <n>`` through the repo's hidden launcher
    (``detach.launch``, no window); returns the launcher's pid."""
    from . import detach
    argv = [sys.executable, "-m", "tools.agentops", "map-run", "--commit", sha,
            "--after-seconds", str(int(wait_seconds)), *(["--cwd", cwd_arg] if cwd_arg else [])]
    _result, pid = detach.launch(argv, "map-run", None)
    return pid


@dataclass
class PlaceDeps:
    """Every seam of the place decision, injectable: the settings, ``heavy_place``'s own seams, the GitHub build, the
    local branch it is sent from, the delayed re-run's spawn and the clock."""
    load_settings: Callable[[], dict] = field(default=lambda: load_place_settings())
    place: heavy_place.Deps = field(default_factory=heavy_place.Deps)
    github: Callable = map_github.build_on_github
    github_ref: Callable = github_ref
    spawn_rerun: Callable[[str, float, str | None], int] = _spawn_rerun
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


def _decide(root: Path, deps: PlaceDeps) -> tuple[heavy_place.Place, dict]:
    """Where this build goes and the settings that decided it. Unreadable settings are the PC."""
    try:
        settings = deps.load_settings()
    except Exception:  # noqa: BLE001 - like the merge suite: an unreadable setting is "off", said in the log
        reason = "github settings unreadable"
        return heavy_place.Place("pc", reason, f"map-build on pc ({reason})"), {}
    return heavy_place.decide("map-build", settings, now=deps.clock(), root=root, deps=deps.place), settings


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _start_time(pid: int) -> float | None:
    """The process' create time (psutil), or None when psutil is absent or the process is gone: it tells a reused pid
    from the waiter that was started."""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001
        return None


def _read_mark(mdir: Path) -> dict:
    try:
        mark = json.loads((mdir / RERUN_MARK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return mark if isinstance(mark, dict) else {}


def _rerun_alive(mdir: Path, at: datetime) -> bool:
    """A waiter is live when its mark names a live pid that started when the mark says (a reused pid is another process)
    and the mark is not older than its ``due_at`` plus ``RERUN_STALE_S`` (a waiter that should have woken is gone)."""
    mark = _read_mark(mdir)
    try:
        pid = int(mark["pid"])
        due = datetime.fromisoformat(mark["due_at"])
    except (KeyError, ValueError, TypeError):
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if at > due + timedelta(seconds=RERUN_STALE_S) or not pid_alive(pid):
        return False
    recorded = mark.get("start")
    if isinstance(recorded, (int, float)):
        current = _start_time(pid)
        if current is not None and abs(current - recorded) > 2.0:
            return False
    return True


def _start_waiter(sha: str, wait: float, retries: int, *, mdir: Path, log_path: Path, cwd_arg: str | None,
                  deps: PlaceDeps, note: str) -> bool:
    """One delayed re-run for ``sha`` in ``wait`` seconds, its mark written; False (and one line) when it cannot start."""
    wait = min(wait, MAX_RERUN_WAIT_S)
    try:
        pid = deps.spawn_rerun(sha, wait, cwd_arg)
    except Exception as exc:  # noqa: BLE001 - no re-run: the next landing's sweep starts one
        _log(log_path, f"map pending | {sha[:12]} | re-run not started: {_first_line(str(exc)) or type(exc).__name__}")
        return False
    at = deps.clock()
    _write_json_atomic(mdir / RERUN_MARK, {"pid": pid, "start": _start_time(pid), "sha": sha, "retries": retries,
                                           "due_at": (at + timedelta(seconds=wait)).isoformat()})
    _log(log_path, f"map pending | {sha[:12]} | {note.format(wait=human_duration(wait))}")
    return True


def _coalesce(sha: str, *, root: Path, mdir: Path, settings: dict, log_path: Path, cwd_arg: str | None,
              deps: PlaceDeps) -> None:
    """A coalesced build: the sha is the one pending (the newest wins) and one delayed re-run will build it. A re-run
    that is already waiting reads the pending sha when it wakes, so none is started beside it."""
    heavy_place.note_pending(root, sha)
    _log(log_path, "map-build on skip (coalesced)")
    if _rerun_alive(mdir, deps.clock()):
        _log(log_path, f"map pending | {sha[:12]} | the waiting re-run builds it")
        return
    wait = heavy_place.seconds_until_due(settings, now=deps.clock(), root=root, deps=deps.place) + RERUN_MARGIN_S
    _start_waiter(sha, wait, 0, mdir=mdir, log_path=log_path, cwd_arg=cwd_arg, deps=deps,
                  note="built in {wait} or by the next landing")


def _sweep(root: Path, mdir: Path, log_path: Path, cwd_arg: str | None, deps: PlaceDeps) -> None:
    """A sha left pending with no live waiter (its process stopped, the machine rebooted, its pid was reused) gets one:
    a coalesced sha is always built. Runs at the end of every ``run_map_run``, so every landing checks. Never raises."""
    try:
        sha = heavy_place.pending(root)
        if sha is None or _rerun_alive(mdir, deps.clock()):
            return
        try:
            settings = deps.load_settings()
        except Exception:  # noqa: BLE001 - the default interval is used
            settings = {}
        wait = heavy_place.seconds_until_due(settings, now=deps.clock(), root=root, deps=deps.place) + RERUN_MARGIN_S
        _start_waiter(sha, wait, 0, mdir=mdir, log_path=log_path, cwd_arg=cwd_arg, deps=deps,
                      note="no waiting re-run, started: built in {wait} or by the next landing")
    except Exception as exc:  # noqa: BLE001 - the sweep never changes a run's result
        _log(log_path, f"map pending | sweep failed: {_first_line(str(exc)) or type(exc).__name__}")


def _github_build(sha: str, *, suite_dir: Path, mdir: Path, settings: dict, repo_name: str,
                  deps: PlaceDeps, log_path: Path) -> map_github.MapBuildResult:
    """The map built on GitHub. Anything that raises is a fallback (``error: <first line>``), never red."""
    branch = _github_branch(sha)
    try:
        with deps.github_ref(suite_dir, branch, sha, settings, log=lambda text: _log(log_path, text)):
            return deps.github(root=suite_dir, branch=branch, sha=sha, settings=settings, map_dir=mdir,
                               repo_name=repo_name or None)
    except Exception as exc:  # noqa: BLE001
        return map_github.MapBuildResult(status="fallback",
                                         reason=f"error: {_first_line(str(exc)) or type(exc).__name__}")


def _safety_run(sha: str, *, root: Path, mdir: Path, suite_cmd: str, shared, log_path: Path,
                deps: PlaceDeps | None = None, cwd_arg: str | None = None) -> int:
    """One build at ``sha``: a green map is written and logged; a red suite logs its ids and writes the marker."""
    deps = deps or PlaceDeps()
    short = sha[:12]
    try:
        if not sha_on_main(root, sha):  # #252: a sha that is not on origin/main is never built, nothing is touched
            _log(log_path, f"map skipped | {short} | not on main")
            return 0
        place, settings = _decide(root, deps)
        if place.where == "skip":
            _coalesce(sha, root=root, mdir=mdir, settings=settings, log_path=log_path, cwd_arg=cwd_arg, deps=deps)
            return 0
        heavy_place.note_build_start(root, deps.clock())
        suite_dir = ensure_suite_worktree(root, sha, shared=shared, lock=None, name=FULL_WORKTREE)
        before = (read_meta(mdir) or {}).get("last_green_commit")
        meta, run_note = None, ""
        if place.where == "github":
            built = _github_build(sha, suite_dir=suite_dir, mdir=mdir, settings=settings,
                                  repo_name=place.repo_name, deps=deps, log_path=log_path)
            if built.status == "green":
                _log(log_path, place.line)
                meta, run_note = read_meta(mdir) or {}, f" | github run {built.run_id}"
            elif built.status == "red":
                _log(log_path, place.line)
                ids = list(built.failed_ids)
                _write_red_marker(mdir, {"sha": sha, "ids": ids, "map_commit": before})
                _log(log_path, f"safety run: red {_ids_text(ids)} | {short} | github run {built.run_id}")
                return 1
            else:  # rule 5: a GitHub that cannot run it is a place problem, never a red suite
                reason = f"github fallback: {built.reason}"
                place = heavy_place.Place("pc", reason, f"map-build on pc ({reason})")
        if meta is None:
            _log(log_path, place.line)
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
         + _output_part(meta.get("output")) + run_note)
    _log(log_path, f"safety run: green | {short}")
    return 0


def run_map_run(sha: str, *, root: Path, suite_cmd: str, shared, log_path: Path, cwd_arg: str | None = None,
                place_deps: PlaceDeps | None = None) -> int:
    """``_run_map_run``, then the sweep for a coalesced sha left pending with no live waiter (#343)."""
    place_deps = place_deps or PlaceDeps()
    mdir = map_dir(root)
    marked = _read_mark(mdir)
    try:
        return _run_map_run(sha, root=root, suite_cmd=suite_cmd, shared=shared, log_path=log_path, cwd_arg=cwd_arg,
                            place_deps=place_deps)
    finally:
        if _read_mark(mdir) == marked:  # a waiter this run started (or took over) needs no sweep
            _sweep(root, mdir, log_path, cwd_arg, place_deps)


def _run_map_run(sha: str, *, root: Path, suite_cmd: str, shared, log_path: Path, cwd_arg: str | None,
                 place_deps: PlaceDeps) -> int:
    """Rebuild the whole map at ``sha`` from the full-run worktree; 0 when the run ends or a live run takes the sha
    (#195: pending). The map is always the one under ``root`` (the main checkout); the worktree is only where the suite
    runs. Never raises: each outcome is one log line. The live run drains the pending sha after its own build.
    ``cwd_arg`` is the ``--cwd`` the caller was given, named in the superseded line (#252) and handed to a delayed
    re-run. ``place_deps`` (tests) holds the seams of the place decision (#343)."""
    mdir = map_dir(root)
    if machine_holds():
        _log(log_path, f"map skipped | {sha[:12]} | machine mode play")
        return 0
    _post_pending(mdir, sha, log_path, cwd_arg)  # posted before the lock is tried: a run ending meanwhile sees it
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
                rc = _safety_run(nxt, root=root, mdir=mdir, suite_cmd=suite_cmd, shared=shared, log_path=log_path,
                                 deps=place_deps, cwd_arg=cwd_arg)
                nxt = _pop_pending(mdir)
        finally:
            lock.unlink(missing_ok=True)
        if not (mdir / SAFETY_PENDING).exists():  # a landing that posted while the lock was released runs too
            return rc


def run_map_run_delayed(commit: str, after_seconds: float, *, root: Path, suite_cmd: str, shared, log_path: Path,
                        cwd_arg: str | None = None, place_deps: PlaceDeps | None = None,
                        sleep: Callable[[float], None] = time.sleep) -> int:
    """The delayed re-run of a coalesced build (#343): wait ``after_seconds`` (at most ``MAX_RERUN_WAIT_S``), clear the
    waiter's mark, then build the sha that is pending then (the newest main; ``commit`` is only the one it was started
    for). Nothing pending means a build took it meanwhile: nothing runs."""
    sleep(min(max(after_seconds, 0.0), MAX_RERUN_WAIT_S))
    place_deps = place_deps or PlaceDeps()
    mdir = map_dir(root)
    retries = _read_mark(mdir).get("retries")
    retries = retries if isinstance(retries, int) else 0
    (mdir / RERUN_MARK).unlink(missing_ok=True)  # before the build: a build coalesced again starts a new waiter
    sha = heavy_place.pending(root)
    if sha is None:
        return 0
    if machine_holds():  # the sha stays pending: arm the successor, a few times, then leave it to the next landing
        if retries >= MAX_WAKE_RETRIES:
            _log(log_path, f"map pending | {sha[:12]} | machine busy")
        else:
            _start_waiter(sha, WAKE_RETRY_S, retries + 1, mdir=mdir, log_path=log_path, cwd_arg=cwd_arg,
                          deps=place_deps, note="machine busy, tried again in {wait}")
        return 0
    return run_map_run(sha, root=root, suite_cmd=suite_cmd, shared=shared, log_path=log_path, cwd_arg=cwd_arg,
                       place_deps=place_deps)


def run_map_run_command(args) -> int:
    """The ``map-run`` subcommand: the merge suite of the settings file, the main checkout (``--cwd``) by default.
    With ``--after-seconds`` it is a delayed re-run (``run_map_run_delayed``)."""
    log_path = _merge_log_path()
    try:
        suite_cmd = resolve_suite_cmd(None)
    except MergeError as exc:
        _log(log_path, f"map failed | {args.commit[:12]} | {_first_line(str(exc))}")
        return 1
    root = args.cwd if args.cwd is not None else ROOT
    cwd_arg = None if args.cwd is None else str(args.cwd)
    if getattr(args, "after_seconds", None) is not None:
        return run_map_run_delayed(args.commit, args.after_seconds, root=root, suite_cmd=suite_cmd,
                                   shared=resolve_suite_shared(), log_path=log_path, cwd_arg=cwd_arg)
    return run_map_run(args.commit, root=root, suite_cmd=suite_cmd, shared=resolve_suite_shared(),
                       log_path=log_path, cwd_arg=cwd_arg)
