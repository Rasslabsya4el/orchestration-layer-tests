"""#1056: `branch_issue` reads the ticket number only from the branch shapes this repo makes; the cleanup summary
counts every SKIP (failed saves and failed removes); a save skips nested git repositories and says so. Throwaway
git repos in tmp_path only: no junction, no live removal (the removal is faked where it is the point of a test)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def main(tmp_path):
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "f.txt").write_bytes(b"base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo


def _worktree(repo, name):
    path = repo / ".claude" / "worktrees" / name
    _git(repo, "worktree", "add", "-q", "-b", f"{name}-b", str(path), "main")
    return path


# row 1: the ticket shapes of the repo's own branches

@pytest.mark.parametrize("branch, number", [
    ("issue-246", 246),
    ("o1052", 1052),
    ("orch-945", 945),
    ("wt-861-u2", 861),
    ("wt-orch-970", 970),
    ("945-cards", 945),
    ("cards-995", 995),
    ("restore-1052-bundle", 1052),
    ("o1051b", 1051),
    ("o958-film", 958),
    ("orch-203b", 203),
    ("orch-724u1b", 724),
    ("research/175-levers-context-line", 175),
])
def test_ticket_branch_shapes_name_their_issue(branch, number):
    assert wtmod.branch_issue(branch) == number


# row 2: hashes, dates and letter-then-digit names are no ticket

@pytest.mark.parametrize("branch", [
    "agent-a0018ec5c8e4f1b2c3d4e5f6a7b8c9d0e1f2a3b4",
    "worktree-agent-a0018ec661fed910c",
    "2026-09-22-smoke",
    "gpt-2026-09-19-charge-triggers-warcry-when-hit",
    "batch-20261009b",
    "bat3",
    "check",
    "",
    None,
])
def test_non_ticket_names_have_no_issue(branch):
    assert wtmod.branch_issue(branch) is None


# row 3: the summary counts every SKIP of the run

def test_summary_counts_two_save_failures_and_one_remove_failure(main, monkeypatch, capsys):
    paths = [_worktree(main, name) for name in ("wt-s1", "wt-s2", "wt-r")]
    dirty = [wtmod.DirtyRow(wtmod.GcRow(p, f"{p.name}-b", wtmod.GC_REASON_DIRTY, True, False, [], None),
                            "save+remove", True, 0, 1, 0) for p in paths]

    def fake_save(row, folder):
        return "untracked" if row.path.name in ("wt-s1", "wt-s2") else None

    real_git = wtmod.git

    def fake_git(*args, cwd=wtmod.ROOT):
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: simulated remove failure")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(wtmod, "_save_worktree", fake_save)
    monkeypatch.setattr(wtmod, "git", fake_git)
    assert wtmod.apply_dirty_gc(main, dirty) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    skip_lines = [line for line in lines if line.startswith("SKIP:")]
    assert len(skip_lines) == 3, out
    assert sum("save failed (untracked)" in line for line in skip_lines) == 2, out
    assert sum("git worktree remove" in line for line in skip_lines) == 1, out
    assert lines[-1] == "removed 0 worktree(s), 0 junction link(s), deleted 0 branch(es), skipped 3", out
    assert all(p.exists() for p in paths)


def test_summary_counts_save_failures_when_nothing_is_left_to_remove(main, monkeypatch, capsys):
    path = _worktree(main, "wt-only")
    dirty = [wtmod.DirtyRow(wtmod.GcRow(path, "wt-only-b", wtmod.GC_REASON_DIRTY, True, False, [], None),
                            "save+remove", True, 0, 1, 0)]
    monkeypatch.setattr(wtmod, "_save_worktree", lambda row, folder: "bundle")
    assert wtmod.apply_dirty_gc(main, dirty) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "SKIP: wt-only: save failed (bundle)"
    assert lines[-1] == "removed 0 worktree(s), 0 junction link(s), deleted 0 branch(es), skipped 1"
    assert path.exists()


# row 4: a save skips nested git repositories, names them, and checks what it copied

def test_untracked_save_skips_a_bare_repo_and_names_it(main, tmp_path, capsys):
    wt = _worktree(main, "wt-n")
    (wt / "x" / "origin.git").mkdir(parents=True)
    (wt / "x" / "origin.git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (wt / "x" / "a.txt").write_text("kept", encoding="utf-8")
    folder = tmp_path / "save" / "wt-n"
    row = wtmod.GcRow(wt, "wt-n-b", wtmod.GC_REASON_DIRTY, True, False, [], None)
    assert wtmod._save_worktree(row, folder) is None
    assert (folder / "untracked" / "x" / "a.txt").read_text(encoding="utf-8") == "kept"
    assert not (folder / "untracked" / "x" / "origin.git").exists()
    out = capsys.readouterr().out
    assert "nested git skipped, not saved" in out and "x/origin.git/HEAD" in out, out


def test_untracked_save_skips_a_nested_working_repo_named_as_a_directory(main, tmp_path, capsys):
    wt = _worktree(main, "wt-m")
    nested = wt / "scratch" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (wt / "scratch" / "b.txt").write_text("kept", encoding="utf-8")
    folder = tmp_path / "save" / "wt-m"
    row = wtmod.GcRow(wt, "wt-m-b", wtmod.GC_REASON_DIRTY, True, False, [], None)
    assert wtmod._save_worktree(row, folder) is None
    assert (folder / "untracked" / "scratch" / "b.txt").read_text(encoding="utf-8") == "kept"
    assert not (folder / "untracked" / "scratch" / "nested").exists()
    assert "scratch/nested/" in capsys.readouterr().out
