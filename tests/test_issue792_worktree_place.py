"""#792: a worktree made through the helper lives inside the project, never in the project's parent folder."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def test_helper_worktree_is_inside_the_project_and_the_parent_stays_empty(tmp_path):
    repo = tmp_path / "proj" / "main"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for name in wtmod.SHARED:
        (repo / name).mkdir()
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    parent_before = sorted(os.listdir(repo.parent))
    wt = wtmod.add_sibling_worktree("orch-792-x", "main", main=repo)
    try:
        assert wt.is_dir()
        assert wt.resolve().is_relative_to(repo.resolve())
        assert wt.resolve().parent == (repo / ".claude" / "worktrees").resolve()
        assert wt.name == "wt-orch-792-x"
        assert sorted(os.listdir(repo.parent)) == parent_before
    finally:
        for name in wtmod.SHARED:  # non-recursive: never through the junction
            if os.path.isjunction(wt / name):
                os.rmdir(wt / name)
