"""#1060: `python -m tools.agentops worktree gc --dirty --apply` saves and removes a worktree whose only tracked edit is
a line-ending change (CRLF under core.autocrlf=true: `git diff` prints no hunk). Its save writes no tracked.patch and
the line says "tracked: line endings only". Row 2: a real tracked edit keeps the patch path (patch saved and checked).
Throwaway repo in a temp folder, no gh (the issue lookup is a fake map). Rows 1 and 2 of the ticket."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402
from tools.agentops.cli import main as cli_main  # noqa: E402

ISSUES = {9: "CLOSED"}
CRLF_B = b"base line\r\nsecond\r\n"
EDIT_B = b"edited line\nsecond\n"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def _fake_state(number, main):
    return ISSUES.get(number, "")


def _make(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    # committed with LF while autocrlf is off: the worktree is then checked out with LF
    _git(main, "config", "core.autocrlf", "false")
    (main / "f.txt").write_bytes(b"base line\nsecond\n")
    _git(main, "add", "f.txt")
    _git(main, "commit", "-q", "-m", "init")
    (main / ".claude" / "worktrees").mkdir(parents=True)
    wt = main / ".claude" / "worktrees" / "wt-orch-9"
    _git(main, "worktree", "add", "-q", "-b", "orch-9", str(wt), "main")
    monkeypatch.chdir(main)
    monkeypatch.setattr(wtmod, "gh_issue_state", _fake_state)
    return main, wt


def _save_root(main):
    return main / "var" / "agentops" / "worktree-save"


def test_row1_crlf_only_edit_is_saved_without_a_patch_and_removed(tmp_path, monkeypatch, capsys):
    main, wt = _make(tmp_path, monkeypatch)
    # the machine's setting (core.autocrlf=true) is on when the edit is made and when gc runs
    _git(main, "config", "core.autocrlf", "true")
    (wt / "f.txt").write_bytes(CRLF_B)
    assert _git(wt, "diff", "--stat", "HEAD") == ""  # git diff prints no hunk for a CRLF-only edit
    assert "f.txt" in _git(wt, "status", "--short")  # ... yet git status lists the file as modified

    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "SKIP" not in out, out
    assert "tracked: line endings only" in out, out
    assert "saved wt-orch-9" in out, out
    assert "skipped 0" in out, out
    assert not wt.exists()

    save = _save_root(main) / "wt-orch-9"
    assert sorted(os.listdir(save)) == ["head.txt"]  # no tracked.patch for a line-ending-only save
    assert (save / "head.txt").read_text(encoding="utf-8").startswith("branch: orch-9\n")


def test_row2_a_real_tracked_edit_keeps_the_patch_path(tmp_path, monkeypatch, capsys):
    main, wt = _make(tmp_path, monkeypatch)
    (wt / "f.txt").write_bytes(EDIT_B)

    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "SKIP" not in out, out
    assert "line endings only" not in out, out
    assert "saved wt-orch-9" in out, out
    assert "skipped 0" in out, out
    assert not wt.exists()

    save = _save_root(main) / "wt-orch-9"
    assert sorted(os.listdir(save)) == ["head.txt", "tracked.patch"]
    patch = (save / "tracked.patch").read_text(encoding="utf-8")
    assert "+edited line" in patch and "-base line" in patch, patch
    fresh = tmp_path / "fresh"
    _git(main, "worktree", "add", "-q", "--detach", str(fresh), "main")
    _git(fresh, "apply", str(save / "tracked.patch"))
    assert (fresh / "f.txt").read_bytes() == EDIT_B
