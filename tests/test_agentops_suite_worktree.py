import os
import subprocess
from pathlib import Path

import pytest

import threading
import time

from tools.agentops.merge import MergeLock, MergeLockLost
from tools.agentops.suite_worktree import SUITE_NAME, SuiteWorktreeError, ensure_suite_worktree


def g(root, *a):
    r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def commit(root, text):
    (root / "f.txt").write_text(text)
    g(root, "add", "f.txt")
    g(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", text)
    return g(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    g(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("var/\n.claude/\n")
    g(root, "add", ".gitignore")
    (root / "var").mkdir()
    (root / "var" / "x.txt").write_text("shared")
    return root


@pytest.fixture
def held(tmp_path):
    lock = MergeLock(tmp_path / "locks" / "merge.lock")
    lock.acquire(30)
    yield lock
    lock.release()


def n_worktrees(root):
    return len(g(root, "worktree", "list").splitlines())


def test_creates_then_refreshes_without_removal(held, repo):
    c1 = commit(repo, "one")
    assert n_worktrees(repo) == 1
    p = ensure_suite_worktree(repo, c1, lock=held)
    assert n_worktrees(repo) == 2
    c2 = commit(repo, "two")
    assert ensure_suite_worktree(repo, c2, lock=held) == p
    assert n_worktrees(repo) == 2
    assert g(p, "rev-parse", "HEAD") == c2
    assert g(p, "branch", "--show-current") == ""


def test_relative_root_registers_the_worktree_at_its_absolute_path(held, repo, tmp_path, monkeypatch):
    c1 = commit(repo, "one")
    monkeypatch.chdir(tmp_path)  # root is relative to this cwd; git must not resolve it against root again
    p = ensure_suite_worktree(Path("repo"), c1, lock=held)
    want = os.path.normcase(os.path.realpath(repo.resolve() / ".claude" / "worktrees" / SUITE_NAME))
    listed = [os.path.normcase(os.path.realpath(line[9:]))
              for line in g(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
    assert os.path.normcase(os.path.realpath(p)) == want
    assert listed.count(want) == 1 and len(listed) == 2


def test_tracked_reset_untracked_kept(held, repo):
    c1 = commit(repo, "one")
    p = ensure_suite_worktree(repo, c1, lock=held)
    (p / "f.txt").write_text("dirty")
    (p / "untracked.txt").write_text("keep")
    ensure_suite_worktree(repo, c1, lock=held)
    assert (p / "f.txt").read_text() == "one"
    assert (p / "untracked.txt").read_text() == "keep"


def test_shared_junction_once(held, repo, monkeypatch):
    c1 = commit(repo, "one")
    p = ensure_suite_worktree(repo, c1, lock=held)
    assert os.path.isjunction(p / "var")
    assert (p / "var" / "x.txt").read_text() == "shared"
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    ensure_suite_worktree(repo, c1, lock=held)
    assert not [c for c in calls if "mklink" in c]


def test_main_checkout_edit_untouched(held, repo):
    c1 = commit(repo, "one")
    (repo / "f.txt").write_bytes(b"uncommitted\r\nedit")
    ensure_suite_worktree(repo, c1, lock=held)
    ensure_suite_worktree(repo, c1, lock=held)
    assert (repo / "f.txt").read_bytes() == b"uncommitted\r\nedit"


def test_non_worktree_dir_raises_and_stays(held, repo):
    c1 = commit(repo, "one")
    d = repo / ".claude" / "worktrees" / "merge-suite"
    d.mkdir(parents=True)
    (d / "keep.txt").write_text("k")
    with pytest.raises(SuiteWorktreeError):
        ensure_suite_worktree(repo, c1, lock=held)
    assert (d / "keep.txt").read_text() == "k"


def test_second_holder_cannot_sync_while_first_runs_its_suite(repo, tmp_path):
    """#420: the first holder runs a (sleeping) suite in the suite worktree; a second MergeLock
    (own token, same file path) is refused by ensure_suite_worktree and the worktree stays put."""
    lock_path = tmp_path / "locks" / "merge.lock"
    first = MergeLock(lock_path)
    first.acquire(30)
    c1 = commit(repo, "one")
    p = ensure_suite_worktree(repo, c1, lock=first)
    c2 = commit(repo, "two")
    second = MergeLock(lock_path)  # never acquired: the lock file names `first`
    with pytest.raises(MergeLockLost):
        ensure_suite_worktree(repo, c2, lock=second)
    # a second that believes it owns the lock but whose token is not on disk is refused too
    second._own = True
    with pytest.raises(MergeLockLost):
        ensure_suite_worktree(repo, c2, lock=second)
    assert g(p, "rev-parse", "HEAD") == c1
    assert (p / "f.txt").read_text() == "one"
    first.release()


def test_waiting_second_holder_syncs_only_after_release(repo, tmp_path):
    lock_path = tmp_path / "locks" / "merge.lock"
    first = MergeLock(lock_path)
    first.acquire(30)
    c1 = commit(repo, "one")
    p = ensure_suite_worktree(repo, c1, lock=first)
    c2 = commit(repo, "two")
    second = MergeLock(lock_path)
    done = []

    def other():
        second.acquire(30)
        try:
            ensure_suite_worktree(repo, c2, lock=second)
            done.append(g(p, "rev-parse", "HEAD"))
        finally:
            second.release()

    t = threading.Thread(target=other)
    t.start()
    time.sleep(1.0)  # the first's "suite" is still running
    assert not done and g(p, "rev-parse", "HEAD") == c1
    first.release()
    t.join(30)
    assert done == [c2]


def test_release_leaves_another_holders_file(tmp_path):
    lock_path = tmp_path / "locks" / "merge.lock"
    first = MergeLock(lock_path)
    first.acquire(30)
    lock_path.write_text('{"pid": 1, "started": "2026-01-01 00:00:00", "token": "other"}')
    first.release()
    assert lock_path.exists()
