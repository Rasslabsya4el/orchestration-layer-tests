"""#449: `python -m tools.agentops junctions` and `_ensure_junctions` in a fresh linked worktree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402
from tools.agentops.cli import main  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo_wt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for name in wtmod.SHARED:
        (repo / name).mkdir()
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "b", str(wt))
    yield repo, wt
    for name in wtmod.SHARED:  # non-recursive: never through the junction
        if os.path.isjunction(wt / name):
            os.rmdir(wt / name)


def test_ensure_junctions_twice(repo_wt):
    repo, wt = repo_wt
    assert wtmod._ensure_junctions(wt, repo) == {n: "made" for n in wtmod.SHARED}
    assert all(os.path.isjunction(wt / n) for n in wtmod.SHARED)
    assert wtmod._ensure_junctions(wt, repo) == {n: "present" for n in wtmod.SHARED}


def test_cli_twice(repo_wt, capsys):
    repo, wt = repo_wt
    assert main(["junctions", str(wt)]) == 0
    first = capsys.readouterr().out
    assert main(["junctions", str(wt)]) == 0
    second = capsys.readouterr().out
    print(first + second)
    assert "0 present / 3 made" in first
    assert "3 present / 0 made" in second


def test_cli_refuses_main_checkout(repo_wt, capsys):
    repo, _ = repo_wt
    assert main(["junctions", str(repo)]) == 1
    assert "main checkout" in capsys.readouterr().out


def test_real_directory_is_not_replaced(repo_wt):
    repo, wt = repo_wt
    (wt / "var").mkdir()
    # an untracked file: an EMPTY real directory holds nothing git does not track, and #633 (orch-633)
    # replaces such a tracked-only directory with a junction on purpose (the same test with an empty dir
    # failed deterministically in every suite run that held that branch: #669)
    (wt / "var" / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit, match="real directory"):
        wtmod._ensure_junctions(wt, repo)
    assert not os.path.isjunction(wt / "var")
    assert (wt / "var" / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_target_inside_the_worktree_is_refused(repo_wt):
    _, wt = repo_wt
    (wt / "inner" / "var").mkdir(parents=True)
    with pytest.raises(SystemExit, match="inside"):
        wtmod._ensure_junctions(wt, wt / "inner")


def test_failing_mklink_raises(repo_wt, monkeypatch):
    repo, wt = repo_wt
    monkeypatch.setattr(wtmod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "denied"))
    with pytest.raises(SystemExit, match="rc 1.*denied"):
        wtmod._ensure_junctions(wt, repo)
