"""tools/agentops/worktree.py: the WIP-commit-on-every-exit-path guarantee (requirement 2),
exercised on a throwaway git repo -- no real opencode, no real worktree of this project."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.agentops.worktree import commit_wip, head_commit


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "agentops-test@example.invalid", cwd=root)
    _git("config", "user.name", "agentops test", cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "initial", cwd=root)
    return root


def test_commit_wip_on_simulated_crash(repo: Path):
    """A worker "crashes" mid-edit: uncommitted changes must land as one WIP commit, so the
    harness never loses the worker's tree (CLAUDE.md, the GPT-era harness's existing rule, now
    removed, #65)."""
    (repo / "output.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("hello, edited\n", encoding="utf-8")

    before = head_commit(repo)
    committed, detail = commit_wip(repo, "smoke-task", "crashed rc=1", elapsed_seconds=125.0)

    assert committed, detail
    after = head_commit(repo)
    assert after != before

    log = _git("log", "-1", "--format=%s", cwd=repo).stdout.strip()
    assert log.startswith("WIP (agentops): smoke-task ended by crashed rc=1 after")
    assert "2.1 min" in log or "2 min" in log  # human_duration(125s), not a bare second count

    status = _git("status", "--porcelain", cwd=repo).stdout
    assert status.strip() == ""  # nothing left uncommitted


def test_commit_wip_on_clean_tree_is_a_noop(repo: Path):
    committed, detail = commit_wip(repo, "smoke-task", "finished", elapsed_seconds=10.0)
    assert not committed
    assert "clean" in detail


def test_junction_target_inside_own_worktree_refused(tmp_path: Path):
    from tools.agentops.worktree import check_junction_target
    main, wt = tmp_path / "main", tmp_path / "main" / ".claude" / "worktrees" / "b"
    (main / "var").mkdir(parents=True)
    (wt / "runtime").mkdir(parents=True)
    check_junction_target(wt / "var", main / "var", wt)  # the normal case passes
    with pytest.raises(ValueError) as exc:
        check_junction_target(wt / "var", wt / "runtime", wt)
    assert str(wt / "var") in str(exc.value) and str(wt / "runtime") in str(exc.value)
    assert not (wt / "var").exists()
    with pytest.raises(ValueError):  # under the link itself, and the link itself
        check_junction_target(wt / "var", wt / "var" / "x", wt)
    with pytest.raises(ValueError):
        check_junction_target(wt / "var", wt / "var", wt)


def _mklink(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junction_check_before_and_after_the_junction_exists(tmp_path: Path):
    from tools.agentops.worktree import check_junction_target
    snap = tmp_path / "snapshots" / "x"
    snap.mkdir(parents=True)
    made = []
    try:
        for name in ("var", "artifacts", "vendor"):
            target = tmp_path / name
            target.mkdir()
            check_junction_target(snap / name, target, snap)  # before
            _mklink(snap / name, target)
            made.append(snap / name)
            check_junction_target(snap / name, target, snap)  # after: not resolved into the target
    finally:
        for link in made:
            os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junction_under_its_own_target_refused(tmp_path: Path):
    from tools.agentops.worktree import check_junction_target
    target = tmp_path / "artifacts"
    snap = target / "runs" / "snap" / "x"
    snap.mkdir(parents=True)
    link = snap / "artifacts"
    with pytest.raises(ValueError) as exc:
        check_junction_target(link, target, snap)
    assert str(link) in str(exc.value) and str(target) in str(exc.value)
    _mklink(link, target)  # even once it exists, the loop is refused
    try:
        with pytest.raises(ValueError):
            check_junction_target(link, target, snap)
    finally:
        os.rmdir(link)
