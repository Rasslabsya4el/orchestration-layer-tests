"""#232 unit 4b (D119): the free-minutes counter of the private repo that runs on its OWN GitHub runners.

`<machine_dir()>/github_runs.jsonl` is the append-only log of GitHub suite runs, one JSON line each: time, repo (the
repo's folder name), slug, month ("YYYY-MM", UTC), minutes (int), machines, outcome, run_id, own_runner; extra keys
are kept. Only a line with ``own_runner`` true counts: a run dispatched on the repo's own slug. A line without the key
is a free public test copy's run and never counts here. Lines that do not parse, or that are not an object with an
int `minutes`, are skipped. `record_run` appends under `util.file_lock`, so two processes never interleave a line.

`over_budget` answers for one repo: the month's used minutes plus the median of its last merges would pass the
free allowance (`github_free_minutes`, 2,000 a month). The month rolls over on the 1st, UTC. A naive ``now`` is taken
as UTC; an aware one is converted to UTC.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import core_settings
from .machine_mode import machine_dir
from .util import append_jsonl, file_lock

RUNS_LOG = "github_runs.jsonl"


def _log(path: Path | None) -> Path:
    return Path(path) if path is not None else machine_dir() / RUNS_LOG


def _records(path: Path) -> list[dict]:
    """Every readable own-runner line of the log that carries an int `minutes`, in file order."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    kept = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and type(record.get("minutes")) is int and record.get("own_runner") is True:
            kept.append(record)
    return kept


def record_run(record: dict) -> None:
    """Append one run record as one JSON line to the machine's `github_runs.jsonl`, under the file's lock."""
    log = _log(None)
    with file_lock(log):
        append_jsonl(log, record)


def minutes_used(repo: str, month: str, path: Path | None = None) -> int:
    """The minutes the repo's own-runner runs cost in ``month`` ("YYYY-MM")."""
    return sum(r["minutes"] for r in _records(_log(path)) if r.get("repo") == repo and r.get("month") == month)


def median_merge_minutes(repo: str, last: int = 10, path: Path | None = None) -> int:
    """The median minutes of the repo's last ``last`` own-runner runs that cost minutes (> 0), rounded up to a whole
    minute; 0 when it has none. A refused or failed send (0 minutes) is not a merge and does not count."""
    mine = [r["minutes"] for r in _records(_log(path)) if r.get("repo") == repo and r["minutes"] > 0]
    tail = sorted(mine[-last:]) if last > 0 else []
    if not tail:
        return 0
    mid = len(tail) // 2
    if len(tail) % 2:
        return tail[mid]
    return math.ceil((tail[mid - 1] + tail[mid]) / 2)


def _first_of_next_month(now: datetime) -> str:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"{year:04d}-{month:02d}-01"


def over_budget(
    repo: str,
    now: datetime | None = None,
    limit: int | None = None,
    path: Path | None = None,
) -> tuple[bool, str]:
    """``(True, reason)`` when used + the median merge would pass ``limit`` (default: `github_free_minutes`) this
    month; ``(False, "")`` otherwise. ``now`` is a UTC datetime (default: the current time); a naive ``now`` is
    taken as UTC, an aware one is converted to UTC."""
    if now is None:
        current = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=timezone.utc)
    else:
        current = now.astimezone(timezone.utc)
    if limit is None:
        limit = core_settings.load()["github_free_minutes"]
    month = f"{current.year:04d}-{current.month:02d}"
    used = minutes_used(repo, month, path)
    median = median_merge_minutes(repo, path=path)
    if used + median > limit:
        return True, f"{used} used + median {median} > {limit} until {_first_of_next_month(current)}"
    return False, ""
