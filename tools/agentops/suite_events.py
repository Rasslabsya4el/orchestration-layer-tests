"""#900: the readers of the suite's events file (written by ``suite_events_plugin``)."""
from __future__ import annotations

from pathlib import Path

ENV_EVENTS = "SUITE_EVENTS_FILE"
PLUGIN = "tools.agentops.suite_events_plugin"


def events_path(out_path: Path | str) -> Path:
    return Path(str(out_path) + ".events")


def event_files(out_path: Path | str) -> list[Path]:
    """The events files of one suite: one per process that ran tests (``<out>.events.<worker>``)."""
    base = events_path(out_path)
    return sorted(base.parent.glob(base.name + ".*"))


def ended_count(out_path: Path | str | None) -> int | None:
    """#116 A2: the tests ended in one run, the E rows of all its events files (`_rows`); None when it has no events file yet."""
    if not out_path or not event_files(out_path):
        return None
    return sum(1 for row in _rows(out_path) if row[1] == "E")


def clear_events(out_path: Path | str) -> None:
    """Empty the files of an earlier suite run on the same output path (emptied, never removed)."""
    for f in event_files(out_path):
        f.write_text("", encoding="utf-8")


def _rows(out_path: Path | str) -> list[tuple[int, str, str, str]]:
    """Every row of the run's events files as ``(ts, kind, worker, nodeid)``, by time (#900: the one line parser)."""
    rows: list[tuple[int, str, str, str]] = []
    for f in event_files(out_path):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split(" ", 3)
            if len(parts) != 4 or not parts[0].isdigit():
                continue
            rows.append((int(parts[0]), parts[1], parts[2], parts[3]))
    rows.sort(key=lambda r: r[0])  # stable: a worker's own lines keep their order
    return rows


def read_events(out_path: Path | str | None) -> tuple[list[str], list[str]]:
    """``(failed_tests, unfinished)`` node ids: the failed ones in the order they failed, once each; the ones a
    worker started and did not finish (at most one per worker: its last start). ``([], [])`` when no file."""
    if not out_path:
        return [], []
    failed: dict[str, None] = {}
    open_by_worker: dict[str, str] = {}
    for _, kind, worker, nodeid in _rows(out_path):
        if kind == "F":
            failed[nodeid] = None
        elif kind == "S":
            open_by_worker[worker] = nodeid
        elif kind == "E" and open_by_worker.get(worker) == nodeid:
            del open_by_worker[worker]
    return list(failed), list(open_by_worker.values())


def read_reasons(out_path: Path | str | None) -> dict[str, str]:
    """#219: ``{node id: the one-line reason of its first failure}`` from the R rows; ``{}`` when no file or no R row."""
    reasons: dict[str, str] = {}
    if not out_path:
        return reasons
    for _, kind, _worker, rest in _rows(out_path):
        if kind == "R":
            nodeid, _tab, reason = rest.partition("	")
            reasons.setdefault(nodeid, reason)
    return reasons
