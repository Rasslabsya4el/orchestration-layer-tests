"""The one standing suite worktree of the merge command (#301): the suite runs on the clean tree of the
merge commit, so uncommitted files of the main checkout never change its result. Nothing here deletes a
file, a directory, a junction or a worktree (docs/agents/machine.md: deleting through a junction wipes
the real directory); an existing worktree is only moved to the commit with checkout + reset --hard."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .util import no_window_flags
from .worktree import SHARED, check_junction_target, git

SUITE_NAME = "merge-suite"
#: the full run's standing folder name; map_run imports it from here, the worktree gc reads it here.
FULL_WORKTREE = "full-run"


class SuiteWorktreeError(RuntimeError):
    pass


def _run(*args: str, cwd: Path) -> str:
    res = git(*args, cwd=cwd)
    if res.returncode != 0:
        raise SuiteWorktreeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def _registered(root: Path, path: Path) -> bool:
    want = os.path.normcase(os.path.realpath(path))
    for line in _run("worktree", "list", "--porcelain", cwd=root).splitlines():
        if line.startswith("worktree ") and os.path.normcase(os.path.realpath(line[9:])) == want:
            return True
    return False


def ensure_suite_worktree(root: Path, commit: str, shared: Sequence[str] = SHARED, *, lock,
                          name: str = SUITE_NAME) -> Path:
    """Only the merge lock's holder brings the worktree to a commit (#420): ``lock`` is the held
    ``MergeLock``; its ``assert_held()`` raises (``MergeLockLost``) before any git command when the
    lock file on disk no longer names this holder."""
    root = Path(root).resolve()  # ``git worktree add`` runs with cwd=root: a relative path would be resolved twice (#134)
    if lock is not None:  # the full run on main (#690) keeps its own worktree ``name`` and holds no merge lock
        lock.assert_held()
    path = root / ".claude" / "worktrees" / name
    if _registered(root, path):
        _run("checkout", "--detach", "--force", commit, cwd=path)
        _run("reset", "--hard", commit, cwd=path)
    else:
        if path.exists():
            raise SuiteWorktreeError(f"{path} exists but is not a worktree of {root}; not touching it")
        _run("worktree", "add", "--detach", str(path), commit, cwd=root)
    for name in shared:
        if (root / name).is_dir() and not (path / name).exists():
            try:
                check_junction_target(path / name, root / name, path)
            except ValueError as exc:
                raise SuiteWorktreeError(str(exc)) from exc
            subprocess.run(["cmd", "/c", "mklink", "/J", str(path / name), str(root / name)],
                           capture_output=True, creationflags=no_window_flags())
    real = [n for n in shared if (root / n).is_dir() and not os.path.isjunction(path / n)]
    if real:
        raise SuiteWorktreeError(f"shared directories are not junctions in {path}: {real}")
    return path
