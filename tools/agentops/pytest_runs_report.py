"""The `pytest-runs` report (#235): the agents' pytest calls of the last N days, per repo.

Reads `pytest_runs.jsonl` in `machine_dir()`, one JSON object per run: `start` and `end` ("YYYY-MM-DD HH:MM:SS",
naive local time), `repo`, `selected`, plus fields the report does not read. A run counts when its `start` falls in
the window. A line that does not parse as a run, or ends before it starts, is skipped and counted, never fatal.

Per repo: runs and runs per day, median and p90 (nearest rank) of the run's minutes, the share of runs above 500
selected tests, and the total minutes of those runs. Durations follow core principle 7: seconds under 90 s, minutes
up to 90 min, hours and minutes past that.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from tools.agentops.machine_mode import machine_dir

LOG_NAME = "pytest_runs.jsonl"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
BIG_SUITE = 500
PLAIN_SECONDS = 90
HOUR_MINUTE_SECONDS = 90 * 60


class Run(NamedTuple):
    start: datetime
    seconds: float
    repo: str
    selected: int


def log_path() -> Path:
    return machine_dir() / LOG_NAME


def parse_line(raw: bytes) -> Run | None:
    """One log line as a run, or None when it is not a readable run."""
    try:
        row = json.loads(raw.decode("utf-8"))
        if not isinstance(row, dict):
            return None
        start = datetime.strptime(row["start"], TIME_FORMAT)
        end = datetime.strptime(row["end"], TIME_FORMAT)
        repo = row["repo"]
        selected = row["selected"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(repo, str) or not repo:
        return None
    if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None
    return Run(start, seconds, repo, selected)


def read_runs(path: Path) -> tuple[list[Run], int]:
    """The readable runs of ``path`` and the count of lines skipped; blank lines are not counted."""
    runs: list[Run] = []
    skipped = 0
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            run = parse_line(raw.rstrip(b"\r\n"))
            if run is None:
                skipped += 1
            else:
                runs.append(run)
    return runs, skipped


def nearest_rank(sorted_values: list[float], percent: int) -> float:
    rank = math.ceil(percent * len(sorted_values) / 100)
    return sorted_values[rank - 1]


def human(seconds: float) -> str:
    """Principle 7: seconds under 90 s, minutes up to 90 min, then hours and minutes."""
    if seconds < PLAIN_SECONDS:
        return f"{seconds:.0f} s"
    if seconds < HOUR_MINUTE_SECONDS:
        return f"{seconds / 60:.1f} min"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes} min"


def repo_line(repo: str, runs: list[Run], days: int) -> str:
    seconds = sorted(run.seconds for run in runs)
    big = [run for run in runs if run.selected > BIG_SUITE]
    share = 100 * len(big) / len(runs)
    big_total = sum(run.seconds for run in big)
    return (f"{repo}: {len(runs)} run{"" if len(runs) == 1 else "s"}, {len(runs) / days:.1f} per day | "
            f"median {human(statistics.median(seconds))}, p90 {human(nearest_rank(seconds, 90))} | "
            f"over {BIG_SUITE} tests: {share:.1f}% of runs, {human(big_total)} total")


def run_pytest_runs(args: argparse.Namespace, *, now: datetime | None = None) -> int:
    """Print the report for ``args`` (``--days``, ``--log``); returns the exit code.
    ``now`` is for tests: the clock the window ends at (default: the local time)."""
    if args.days < 1:
        print("error: --days must be at least 1")
        return 2
    moment = now if now is not None else datetime.now()
    path = args.log if args.log is not None else log_path()
    runs: list[Run] = []
    skipped = 0
    if path.is_file():
        try:
            runs, skipped = read_runs(path)
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}")
            return 2
    cutoff = moment - timedelta(days=args.days)
    window = [run for run in runs if cutoff <= run.start <= moment]

    lines: list[str] = []
    if not window:
        lines.append("no runs logged")
    else:
        lines.append(f"pytest calls, last {args.days} days")
        by_repo: dict[str, list[Run]] = defaultdict(list)
        for run in window:
            by_repo[run.repo].append(run)
        for repo in sorted(by_repo):
            lines.append(repo_line(repo, by_repo[repo], args.days))
    if skipped:
        lines.append(f"skipped {skipped} unreadable lines")
    print("\n".join(lines))
    return 0


def add_subparser(sub) -> argparse.ArgumentParser:
    """Register the ``pytest-runs`` command on the CLI's subparsers."""
    parser = sub.add_parser("pytest-runs", help="the agents' pytest calls per repo: per day, median and p90 minutes, "
                                                "share above 500 tests (#235)")
    parser.add_argument("--days", type=int, default=7, help="the last N days, by each run's start (default 7)")
    parser.add_argument("--log", type=Path, default=None,
                        help="the log file (default: pytest_runs.jsonl in the machine dir)")
    return parser
