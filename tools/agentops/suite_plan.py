"""#758: the share of a suite's work done, from its plan file (the readers).

The plan file `<suite out path>.plan.json` = `{"seconds": [...]}` (one number per test, in the order
they are handed out) is written by `suite_plan_plugin`. A reader with no plan counts as before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PROGRESS_RE = re.compile(r"^([.sFEx]+)\s+\[\s*(\d+)%\]\s*$", re.MULTILINE)


_POLL_RE = re.compile(r"(?:restart poll|suite slow):[^\n]*\n")  # the watchers' lines (#900, #63)


def strip_poll(text: str) -> str:
    """#900: the restart watcher writes its ``restart poll:`` line (and #63 the stall watch its ``suite slow:`` line) into the out in the middle of a progress line
    (``....F...restart poll: ...`` then ``.... [ 36%]``); a reader drops the poll text so the line is whole again
    and its F and dots are counted (a poll line inside a progress line hid one F: 9 failed of 10)."""
    return _POLL_RE.sub("", text)


def plan_path(out_path: Path | str) -> Path:
    return Path(str(out_path) + ".plan.json")


def load_plan(out_path: Path | str | None) -> list[float] | None:
    """The plan's seconds, or None when absent, unreadable, empty or summing to 0."""
    if not out_path:
        return None
    try:
        data = json.loads(plan_path(out_path).read_text(encoding="utf-8"))
        seconds = [float(x) for x in data["seconds"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return seconds if seconds and sum(seconds) > 0 else None


_TAIL_RE = re.compile(r"^([.sFEx]+)(?:\s+\[[^\]]*)?$")


def partial_tail(text: str) -> str:
    """#829: the progress characters of the last, not yet newline-terminated line (a line is
    ~72 tests; at one worker it takes minutes to complete). "" when the text ends in a newline,
    the tail is no progress line, or it is a complete one (the line regex counts that)."""
    tail = strip_poll(text).rsplit(chr(10), 1)[-1]
    if _PROGRESS_RE.match(tail):
        return ""
    m = _TAIL_RE.match(tail)
    return m.group(1) if m else ""


def finished_count(text: str) -> int:
    """Count of progress characters `[.sFEx]` in the output's progress lines, the partial last line too."""
    text = strip_poll(text)
    return sum(len(m.group(1)) for m in _PROGRESS_RE.finditer(text)) + len(partial_tail(text))


def work_share(plan: list[float], n_done: int) -> float:
    return sum(plan[:max(0, n_done)]) / sum(plan)


def rest_seconds(elapsed: float, share: float) -> float | None:
    if share <= 0:
        return None
    return elapsed * (1 - share) / share


_SUMMARY_RE = re.compile(r"^(?=.*\b\d+ (?:passed|failed)\b)(?=.*\bin \d+(?:\.\d+)?s\b).*$", re.MULTILINE)


def summary_printed(text: str) -> bool:
    """pytest's final summary line (``47 passed in 14.71s``, ``=== 1 failed, 46 passed in 14.7s ===``) is in the
    output: the run has finished its tests, whatever its process still does before it exits."""
    return _SUMMARY_RE.search(strip_poll(text)) is not None


def last_percent(text: str) -> int | None:
    """#892: the last ``[ NN%]`` marker of a progress line, None before the first."""
    found = _PROGRESS_RE.findall(strip_poll(text))
    return int(found[-1][1]) if found else None
