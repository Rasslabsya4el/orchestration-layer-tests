"""#319: `red-on-base` marks the tests already red on a base commit, so a unit is not blamed for them.

It runs the pytest selection in the base worktree (nothing there changes), with the plugin's report on, and appends
each red id not yet listed to the list file as `skip <id> # Tests red on main before #N`. The plugin
(tools/agentops/red_on_main_plugin.py) skips the listed ids in every later run.

Exit codes: 0 done (or no red), 1 the selection did not run (a usage error, a crash, the timeout), 2 `--ticket`
differs from the ticket the list names (nothing written), 3 no ticket known (nothing written; the ids are printed
for a new ticket).

#343 u6: the base run goes where ``heavy_place.decide("red-on-base")`` says. On GitHub (``github_suite`` on, a plan
exists) the ids pytest collects on the PC go as the run's ids and the reds are the failed ids GitHub names; a GitHub
fallback, or no id collected, runs the PC as before. The one line ``red-on-base on github`` or
``red-on-base on pc (<reason>)`` is printed. Out of scope (#341): PC-only tests that read files outside the public
test copy are not handled on the GitHub route.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from tools.agentops import heavy_place, merge as m, merge_github
from tools.agentops.github_suite import GithubSuiteResult
from tools.agentops.util import nested_env, no_window_flags
from tools.agentops.red_on_main_plugin import PLUGIN, REPORT_ENV, format_line, normalise, read_entries

DEFAULT_FILE = "tests/red_on_main.txt"
DEFAULT_TIMEOUT_S = 1800
TICKET_RE = re.compile(r"before #(\d+)")
HEADER = (
    b"# Tests red on main before a ticket: skipped with that reason, not fixed (#319).\n"
    b"# Written by `python -m tools.agentops red-on-base`; one `skip <nodeid or path> # reason` line each.\n"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
PYTEST_OK, PYTEST_REDS, PYTEST_NOTHING_SELECTED = 0, 1, 5


class SelectionDidNotRun(Exception):
    """The pytest selection did not run to its end: a usage error, a crash, or the timeout."""


def human(seconds: int) -> str:
    """Seconds under ~90, then minutes (core rule 7: human text never shows a big count of seconds)."""
    return f"{seconds} s" if seconds < 90 else f"{seconds // 60} min"


def split_selection(tests: str) -> list[str]:
    """The pytest arguments of --tests. POSIX escapes are off (a Windows path keeps its backslashes), quotes are
    stripped, and a non-option token has its backslashes turned into '/'."""
    args: list[str] = []
    for token in shlex.split(tests, posix=False):
        token = token.strip("\"'")
        args.append(token if token.startswith("-") else token.replace("\\", "/"))
    return args


def _base_env(extra: dict[str, str]) -> dict[str, str]:
    """The child's environment: none of an outer build's variables (util.nested_env, #195), UTF-8, this repo on the path."""
    pythonpath = os.pathsep.join(p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    # the suite's own PYTEST_ADDOPTS (plan and events plugins) is that suite's, not the base run's: its plugins
    # name hooks only the suite's own xdist run defines
    env = nested_env({"PYTHONPATH": pythonpath, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **extra})
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _child_env(report: Path) -> dict[str, str]:
    return _base_env({REPORT_ENV: str(report)})


def collect_ids(cwd: Path, tests: str, timeout_s: int) -> list[str]:
    """The node ids pytest collects for the selection in cwd, in pytest's order, each once; [] when nothing is selected
    (exit 5). Collection only: no test is run. Any other exit code, and the timeout, raise SelectionDidNotRun."""
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "--collect-only", "-q",
           *split_selection(tests)]
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=_base_env({}), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s, creationflags=no_window_flags())
    except subprocess.TimeoutExpired as exc:
        raise SelectionDidNotRun(f"timed out after {human(timeout_s)}") from exc
    if proc.returncode == PYTEST_NOTHING_SELECTED:
        return []
    if proc.returncode != PYTEST_OK:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
        raise SelectionDidNotRun(f"pytest exit {proc.returncode}: {tail}")
    ids: list[str] = []
    for line in proc.stdout.splitlines():  # a node id starts at the line's first column; the summary and notes do not
        if "::" in line and not line[:1].isspace() and line.strip() not in ids:
            ids.append(line.strip())
    return ids


def collect_reds(cwd: Path, tests: str, timeout_s: int) -> list[str]:
    """The failed and errored test ids of one pytest run of the selection in cwd, in run order, each once.

    `-o addopts=` and `-p no:cacheprovider` keep the base's own addopts (a -x would stop the list short) and its
    cache out of it. Exit 5 (nothing selected) is []. Any other exit code other than 0 and 1, and the timeout,
    raise SelectionDidNotRun."""
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "reds.txt"
        cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-p", PLUGIN,
               "-q", "--continue-on-collection-errors", *split_selection(tests)]
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=_child_env(report), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout_s,
                                  creationflags=no_window_flags())
        except subprocess.TimeoutExpired as exc:
            raise SelectionDidNotRun(f"timed out after {human(timeout_s)}") from exc
        if proc.returncode == PYTEST_NOTHING_SELECTED:
            return []
        if proc.returncode not in (PYTEST_OK, PYTEST_REDS):
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
            raise SelectionDidNotRun(f"pytest exit {proc.returncode}: {tail}")
        if not report.is_file():
            return []
        ids: list[str] = []
        for line in report.read_text(encoding="utf-8").splitlines():
            if line.strip() and normalise(line) not in ids:
                ids.append(normalise(line))
        return ids


@dataclass
class Seams:
    """The injectable parts of a run (tests only). Each None is the real one, looked up when the run asks for it."""
    load_settings: Callable[[], dict] | None = None
    clock: Callable[[], datetime] | None = None
    collect_ids: Callable[[Path, str, int], list[str]] | None = None
    github_run: Callable | None = None                              # merge_github.run
    pc_reds: Callable[[Path, str, int], list[str]] | None = None    # collect_reds
    pc_only_files: Callable[[Path], list[str]] | None = None        # merge._pc_only_files
    head: Callable[[Path], str] | None = None                       # merge._head


def base_branch(sha: str) -> str:
    """The name one base run's copy takes on GitHub (``merge_github.ref_name`` adds ``merge-suite/``, and ``run`` deletes
    it afterwards as for a merge): unique per run, so parallel runs (one per unit) never overwrite each other's ref."""
    return f"red-on-base-{sha[:8]}-{os.getpid()}"


def base_place(settings: dict | None, root, seams: Seams) -> tuple[str, str, object]:
    """Where the base run goes: ``(where, reason, plan)`` from ``heavy_place.decide("red-on-base")``."""
    at = seams.clock() if seams.clock is not None else None
    place = heavy_place.decide("red-on-base", settings, now=at, root=root)
    return place.where, place.reason, place.plan


def _base_on_github(seams: Seams, plan, cwd: Path, ids: list[str], settings: dict) -> GithubSuiteResult:
    """The base's collected ids on GitHub, the Windows job when a PC-only file is among the ids. An unreadable PC-only
    list is a fallback (the PC runs the selection), never a traceback."""
    try:
        pc_files = (seams.pc_only_files or m._pc_only_files)(cwd)
    except Exception:  # noqa: BLE001 -- the merge reads the same list; unreadable means the PC takes the run
        return GithubSuiteResult(status="fallback", reason="pc-only list unreadable")
    sha = (seams.head or m._head)(cwd)
    with tempfile.TemporaryDirectory() as scratch:
        return (seams.github_run or merge_github.run)(
            plan, cwd=cwd, suite_dir=cwd, sha=sha, branch=base_branch(sha), priority=False,
            settings=settings, alive=lambda: None, output_path=Path(scratch) / "suite.out", tests=len(ids), ids=ids,
            windows=m._windows_for(ids, pc_files))


def _reds(cwd: Path, tests: str, timeout_s: int, seams: Seams) -> list[str]:
    """The tests red on the base: on GitHub when heavy_place says so (an empty id list never goes to GitHub, which would
    run the whole suite), else on the PC. Prints the one place line."""
    settings = heavy_place.read_settings(seams.load_settings)
    where, reason, plan = base_place(settings, cwd, seams)
    if where == "github":
        try:
            ids = [normalise(node) for node in (seams.collect_ids or collect_ids)(cwd, tests, timeout_s)]
        except SelectionDidNotRun:  # a base that fails to import is a red the PC run lists through its plugin
            ids = None
        if ids is None:
            where, reason = "pc", "collect failed"
        elif not ids:
            where, reason = "pc", "no test collected"
        else:
            result = _base_on_github(seams, plan, cwd, ids, settings)
            if not merge_github.goes_to_pc(result):
                print("red-on-base on github")
                return [normalise(node) for node in result.failed_ids]
            where, reason = "pc", merge_github.pc_reason(result)
    print(f"red-on-base on {where} ({reason})")
    return (seams.pc_reds or collect_reds)(cwd, tests, timeout_s)


def red_on_base(cwd: Path, tests: str, ticket: int | None, file: Path, timeout_s: int = DEFAULT_TIMEOUT_S,
                seams: Seams | None = None) -> int:
    """List the reds of the selection in cwd under the ticket; print the result; return the exit code.
    ``seams``: the injectable parts (tests only, #343 u6)."""
    seams = seams or Seams()
    if not cwd.is_dir():
        print(f"red-on-base: refused, no base folder {cwd}")
        return 1
    path = file if file.is_absolute() else cwd / file
    current = path.read_bytes() if path.is_file() else b""
    entries = read_entries(current.decode("utf-8"))
    listed = [target for target, _ in entries]
    listed_ticket = None
    for _, reason in entries:
        found = TICKET_RE.search(reason)
        if found:
            listed_ticket = int(found.group(1))
            break
    if ticket is not None and listed_ticket is not None and ticket != listed_ticket:
        print(f"red-on-base: refused, {path} names #{listed_ticket}, not #{ticket}; nothing written")
        return 2
    try:
        reds = _reds(cwd, tests, timeout_s, seams)
    except SelectionDidNotRun as exc:
        print(f"red-on-base: refused, the selection {tests!r} did not run in {cwd} ({exc}); nothing written")
        return 1
    if not reds:
        print("no red tests on base")
        return 0
    target_ticket = ticket if ticket is not None else listed_ticket
    if target_ticket is None:
        print('open one ticket "Tests red on main before <next>" listing these ids:')
        for nodeid in reds:
            print(f"  {nodeid}")
        return 3
    new = [nodeid for nodeid in reds if nodeid not in listed]
    if new:
        lines = b"".join(
            (format_line(nodeid, f"Tests red on main before #{target_ticket}") + "\n").encode("utf-8")
            for nodeid in new)
        if not current:
            lines = HEADER + lines
        elif not current.endswith(b"\n"):
            lines = b"\n" + lines
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(lines)
    total = len(set(listed) | set(new))
    print(f"ticket: #{target_ticket}, added {len(new)}, total {total}")
    return 0


def add_subparser(sub) -> None:
    parser = sub.add_parser("red-on-base", help="list the tests red on a base as red before a ticket, in "
                                                "tests/red_on_main.txt, so a unit is not blamed for them (#319)")
    parser.add_argument("--cwd", type=Path, required=True, help="the base worktree; the run changes nothing in it")
    parser.add_argument("--tests", required=True, help='the pytest selection, e.g. "tests/test_x.py -k foo"')
    parser.add_argument("--ticket", type=int, default=None,
                        help="the ticket the reds are listed before; the list's own ticket when absent")
    parser.add_argument("--file", type=Path, default=Path(DEFAULT_FILE),
                        help=f"the list file, relative to --cwd (default {DEFAULT_FILE})")
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S,
                        help=f"stop the base run after this many seconds (default {human(DEFAULT_TIMEOUT_S)})")


def run_red_on_base_command(args) -> int:
    return red_on_base(args.cwd, args.tests, args.ticket, args.file, args.timeout_s)
