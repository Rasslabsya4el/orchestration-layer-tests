"""Issue 1115 (unit 2): a home merge, after its push, runs `core update` once in each target of the
home's `targets` key (folder + repo), and returns one line per target for the merge result record.

    run_core_targets(targets, home_root=<home checkout>, gh_runner=None) -> ["<repo>: taken <sha8>", ...]

Each line is one of `taken <sha8>` (the target now holds the home's head; `taken <sha8> (by a concurrent update)`
when another update of the target held its lock first and brought it there, #76), `refused: ORCH live` (its
ORCH is live), `refused: merge running` (its merge lock stayed taken for `CORE_UPDATE_WAIT_S`, #49: 0 files
written), or
`failed: <reason>` (the reason is appended to the target's `artifacts/runs/dispatch.out` once per cause, #42:
a repeat of the file's last reason for this repo adds no line). Nothing
here raises: a target's failure never changes the home's merge rc. `gh_runner` is the same injection
`core_update` takes (tests pass a fake; None = the real `gh`).

A refused push (`refused: ORCH live`, `refused: merge running`) also leaves a pending mark in the target
(`core_pending`, #89: the home head sha and the time), and the target's dispatcher takes the core at its first tick
without an ORCH. A mark that cannot be written, or a home head that cannot be read, changes nothing: the line is the
same. `taken` and a plain `failed` leave no mark; a `failed` on the GitHub rate limit (`CoreUpdateRateLimited`, #148) leaves
one carrying `until`, the limit's reset time, before which no retry runs.

Stdlib only, like every module the core ships with."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import core_pending
from .core_update import (CORE_UPDATE_WAIT_S, CoreUpdateBusy, CoreUpdateRateLimited, blocking_orch_live,
                          run_core_update)
from .util import no_window_flags, now

DISPATCH_OUT = Path("artifacts") / "runs" / "dispatch.out"


def run_core_targets(targets: list, *, home_root: Path, gh_runner=None) -> list[str]:
    return [_one_target(entry, home_root, gh_runner) for entry in targets]


def _one_target(entry, home_root: Path, gh_runner) -> str:
    repo = str(entry.get("repo") or "?") if isinstance(entry, dict) else "?"
    folder = _folder_of(entry, home_root)
    if folder is None:
        return f"{repo}: failed: target entry has no folder"
    waiting = core_pending.read_mark(folder)
    if core_pending.mark_waits(waiting, time.time()):  # #148: no attempt before the rate limit resets
        stamp = time.strftime("%H:%M:%S", time.localtime(float(waiting["until"])))
        return f"{repo}: failed: waits for the GitHub rate limit until {stamp}"
    try:
        if blocking_orch_live(folder):  # #188 rule 2: a finishing session does not block the push
            _mark_pending(folder, home_root)
            return f"{repo}: refused: ORCH live"
        result = run_core_update(folder, gh_runner=gh_runner, lock_wait_s=CORE_UPDATE_WAIT_S,
                                 home_count=lambda sha: _home_count(home_root, sha))
        if result.home_head is None:
            raise RuntimeError("the target's update_rule is home: nothing was taken")
        by_other = " (by a concurrent update)" if result.waited else ""
        return f"{repo}: taken {result.home_head[:8]}{by_other}"
    except CoreUpdateBusy:
        _mark_pending(folder, home_root)
        return f"{repo}: refused: merge running"
    except Exception as exc:  # noqa: BLE001 -- a target's failure never fails the home's merge
        if isinstance(exc, CoreUpdateRateLimited):
            _mark_pending(folder, home_root, until=exc.until)  # #148: no retry before the limit resets
        reason = _bounded(_one_line(str(exc)) or type(exc).__name__)
        _log_failure(folder, repo, reason)
        return f"{repo}: failed: {reason}"


def _mark_pending(folder: Path, home_root: Path, until: float | None = None) -> None:
    """#89: the refused target gets the pending mark with the home head. Nothing here raises, and a failure leaves
    no mark while the caller's line stays as it is."""
    try:
        head = _home_head(home_root)
        if head is not None:
            core_pending.write_mark(folder, head, until=until)
    except Exception:  # noqa: BLE001 -- a mark never changes the target's line (#89)
        return


def _home_head(home_root: Path) -> str | None:
    """`git -C <home_root> rev-parse HEAD`; None when the read fails."""
    try:
        proc = subprocess.run(["git", "-C", str(home_root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=no_window_flags(), timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _home_count(home_root: Path, sha: str) -> int | None:
    """#157: `git -C <home_root> rev-list --count <sha>`, the commits up to the taken home commit, counted at the home
    checkout so the update makes no extra GitHub call; None when the read fails."""
    try:
        proc = subprocess.run(["git", "-C", str(home_root), "rev-list", "--count", sha], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", creationflags=no_window_flags(),
                              timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    return int(text) if proc.returncode == 0 and text.isdigit() else None


def _folder_of(entry, home_root: Path) -> Path | None:
    if not isinstance(entry, dict) or not entry.get("folder"):
        return None
    folder = Path(str(entry["folder"]))
    return folder if folder.is_absolute() else Path(home_root) / folder


def _one_line(text: str) -> str:
    """The whole text on one line (#53): its non-empty lines joined, never cut at a newline."""
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


REASON_CHARS = 400
ELLIPSIS = "..."
FULL_OUTPUT_MARK = " (full output: "
#: the `(full output: <path>)` end is kept whole only while it takes at most this many characters
FULL_OUTPUT_END_CHARS = 360


def _bounded(text: str) -> str:
    """At most `REASON_CHARS` characters (#53), and the end is what is kept. A text longer than that keeps its
    `(full output: <path>)` end whole and cuts from its left, the wrapper first, then the head of the exception
    line, so the line survives whole whenever it fits with the end. A text whose end is longer than
    `FULL_OUTPUT_END_CHARS`, or that has no end, keeps only its last `REASON_CHARS - 3` characters behind `...`."""
    if len(text) <= REASON_CHARS:
        return text
    at = text.rfind(FULL_OUTPUT_MARK)
    if at >= 0 and len(text) - at <= FULL_OUTPUT_END_CHARS:
        end = text[at:]
        room = REASON_CHARS - len(end) - len(ELLIPSIS)
        return ELLIPSIS + text[at - room:at] + end
    return ELLIPSIS + text[-(REASON_CHARS - len(ELLIPSIS)):]


def _log_failure(folder: Path, repo: str, reason: str) -> None:
    """One line in the target's own dispatch.out per cause (#42): no line when the file's last failure line for this
    repo already gives the same reason. A folder that is not there has no log to write."""
    if not folder.is_dir():
        return
    path = folder / DISPATCH_OUT
    if _last_failure_reason(path, repo) == reason:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{now()} | core update (home merge, {repo}) failed: {reason}\n")
    except OSError:
        return


TAIL_BYTES = 64 * 1024


def _last_failure_reason(path: Path, repo: str) -> str | None:
    """The reason of the last `core update (home merge, <repo>) failed:` line in the file's tail; None if there is none."""
    mark = f"core update (home merge, {repo}) failed: "
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if mark in line:
            return line.split(mark, 1)[1].strip()
    return None
