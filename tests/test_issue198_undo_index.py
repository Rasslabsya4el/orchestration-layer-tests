"""Issue 198 unit 2: every undo of a failed core-update commit leaves the index AND the worktree of the touched
paths as they were, and the text `undone, nothing changed` is printed only when `git status --porcelain` of those
paths (index column included) is empty; otherwise the error names the still-dirty paths. Real git on a tmp target
(bare origin + clone); the home is a fake `gh` runner, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_issue28_core_update_failed_commit import (
    MANIFEST, NEW_HOME_FILES, OLD_FILES, ORCH_JSON, _git, _hook, _run_update, _snapshot, _target, _write,
)
from tools.agentops import core_update
from tools.agentops.core_update import CoreUpdateError

TOUCHED = [ORCH_JSON, MANIFEST, *NEW_HOME_FILES]


def _staged(root: Path) -> str:
    return _git(root, "diff", "--cached", "--", *TOUCHED).stdout


def _fake_git(monkeypatch: pytest.MonkeyPatch, subcommand: str, returncode: int = 0) -> None:
    """`core_update._git` answers ``returncode`` and does nothing for ``subcommand``; real git otherwise."""

    real = core_update._git

    def fake(cwd: Path, *args: str, env=None):
        if args and args[0] == subcommand:
            return subprocess.CompletedProcess(["git", *args], returncode, "", "fatal: forced by the test")
        return real(cwd, *args, env=env)

    monkeypatch.setattr(core_update, "_git", fake)


# (1) a commit that fails after the files are staged: index empty, text claims a clean undo.
def test_failed_commit_leaves_the_index_empty(tmp_path: Path) -> None:
    root = _target(tmp_path)
    before = _snapshot(root)
    _hook(root, "pre-commit")

    error = _run_update(root)

    assert str(error).endswith("undone, nothing changed")
    assert _staged(root) == ""
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _snapshot(root) == before


# (1b) the same failure in a checkout mid-merge (git refuses a partial commit there): the merge's own staged
# file stays staged, the update's paths are clean in index and worktree.
def test_failed_commit_mid_merge_leaves_the_merges_index_alone(tmp_path: Path) -> None:
    root = _target(tmp_path)
    assert _git(root, "checkout", "-b", "side").returncode == 0
    _write(root, "docs/agents/extra.md", "extra\n")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "side adds extra").returncode == 0
    assert _git(root, "checkout", "main").returncode == 0
    assert _git(root, "merge", "--no-commit", "--no-ff", "side").returncode == 0
    paths = {ORCH_JSON, "docs/agents/one.md"}
    before = {p: (root / p).read_bytes() for p in sorted(paths)}
    _write(root, "docs/agents/one.md", "one v2\n")
    _write(root, ORCH_JSON, '{"core_version": "b"}\n')

    with pytest.raises(CoreUpdateError) as caught:
        core_update._commit_and_push(root, paths, "b" * 40, before)

    assert "partial commit during a merge" in str(caught.value)
    assert str(caught.value).endswith("undone, nothing changed")
    assert _git(root, "diff", "--cached", "--name-only").stdout.split() == ["docs/agents/extra.md"]
    assert core_update._dirty_paths(root, paths) == []
    assert {p: (root / p).read_bytes() for p in sorted(paths)} == before


# (2) a forced leftover: the unstaging `git reset` does nothing, so the index keeps the update: named, not "nothing changed".
def test_leftover_index_is_named_not_called_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path)
    _hook(root, "pre-commit")
    _fake_git(monkeypatch, "reset")

    error = str(_run_update(root))

    assert "nothing changed" not in error
    assert "git commit failed" in error
    assert ORCH_JSON in error
    assert "docs/agents/one.md" in error
    assert _staged(root) != ""


# (2b) a status that cannot be read is not a clean status.
def test_unreadable_status_is_not_called_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path)
    _hook(root, "pre-commit")
    _fake_git(monkeypatch, "status", returncode=128)

    error = str(_run_update(root))

    assert "nothing changed" not in error
    assert ORCH_JSON in error


# (3) a refused push: the local commit is dropped, the index is empty, the text claims a clean undo.
def test_refused_push_leaves_the_index_empty(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _snapshot(root)
    _hook(root, "pre-push")

    error = _run_update(root)

    assert "push refused" in str(error)
    assert str(error).endswith("undone, nothing changed")
    assert _staged(root) == ""
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert _snapshot(root) == before


# (3b) a refused push whose undo leaves the update staged: named, not "nothing changed".
def test_refused_push_with_leftover_is_named_not_called_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path)
    _hook(root, "pre-push")
    _fake_git(monkeypatch, "restore")

    error = str(_run_update(root))

    assert "push refused" in error
    assert "nothing changed" not in error
    assert ORCH_JSON in error
    assert "docs/agents/one.md" in error
