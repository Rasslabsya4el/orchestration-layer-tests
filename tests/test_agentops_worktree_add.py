"""#576: `python -m tools.agentops worktree add <name> --base <ref>` makes a worktree under .claude/worktrees (#792)."""

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
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for name in wtmod.SHARED:
        (repo / name).mkdir()
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "orch-1")
    (repo / "g.txt").write_text("y", encoding="utf-8")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", "two")
    _git(repo, "checkout", "-q", "main")
    monkeypatch.chdir(repo)
    yield tmp_path, repo
    for wt in (repo / ".claude" / "worktrees").glob("wt-*"):
        for name in wtmod.SHARED:  # non-recursive: never through the junction
            if os.path.isjunction(wt / name):
                os.rmdir(wt / name)


def test_add_makes_worktree(env, capsys):
    tmp, repo = env
    assert main(["worktree", "add", "orch-1-u1", "--base", "orch-1"]) == 0
    out = capsys.readouterr().out
    wt = repo / ".claude" / "worktrees" / "wt-orch-1-u1"
    assert out.splitlines() == [str(wt)]
    assert wt.is_dir()
    assert _git(wt, "branch", "--show-current") == "orch-1-u1"
    assert _git(wt, "rev-parse", "HEAD") == _git(repo, "rev-parse", "orch-1")
    for n in wtmod.SHARED:
        assert os.path.realpath(wt / n) == os.path.realpath(repo / n)


def test_add_from_linked_worktree(env, capsys, monkeypatch):
    tmp, repo = env
    assert main(["worktree", "add", "orch-1-u1", "--base", "orch-1"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(repo / ".claude" / "worktrees" / "wt-orch-1-u1")
    assert main(["worktree", "add", "orch-1-u2", "--base", "orch-1"]) == 0
    assert capsys.readouterr().out.splitlines() == [str(repo / ".claude" / "worktrees" / "wt-orch-1-u2")]
    assert os.path.realpath(repo / ".claude" / "worktrees" / "wt-orch-1-u2" / "var") == os.path.realpath(repo / "var")


def test_existing_and_bad_base_change_nothing(env, capsys):
    tmp, repo = env
    assert main(["worktree", "add", "orch-1-u1", "--base", "orch-1"]) == 0
    capsys.readouterr()
    branches, listing = _git(repo, "branch", "--list"), sorted(os.listdir(tmp))
    assert main(["worktree", "add", "orch-1-u1", "--base", "orch-1"]) == 1
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    assert _git(repo, "branch", "--list") == branches and sorted(os.listdir(tmp)) == listing
    assert main(["worktree", "add", "nope", "--base", "no-such-ref"]) == 1
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    assert _git(repo, "branch", "--list") == branches and sorted(os.listdir(tmp)) == listing


def _track_gitkeep(repo):
    (repo / "var" / "x").mkdir()
    (repo / "var" / "x" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-f", "var/x/.gitkeep")
    _git(repo, "commit", "-q", "-m", "track gitkeep")


def test_tracked_only_shared_dir_becomes_a_junction(env):
    tmp, repo = env
    _track_gitkeep(repo)
    wt = wtmod.add_sibling_worktree("orch-2", "main", main=repo)
    assert os.path.isjunction(wt / "var")
    assert os.path.realpath(wt / "var") == os.path.realpath(repo / "var")
    assert _git(wt, "status", "--porcelain") == ""


def test_untracked_entry_in_shared_dir_is_still_refused(env):
    tmp, repo = env
    _track_gitkeep(repo)
    wt = tmp / "wt-orch-3"  # made by hand, outside the helper: stays valid
    _git(repo, "worktree", "add", "-q", "-b", "orch-3", str(wt), "main")
    (wt / "var" / "untracked.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit, match="is a real directory, not a junction: refused, never replaced"):
        wtmod._ensure_junctions(wt, repo)
    assert (wt / "var" / "untracked.txt").read_text(encoding="utf-8") == "keep"
    assert not os.path.isjunction(wt / "var")


def _commit_files(repo, files, msg):
    for rel, data in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def test_133_draw_copy_lands_in_each_mod_that_ignores_it(env):
    """#133: mods/m1 lists hooks/draw.ts -> its hooks/draw.ts is the new worktree's mods/shared/draw.ts; m2 is not touched."""
    tmp, repo = env
    _commit_files(repo, {
        "mods/shared/draw.ts": b"draw A",
        "mods/m1/.gitignore": b"hooks/draw.ts\n",
        "mods/m2/.gitignore": b"*.log\n",
    }, "mods")
    assert main(["worktree", "add", "i133", "--base", "main"]) == 0
    wt = repo / ".claude" / "worktrees" / "wt-i133"
    assert (wt / "mods" / "m1" / "hooks" / "draw.ts").read_bytes() == b"draw A"
    assert not (wt / "mods" / "m2" / "hooks" / "draw.ts").exists()
    assert _git(wt, "status", "--porcelain") == ""


def test_133_copy_takes_the_base_branch_bytes(env):
    """#133: the source is the worktree's own copy, so a base branch with other bytes wins over main's checkout."""
    tmp, repo = env
    _commit_files(repo, {
        "mods/shared/draw.ts": b"draw A",
        "mods/m1/.gitignore": b"hooks/draw.ts\n",
    }, "mods")
    _git(repo, "checkout", "-q", "-b", "draw-b")
    _commit_files(repo, {"mods/shared/draw.ts": b"draw B"}, "draw B")
    _git(repo, "checkout", "-q", "main")
    assert main(["worktree", "add", "i133-b", "--base", "draw-b"]) == 0
    wt = repo / ".claude" / "worktrees" / "wt-i133-b"
    assert (wt / "mods" / "m1" / "hooks" / "draw.ts").read_bytes() == b"draw B"
    assert (repo / "mods" / "shared" / "draw.ts").read_bytes() == b"draw A"
    assert _git(wt, "status", "--porcelain") == ""


def test_133_no_shared_draw_copies_nothing(env):
    """#133: no mods/shared/draw.ts in the new worktree -> nothing copied, rc 0."""
    tmp, repo = env
    _commit_files(repo, {"mods/m1/.gitignore": b"hooks/draw.ts\n"}, "no draw")
    assert main(["worktree", "add", "i133-c", "--base", "main"]) == 0
    wt = repo / ".claude" / "worktrees" / "wt-i133-c"
    assert not (wt / "mods" / "m1" / "hooks").exists()
