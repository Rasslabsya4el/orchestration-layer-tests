"""Issue 89 (unit 1): the pending mark a refused home push leaves in a target.

    write_mark(folder, sha, *, clock=None, until=None) -> Path  # artifacts/runs/core_update_pending.json: {"sha", "at"[, "until"]}
    mark_waits(mark, now) -> bool                      # #148: the mark's `until` (rate-limit reset, epoch s) is still ahead
    read_mark(folder) -> dict | None                   # None when absent, unreadable or malformed
    clear_mark(folder) -> None                         # no error when absent; also clears the hold file
    read_hold(folder) -> dict | None                   # #117: artifacts/runs/core_hold.json, None when absent/unreadable
    write_hold(folder, hold) -> None                   # atomic; the dispatcher's hold of further ORCH starts
    clear_hold(folder) -> None

`at` is the local time to the second, ISO (`2026-10-16T21:30:05`); `clock` is a callable returning a datetime
(None = the local now). Stdlib only, like every module the core ships with."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

MARK_REL = Path("artifacts") / "runs" / "core_update_pending.json"
#: #148: the longest a mark may wait for the rate limit: 70 minutes
MAX_WAIT_S = 70 * 60.0
HOLD_REL = Path("artifacts") / "runs" / "core_hold.json"


def write_mark(folder: Path, sha: str, *, clock=None, until: float | None = None) -> Path:
    path = Path(folder) / MARK_REL
    at = (clock or datetime.now)().isoformat(timespec="seconds")
    body: dict = {"sha": sha, "at": at}
    now = time.time()
    if until is None:
        standing = read_mark(folder)  # #148: a refused/busy mark does not erase a standing mark's wait
        if mark_waits(standing, now):
            until = standing["until"]
    if until is not None:
        # #148: epoch seconds, machine file; the GitHub rate limit's reset, cut to MAX_WAIT_S ahead so a bad value
        # cannot stall updates for long
        body["until"] = min(float(until), now + MAX_WAIT_S)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def mark_waits(mark: dict | None, now: float) -> bool:
    """#148: True iff the mark carries a numeric `until` (epoch seconds) later than `now`: no update attempt and no
    ORCH-start hold before then. A mark without `until` (refused, busy) never waits."""
    until = mark.get("until") if isinstance(mark, dict) else None
    return isinstance(until, (int, float)) and not isinstance(until, bool) and until > now


def read_mark(folder: Path) -> dict | None:
    try:
        data = json.loads((Path(folder) / MARK_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_mark(folder: Path) -> None:
    (Path(folder) / MARK_REL).unlink(missing_ok=True)
    clear_hold(folder)  # #117: the hold lives only while its mark stands


def read_hold(folder: Path) -> dict | None:
    try:
        data = json.loads((Path(folder) / HOLD_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_hold(folder: Path, hold: dict) -> None:
    path = Path(folder) / HOLD_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(hold, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def clear_hold(folder: Path) -> None:
    (Path(folder) / HOLD_REL).unlink(missing_ok=True)
