"""#352 unit 4: ``python -m tools.agentops test run <pytest args>``, one pytest selection, placed by ``place_decision``.

``route_selection(args, cwd, seams, ids=None)`` -> ``Route(where, reason, pc_ids, rest, plan, settings, ids)``: the
selection's ids are collected (``red_on_base.collect_ids``, bounded) unless the caller has them (the worker guard, from
the stored durations), ``place_decision.decide_place`` decides GitHub or the PC, and a GitHub decision on a tree that
GitHub would not test as it is (uncommitted tracked changes, or untracked files under the selection) goes to the PC.
The worker guard (``tools/hooks/worker_process_guard.py``) calls the same function, so the refusal and the run agree.

``run_test_run(args, cwd, seams)`` -> the exit code: on the PC it prints ``test run: PC (<reason>)`` and runs
``python -m pytest <args>``; on GitHub it prints ``test run: GitHub (<reason>)``, sends the ids (``merge_github.run``,
the way prequeue and red-on-base send theirs), runs the PC-only ids on the PC beside it, and prints the verdict. A
GitHub refusal or failed start falls back to the PC with one line ``test run: GitHub fallback to PC (<reason>)``; a
GitHub result that did not collect one id (``GithubSuiteResult.not_collected``, #341) runs that id alone on the PC and
sends the rest to GitHub again. Every PC run is bounded by the setting ``test_run_timeout_minutes`` (default 60): past
it the run is killed, exits 1 and prints one line.

Only settings decide, never a repo's name, a role, a path or a test name. ``Seams`` holds the injectable parts (tests).
"""
from __future__ import annotations

import argparse
import functools
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from tools.agentops import heavy_place, merge as m, merge_github, place_decision
from tools.agentops.github_suite import GithubSuiteResult
from tools.agentops.place_decision import TestPlace
from tools.agentops.red_on_base import SelectionDidNotRun, collect_ids
from tools.agentops.util import no_window_flags

COLLECT_S = 120.0  # the collect of a selection on the PC, bounded (the prompt's bound for ``test run``)


@dataclass
class Seams:
    """The injectable parts of a run (tests only). Each None is the real one, read when the run asks for it."""
    load_settings: Callable[[], dict] | None = None
    clock: Callable[[], float] | None = None                       # epoch seconds
    collect: Callable[[Path, list, float], list] | None = None     # (cwd, args, timeout_s) -> node ids
    deps: place_decision.Deps | None = None                        # place_decision's own reads
    dirty: Callable[[Path], bool] | None = None                    # uncommitted tracked changes?
    untracked: Callable[[Path, list], list] | None = None          # (cwd, args) -> untracked files under the selection
    head: Callable[[Path], str] | None = None                      # merge._head
    pc_only_files: Callable[[Path], list] | None = None            # merge._pc_only_files
    github_run: Callable | None = None                             # merge_github.run
    pc_run: Callable[[list, Path], int] | None = None              # (args, cwd) -> pytest exit code


@dataclass(frozen=True)
class Route:
    where: str                 # "github" or "pc"
    reason: str
    pc_ids: list = field(default_factory=list)
    rest: list = field(default_factory=list)     # the ids GitHub runs (the selection minus pc_ids)
    plan: object | None = None                   # merge_github.Plan on github
    settings: dict | None = None
    ids: list = field(default_factory=list)      # the whole collected selection


#: the caller's own verbosity: it changes pytest's collect output (a count instead of node ids), not the selection
_VERBOSITY = re.compile(r"^(-q+|-v+|--quiet|--verbose)$")


def collect_args(cwd: Path, args: list, timeout_s: float) -> list:
    """The node ids pytest collects for the selection (red_on_base's collector; the caller's -q/-v left out of it)."""
    words = [word for word in args if not _VERBOSITY.match(word)]
    return collect_ids(cwd, shlex.join(words), int(timeout_s))


def _dirty(cwd: Path) -> bool:
    """True when a tracked file differs from HEAD, or the check itself fails (then the PC runs it, the safe side)."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=no"],
                              capture_output=True, text=True, timeout=30, creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return True
    return proc.returncode != 0 or bool(proc.stdout.strip())


def untracked_under(cwd: Path, args: list) -> list:
    """The untracked files under the selection's paths (``git status --porcelain --untracked-files=normal -- <paths>``;
    a node id counts as its file). A selection with no path word (bare pytest, options only) names no path: []. A git
    failure raises (the caller says it)."""
    paths = [word.split("::", 1)[0] for word in args if word and not word.startswith("-")]
    if not paths:
        return []
    proc = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=normal", "--", *paths],
                          capture_output=True, text=True, timeout=30, creationflags=no_window_flags())
    if proc.returncode != 0:
        raise RuntimeError(f"git status exit {proc.returncode}")
    return [line[3:] for line in proc.stdout.splitlines() if line.startswith("?? ")]


def _unsent_reason(root: Path, args: list, seams: Seams) -> str:
    """Why GitHub would not test this tree as it is, "" when it would: uncommitted tracked changes, or untracked files
    under the selection (GitHub runs the commit, which lacks them)."""
    if (seams.dirty or _dirty)(root):
        return "uncommitted tracked changes: GitHub would test the commit, not the tree"
    try:
        untracked = (seams.untracked or untracked_under)(root, list(args))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return f"untracked check failed ({exc}): GitHub would test the commit, not the tree"
    if untracked:
        return (f"untracked files under the selection: GitHub would test the commit, not the tree "
                f"({', '.join(untracked[:3])})")
    return ""


def _inherit(stream):
    """The stream's file descriptor for the child, or None. A child started with CREATE_NO_WINDOW writes to the
    caller's stdout only through an explicit handle, so the descriptor is passed."""
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _timeout_seconds(settings: dict) -> float:
    """The PC run's bound: the setting ``test_run_timeout_minutes`` (default 60; a non-number or a value of 0 or less
    is the default too), in seconds."""
    value = settings.get("test_run_timeout_minutes", 60)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        value = 60
    return float(value) * 60


def _run_pc(args: list, cwd: Path, timeout_s: float | None = None) -> int:
    """pytest on the PC in cwd; past ``timeout_s`` (None: no bound) the child is killed, the exit is 1, one line says so."""
    try:
        return subprocess.run([sys.executable, "-m", "pytest", *args], cwd=str(cwd), creationflags=no_window_flags(),
                              stdout=_inherit(sys.stdout), stderr=_inherit(sys.stderr), timeout=timeout_s).returncode
    except subprocess.TimeoutExpired:
        print(f"test run: PC run killed: it ran past the {place_decision.human(timeout_s or 0)} bound "
              f"(test_run_timeout_minutes)", flush=True)
        return 1


def _pc(reason: str, settings: dict | None = None) -> Route:
    return Route("pc", reason, [], [], None, settings, [])


def route_selection(args: list, cwd, seams: Seams | None = None, timeout_s: float = COLLECT_S,
                    ids: list | None = None) -> Route:
    """Where the selection ``args`` runs, from ``place_decision.decide_place`` (see the module doc). ``ids`` is the
    selection's ids when the caller already has them (the worker guard: from the stored durations, no collect); None
    collects them here. Never raises for a failed read: a settings or collect failure is a PC route with its reason."""
    seams = seams or Seams()
    root = Path(cwd)
    settings = heavy_place.read_settings(seams.load_settings)
    if settings is None:
        return _pc("settings unreadable")
    if settings.get("github_suite") is not True:
        return _pc("github_suite off", settings)
    if ids is None:
        try:
            ids = list((seams.collect or collect_args)(root, list(args), timeout_s))
        except SelectionDidNotRun:
            return _pc("collect failed", settings)
    ids = list(ids)
    deps = seams.deps or place_decision.Deps()
    place = place_decision.decide_place(ids, settings, runner="linux", now=(seams.clock or time.time)(),
                                        deps=deps, root=root)
    if place.where == "github":
        unsent = _unsent_reason(root, list(args), seams)
        if unsent:
            place = TestPlace("pc", unsent, place.pc_seconds, place.github_seconds, place.pc_ids)
    if place.where != "github":
        return Route("pc", place.reason, [], [], None, settings, ids)
    read_plan = deps.plan_for or (lambda: merge_github.plan_for(settings, root, None))
    plan, _note = read_plan()
    pc_ids = list(place.pc_ids)
    rest = [i for i in ids if i not in set(pc_ids)]
    return Route("github", place.reason, pc_ids, rest, plan, settings, ids)


def _github(route: Route, cwd: Path, seams: Seams) -> GithubSuiteResult:
    """The rest of the selection on GitHub, the way prequeue and red-on-base send theirs. Never raises: a failure is a
    fallback result."""
    try:
        pc_files = (seams.pc_only_files or m._pc_only_files)(cwd)
        sha = (seams.head or m._head)(cwd)
        with tempfile.TemporaryDirectory() as scratch:
            return (seams.github_run or merge_github.run)(
                route.plan, cwd=cwd, suite_dir=cwd, sha=sha, branch=f"test-run-{(sha or 'x')[:8]}-{os.getpid()}",
                priority=False, settings=route.settings, alive=lambda: None,
                output_path=Path(scratch) / "suite.out", tests=len(route.rest), ids=route.rest,
                windows=m._windows_for(route.rest, pc_files))
    except Exception as exc:  # noqa: BLE001 -- a GitHub that cannot be used is a fallback, said in one line
        return GithubSuiteResult(status="fallback", reason=merge_github._error_reason(exc))


def run_test_run(args: list, *, cwd, seams: Seams | None = None) -> int:
    """Run the selection where it is placed; print the place line first; return the exit code (0 passed, 1 failed)."""
    seams = seams or Seams()
    root = Path(cwd)
    route = route_selection(list(args), root, seams)
    bound = _timeout_seconds(route.settings or {})
    pc_run = seams.pc_run or functools.partial(_run_pc, timeout_s=bound)
    if route.where != "github":
        print(f"test run: PC ({route.reason})", flush=True)
        return pc_run(list(args), root)

    print(f"test run: GitHub ({route.reason})", flush=True)
    pc_result: dict = {}
    beside = None
    if route.pc_ids:
        print(f"test run: PC beside GitHub: {len(route.pc_ids)} ids", flush=True)

        def part() -> None:
            try:
                pc_result["rc"] = pc_run(list(route.pc_ids), root)
            except Exception:  # noqa: BLE001 -- a PC part that cannot start is a failed part
                pc_result["rc"] = 1

        beside = threading.Thread(target=part, daemon=True)
        beside.start()

    result = _github(route, root, seams)
    if beside is not None:
        beside.join(bound)  # a PC part still running past the bound counts as failed (its own run is bounded too)
    rest_ids, stuck_rc = list(route.rest), None
    if result.status == "red" and result.not_collected in route.rest:
        # #341: GitHub sent one id it did not collect; that id alone runs on the PC, the rest goes to GitHub again
        stuck = result.not_collected
        print(f"test run: not collected on GitHub: {stuck}; that id on the PC, the rest again on GitHub", flush=True)
        stuck_rc = pc_run([stuck], root)
        rest_ids = [test_id for test_id in route.rest if test_id != stuck]
        result = (_github(replace(route, rest=rest_ids), root, seams) if rest_ids
                  else GithubSuiteResult(status="green"))
    if merge_github.goes_to_pc(result):
        reason = merge_github.pc_reason(result)
        print(f"test run: GitHub fallback to PC ({reason})", flush=True)
        return pc_run(list(args), root)  # the full selection on the PC: it covers the PC-only ids again

    rc = 0
    if result.status == "green":
        print(f"passed on GitHub: {len(rest_ids)} ids")
    else:
        print("failed on GitHub:")
        for test_id in result.failed_ids:
            print(f"  FAILED {test_id}")
        rc = 1
    if stuck_rc is not None:
        print(f"PC part: 1 id not collected on GitHub, {'passed' if stuck_rc == 0 else 'failed'}")
        rc = rc or (0 if stuck_rc == 0 else 1)
    if route.pc_ids:
        pc_rc = pc_result.get("rc", 1)
        print(f"PC part: {len(route.pc_ids)} ids, {'passed' if pc_rc == 0 else 'failed'}")
        rc = rc or (0 if pc_rc == 0 else 1)
    return rc


def add_subparser(sub) -> None:
    parser = sub.add_parser("test", help="run a test selection where place_decision says, GitHub or the PC (#352)")
    tsub = parser.add_subparsers(dest="test_command", required=True)
    run = tsub.add_parser("run", help="python -m tools.agentops test run <pytest args>: the selection, placed")
    run.add_argument("pytest_args", nargs=argparse.REMAINDER,
                     help="pytest's own words: test paths, node ids, -k, -m, -q ...")


def pytest_words_first(argv: list) -> list:
    """``argv`` with ``--`` put after ``test run`` when pytest's own words begin with an option (``-q``): argparse
    would take a leading option of the ``run`` parser as its own and fail. ``-h``/``--help`` and a ``--`` already
    there pass unchanged."""
    words = list(argv)
    if words[:2] == ["test", "run"] and len(words) > 2 and words[2].startswith("-"):
        if words[2] not in ("-h", "--help", "--"):
            words.insert(2, "--")
    return words


def run_test_command(args) -> int:
    words = list(args.pytest_args)
    if words[:1] == ["--"]:
        words = words[1:]  # the separator pytest_words_first put before pytest's own leading option
    return run_test_run(words, cwd=Path.cwd())
