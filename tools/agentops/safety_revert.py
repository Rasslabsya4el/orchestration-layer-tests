"""The safety run's red marker, resolved (#195): a landing the old map says the failing test depends on is reverted.

``resolve_safety_red`` is the one entry. The merge line calls it while it holds the merge lock (nothing else lands), in
place of waiting on a red marker for ever. Every outcome is one merge-log line and the marker goes, so the line never
freezes with no one able to clear it; nothing here raises. No suite is run by the revert: the commit it pushes is a
landing, and the next safety run checks it as it checks any landing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import test_select
from .map_run import _ids_text, _log
from .merge import MAIN_PUSH_ENV, _first_line, safety_red_path
from .test_select import Selection
from .util import no_window_flags

REMOTE = "origin"
MAIN = "main"
STATE_FILES = frozenset({"docs/STATE.md"})  # a commit that changes only these is a State commit, never a landing
GIT_TIMEOUT = 120.0  # seconds, per git call; a call over it is the outcome ``not reverted: timeout``


class _Timeout(RuntimeError):
    pass


def _git(cwd, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=no_window_flags(), timeout=GIT_TIMEOUT,
                              env={**os.environ, **env} if env else None)
    except subprocess.TimeoutExpired:
        raise _Timeout("timeout") from None


def _spawn(root, sha: str) -> str:
    """The next safety run, at the revert commit: "" or one line saying why it did not start."""
    from .merge import _spawn_map_run  # lazy: merge imports this module only inside its hold

    return _spawn_map_run(Path(root), sha)


def _read_marker(root) -> dict:
    try:
        data = json.loads(safety_red_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _out(root, *args: str) -> str:
    r = _git(Path(root), *args)
    if r.returncode != 0:
        raise RuntimeError(_first_line(r.stderr) or _first_line(r.stdout) or f"git {args[0]} failed")
    return r.stdout.strip()


def _landings(root, map_commit: str, sha: str, reverts: frozenset[str] | set[str] = frozenset()
              ) -> list[tuple[str, str, bool]]:
    """``(landing, its first parent, is a merge)`` of the first-parent line ``map_commit..sha``, newest first. Not a
    landing: a State commit (its own diff touches only ``STATE_FILES``: no code a test could depend on) and a revert
    commit in ``reverts`` (it takes code out; reverting it would bring the reverted landing back)."""
    out: list[tuple[str, str, bool]] = []
    for line in _out(root, "rev-list", "--first-parent", "--parents", f"{map_commit}..{sha}").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in reverts:  # a root commit has no base to diff against
            continue
        if not _is_state_commit(root, parts[1], parts[0]):
            out.append((parts[0], parts[1], len(parts) >= 3))
    return out


def _is_state_commit(root, parent: str, commit: str) -> bool:
    files = _own_files(root, parent, commit)
    return bool(files) and files <= STATE_FILES


_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")


def _revert_history(root, since: str) -> tuple[set[str], set[str]]:
    """``(reverted, reverts)`` over the first-parent commits after ``since`` (up to HEAD), read from the
    ``This reverts commit <sha>`` line ``git revert`` writes, oldest first. ``reverted``: the full shas main has
    reverted and not reverted again (a revert that was itself reverted puts its landing back). ``reverts``: the revert
    commits that took such a landing out (the re-landing revert of a revert is a landing, so it is not among them)."""
    log = _out(root, "log", "--first-parent", "--reverse", "--format=%H%x1f%B%x1e", f"{since}..HEAD")
    reverted: set[str] = set()
    reverts_of: dict[str, list[str]] = {}  # a revert commit -> the shas it reverted
    for record in log.split("\x1e"):
        commit, _, body = record.strip().partition("\x1f")
        if not commit:
            continue
        for reverted_sha in _REVERTS.findall(body):
            if reverted_sha in reverts_of:  # a revert of a revert: what it reverted is back on main
                reverted.difference_update(reverts_of[reverted_sha])
            else:
                reverted.add(reverted_sha)
                reverts_of.setdefault(commit, []).append(reverted_sha)
    return reverted, set(reverts_of)


def already_reverted(root, sha: str) -> bool:
    """Main (HEAD of ``root``) already carries a revert of the landing ``sha``. Raises ``RuntimeError`` (the git
    reason) when the history cannot be read: the caller logs it as ``not reverted: <reason>``."""
    full = _out(root, "rev-parse", "--verify", f"{sha}^{{commit}}")
    return full in _revert_history(root, full)[0]


def _own_files(root, parent: str, landing: str) -> set[str]:
    """The files the landing's own diff changed (against its first parent), a rename as both paths."""
    return set(_out(root, "diff", "--name-only", "--no-renames", parent, landing).splitlines())


def _is_guilty(root, selection: Selection, parent: str, landing: str, ids: list[str]) -> bool:
    """A failing id is among the selection's mapped ids, or its file is among the files the landing itself changed.
    A file in the selection only because the selection pads it (a whole file younger than the map, the no-window
    guard) does not count."""
    if set(ids) & set(selection.ids):
        return True
    return bool({i.split("::", 1)[0] for i in ids} & _own_files(root, parent, landing))


def _pick_guilty(root, mdir, landings, ids: list[str]) -> tuple[str, bool] | None:
    """The newest guilty landing; failing that, the newest whose selection was the full suite (ambiguous: it covers
    everything). ``(landing, is a merge)`` or None."""
    ambiguous: tuple[str, bool] | None = None
    for landing, parent, is_merge in landings:
        selection = test_select.select_for_diff(root, parent, landing, mdir)
        if selection.full:
            ambiguous = ambiguous or (landing, is_merge)
        elif _is_guilty(root, selection, parent, landing, ids):
            return landing, is_merge
    return ambiguous


def _best_effort(root, *args: str) -> None:
    try:
        _git(Path(root), *args)
    except _Timeout:
        pass


def _revert_and_push(root, landing: str, is_merge: bool) -> str:
    """The revert commit pushed to origin/main (never forced); raises with the reason, the checkout as it was."""
    before = _out(root, "rev-parse", "HEAD")
    try:
        reverted = _git(Path(root), "revert", "--no-edit", *(["-m", "1"] if is_merge else []), landing)
    except _Timeout:
        _best_effort(root, "revert", "--abort")
        raise
    if reverted.returncode != 0:
        _git(Path(root), "revert", "--abort")
        raise RuntimeError(f"revert conflict: {_first_line(reverted.stderr) or _first_line(reverted.stdout)}")
    try:
        pushed = _git(Path(root), "push", REMOTE, MAIN, env=MAIN_PUSH_ENV)
    except _Timeout:
        _best_effort(root, "reset", "--keep", before)
        raise
    if pushed.returncode != 0:
        _git(Path(root), "reset", "--keep", before)  # the revert was never published: take it back
        raise RuntimeError(f"push refused: {_first_line(pushed.stderr)}")
    return _out(root, "rev-parse", "HEAD")


def _resolve(root, mdir, marker: dict) -> str:
    """The tail of the log line: ``reverted <sha12>``; raises with the reason when nothing was reverted."""
    sha, map_commit, ids = marker.get("sha"), marker.get("map_commit"), marker.get("ids") or []
    if not ids:
        raise RuntimeError("no failing id")
    if not sha or not map_commit:
        raise RuntimeError("the marker names no commit range")
    if already_reverted(root, sha):  # the cause left main with that revert: another landing is not to blame for it
        return f"already reverted: {sha[:12]}"
    if _git(Path(root), "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise RuntimeError("the main checkout has uncommitted changes")
    if _out(root, "symbolic-ref", "--short", "HEAD") != MAIN:
        raise RuntimeError(f"the main checkout is not on {MAIN}")
    _git(Path(root), "fetch", REMOTE, MAIN)  # a failed fetch leaves the compare to the ref as it stands
    if _out(root, "rev-parse", "HEAD") != _out(root, "rev-parse", f"{REMOTE}/{MAIN}"):
        raise RuntimeError(f"local {MAIN} is not at {REMOTE}/{MAIN}")
    reverted, reverts = _revert_history(root, map_commit)
    guilty = _pick_guilty(root, mdir, _landings(root, map_commit, sha, reverts), ids)
    if guilty is None:
        raise RuntimeError("no landing in the range selects the failing tests")
    landing, is_merge = guilty
    if landing in reverted:  # the landing the old map blames is already out of main
        return f"already reverted: {landing[:12]}"
    new_head = _revert_and_push(root, landing, is_merge)
    problem = _spawn(root, new_head)
    return f"reverted {landing[:12]}" + (f" (next safety run not started: {problem})" if problem else "")


def resolve_safety_red(root, mdir, log_path: Path) -> str:
    """Read the marker, revert the newest landing it names (or log why none), remove the marker, log one line, and
    return it. The caller holds the merge lock. Never raises."""
    marker = _read_marker(root)
    try:
        outcome = _resolve(root, mdir, marker)
    except _Timeout:
        outcome = "not reverted: timeout"
    except Exception as exc:  # noqa: BLE001 -- every outcome is one line, and the marker goes
        outcome = f"not reverted: {_first_line(str(exc)) or type(exc).__name__}"
    line = f"safety run: red {_ids_text(marker.get('ids') or [])} | {outcome}"
    try:
        safety_red_path(root).unlink(missing_ok=True)
        _log(Path(log_path), line)
    except OSError:
        pass
    return line
