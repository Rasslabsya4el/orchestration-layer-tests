"""#1063: a save writes into `<name>.partial` and renames it to `<name>` only when every step passed; a failed save
leaves the partial folder, the next run reuses it, and nothing is ever deleted. Throwaway git repos in tmp_path."""

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
    (repo / "f.txt").write_bytes(b"base" + bytes([10]))
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo


def _dirty_worktree(repo, name):
    path = repo / ".claude" / "worktrees" / name
    _git(repo, "worktree", "add", "-q", "-b", f"{name}-b", str(path), "main")
    (path / "u.txt").write_text("untracked", encoding="utf-8")
    (path / "sub").mkdir()
    (path / "sub" / "v.txt").write_text("more", encoding="utf-8")
    return path


def _row(path):
    return wtmod.GcRow(path, f"{path.name}-b", wtmod.GC_REASON_DIRTY, True, False, [], None)


def _dirty_row(path):
    return wtmod.DirtyRow(_row(path), "save+remove", True, 0, 1, 0)


# rows 1 and 2: a failing step leaves `.partial` and no final folder; the second run saves and the worktree goes

def test_failed_save_leaves_partial_then_second_run_saves_and_removes(main, tmp_path, monkeypatch, capsys):
    path = _dirty_worktree(main, "wt-a")
    save_root = tmp_path / "save"
    real_head = wtmod.head_commit
    monkeypatch.setattr(wtmod, "head_commit", lambda _wt: None)  # the last step fails: "head"
    row = _row(path)
    folder = save_root / "wt-a"
    assert wtmod._save_worktree(row, folder) == "head"
    partial = save_root / "wt-a.partial"
    assert partial.is_dir() and not folder.exists()
    assert (partial / "untracked" / "u.txt").read_text(encoding="utf-8") == "untracked"
    monkeypatch.setattr(wtmod, "head_commit", real_head)
    assert wtmod._save_worktree(row, folder) is None
    assert (folder / "head.txt").exists() and not partial.exists()
    assert (folder / "untracked" / "sub" / "v.txt").read_text(encoding="utf-8") == "more"


# row 1's printed line

def test_failed_save_prints_the_skip_line(main, tmp_path, monkeypatch, capsys):
    path = _dirty_worktree(main, "wt-b")
    monkeypatch.setattr(wtmod, "head_commit", lambda _wt: None)
    wtmod.apply_dirty_gc(main, [_dirty_row(path)])
    out = capsys.readouterr().out
    assert "SKIP: wt-b: save failed (head)" in out, out
    assert path.exists()


# row 3: a stray file in an existing partial folder stays, and the count check passes

def test_existing_partial_with_a_stray_file_is_reused(main, tmp_path):
    path = _dirty_worktree(main, "wt-c")
    folder = tmp_path / "save" / "wt-c"
    partial = tmp_path / "save" / "wt-c.partial"
    (partial / "untracked").mkdir(parents=True)
    (partial / "untracked" / "stray.txt").write_text("old", encoding="utf-8")
    assert wtmod._save_worktree(_row(path), folder) is None
    assert (folder / "untracked" / "stray.txt").read_text(encoding="utf-8") == "old"
    assert (folder / "untracked" / "u.txt").exists() and (folder / "head.txt").exists()


# row 4: an existing final folder answers `folder` and nothing is written

def test_existing_final_folder_answers_folder(main, tmp_path):
    path = _dirty_worktree(main, "wt-d")
    folder = tmp_path / "save" / "wt-d"
    folder.mkdir(parents=True)
    assert wtmod._save_worktree(_row(path), folder) == "folder"
    assert list(folder.iterdir()) == []
    assert not (tmp_path / "save" / "wt-d.partial").exists()
