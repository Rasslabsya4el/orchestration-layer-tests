"""``python -m tools.agentops prequeue --cwd <issue worktree>``: the tests the merge would select for an issue
branch, run on that branch merged with origin/main, before ORCH queues the merge (#124 unit 1).

The merge's own selection (``merge._plan_selection``) and the line check's preparation (``LineCheck._prepare_into``,
its merge commit as a git object, its suite worktree) are reused. Nothing is pushed; the worktree's HEAD and status
are never touched (the merge commit is written as an object only; the suite runs in the suite worktree).
Failed ids are re-run alone as the merge does (``merge._recheck_failed``).

Exit 0: green, or no test the change can affect, or a full suite the merge log says is too long to run here
(``prequeue: skipped, full suite median <N>``). Exit 1: red, or not mergeable: the branch must not be queued.
Line 1 of the output is the selection's line 1 (``selected N of M (map <sha8>)`` or ``full suite: <reason>``).

The suite worktree is the line check's standing name (``check``): ``--cwd`` is an issue worktree, never the main
checkout, whose ``check`` worktree the line check uses.
"""
from __future__ import annotations

import statistics
from collections.abc import Iterable
from pathlib import Path

from . import merge as m
from .line_check import LineCheck, _Prepared

FULL_SUITE_MAX_MEDIAN_SECONDS = 10 * 60   # a full suite whose median is longer is not run here (#124 unit 1)
MEDIAN_OVER_FULL_RUNS = 20                # the last N full runs in merge.log
MERGE_LOG_TAIL_BYTES = 1024 * 1024        # the end of merge.log is read, never the whole file


# -- the median and the threshold (pure) ---------------------------------------------------------

def _duration_seconds(line: str) -> float | None:
    """The ``| suite`` field of one merge-log line that is a full run's (``| 0 failed`` right after it), in
    seconds; None for any other line: a red batch line (``| N failed``), a selected run, no suite field."""
    found = m._LOG_FULL_RE.search(line)
    if found is None:
        return None
    if found.group(1) is not None:
        return int(found.group(1)) * 3600 + int(found.group(2)) * 60
    return float(found.group(3)) * (60 if found.group(4) == "min" else 1)


def full_suite_median_seconds(lines: Iterable[str]) -> float | None:
    """Median seconds of the last ``MEDIAN_OVER_FULL_RUNS`` full suite runs among merge-log ``lines``.
    A full run is a green-suite line (``_LOG_FULL_RE``) without a selection (``| selected``) whose suite
    ran (a positive time); None when there is none."""
    runs: list[float] = []
    for line in lines:
        if " | selected " in line:
            continue
        seconds = _duration_seconds(line)
        if seconds is not None and seconds > 0:
            runs.append(seconds)
    last = runs[-MEDIAN_OVER_FULL_RUNS:]
    return float(statistics.median(last)) if last else None


def full_suite_runs(median_seconds: float | None) -> bool:
    """True when the full suite runs here: a median at most ``FULL_SUITE_MAX_MEDIAN_SECONDS``, or no
    median at all (no full run in the log to say it is long, so the gate is not closed on no evidence)."""
    return median_seconds is None or median_seconds <= FULL_SUITE_MAX_MEDIAN_SECONDS


def _merge_log_lines(log_path: Path) -> list[str]:
    """The lines at the end of ``log_path`` (``MERGE_LOG_TAIL_BYTES`` of them); [] when unreadable."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - MERGE_LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[1:] if size > MERGE_LOG_TAIL_BYTES else lines  # a cut-off first line is not a line


# -- the run ----------------------------------------------------------------------------------------

def _line_one(prep: _Prepared) -> str:
    """The selection's line 1, as the merge writes it. With ``merge_select_tests`` off the merge runs the full
    suite and writes no selection line; prequeue then names that as the full suite."""
    if prep.green_now:  # the empty selection wrote its line into the output file
        return prep.output_path.read_text(encoding="utf-8").splitlines()[0]
    first = prep.head_line.splitlines()[0] if prep.head_line else ""
    if first.startswith(("selected ", "full suite: ")):
        return first
    return "full suite: selection off (merge_select_tests)"


def run_prequeue(cwd, *, suite_cmd: str, main: str = "origin/main", remote: str = m.DEFAULT_REMOTE,
                 log_path: Path | None = None, output_dir: Path | None = None, suite_shared=None) -> int:
    """Run the merge's selection for the branch at ``cwd`` (HEAD) merged with ``main``; print line 1 and the
    result; return 0 (may be queued) or 1 (must not be queued). ``log_path``: the merge log (default: the
    installation's); ``output_dir``: the suite output folder (default: the merge's); ``suite_shared``: the
    directories linked into the suite worktree (default: the merge's)."""
    cwd = Path(cwd).resolve()  # absolute before any git call: git resolves a relative path against each call's cwd (#134)
    fetch = m._git(cwd, "fetch", remote)
    if fetch.returncode != 0:
        print(f"prequeue: refused, fetch of {remote} failed: {m._first_line(fetch.stderr)}")
        return 1
    target = m._head(cwd)
    if not target:
        print("prequeue: refused, the worktree names no commit")
        return 1
    branch = m._git(cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    check = LineCheck(cwd, lock_path=cwd / "prequeue.lock", ticket="prequeue", branch=branch, target=target,
                      suite_cmd=suite_cmd, suite_env={}, suite_shared=suite_shared, remote=remote, main=main,
                      output_dir=output_dir)
    try:
        prep = _Prepared()
        try:
            check._prepare_into(prep)
        except Exception as exc:  # noqa: BLE001 -- a refusal is one line, never a traceback
            print(f"prequeue: refused, preparation failed: {type(exc).__name__}: {m._first_line(str(exc))}")
            return 1
        if prep.skip_reason:
            print(f"prequeue: refused, {prep.skip_reason}")
            return 1
        line_one = _line_one(prep)
        print(line_one)
        if prep.green_now:
            print("prequeue: green, no test the change can affect")
            return 0
        if line_one.startswith("full suite:"):
            median = full_suite_median_seconds(_merge_log_lines(log_path or m._merge_log_path()))
            if not full_suite_runs(median):
                print(f"prequeue: skipped, full suite median {median / 60:.0f} min")
                return 0
            if median is None:
                print("prequeue: no full run in the merge log to judge by; the full suite runs")
        # routed_ok=False: the run stays on this PC (a remote suite host changes its time, not its result)
        run = m._run_merge_suite(prep.cmd, suite_cmd, 0, prep.head_line, prep.check_dir, prep.env, prep.output_path,
                                 routed_ok=False, root=cwd)
        if run.stuck:
            print(f"prequeue: red, the run went silent and was ended: {run.stuck}")
            return 1
        if run.ok:
            print("prequeue: green")
            return 0
        red_ids = m._failed_ids(run.output)
        if not red_ids:
            print("prequeue: red, the output names no failing test id")
            return 1
        try:
            still, _output = m._recheck_failed(suite_cmd, red_ids, prep.check_dir, prep.env, prep.output_path)
        except m.RecheckInconclusive:
            print("prequeue: red, the re-check could not tell which tests fail")
            return 1
        if not still:
            print(f"prequeue: green after re-check of {len(red_ids)}: {', '.join(red_ids)}")
            return 0
        print("prequeue: red, failing tests:")
        for test_id in still:
            print(f"  FAILED {test_id}")
        return 1
    finally:
        check.release_berth()  # #224: the berth a full-suite check took while preparing, whichever way this ends


# -- the CLI ----------------------------------------------------------------------------------------

def add_subparser(sub) -> None:
    pq = sub.add_parser("prequeue", help="run the merge's own tests on an issue branch merged with origin/main "
                                         "before it is queued (#124)")
    pq.add_argument("--cwd", type=Path, required=True, help="the issue worktree")


def run_prequeue_command(args) -> int:
    try:
        suite_cmd = m.resolve_suite_cmd(None)
    except m.MergeError as exc:
        print(f"prequeue: refused, {exc}")
        return 1
    return run_prequeue(args.cwd, suite_cmd=suite_cmd)
