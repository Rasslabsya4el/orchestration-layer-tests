"""#1032: `python -m tools.agentops worktree gc` removes finished worktrees link by link and never the shared
folders. Three made-up worktrees in a temp repo: merged, clean and old, dirty; each holds a junction `var` to a
shared folder with one file. Teardown removes the junctions with `cmd /c rmdir` (non-recursive) only."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402
from tools.agentops.cli import main as cli_main  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows")

DAY = 86400


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def _junction(link, target):
    res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def _drop_links(main):
    """Teardown: `cmd /c rmdir` on each `var` junction left under the test's worktrees (no /S: link only)."""
    wts = Path(main) / ".claude" / "worktrees"
    if not wts.is_dir():
        return
    for wt in wts.iterdir():
        link = wt / "var"
        if os.path.isjunction(link):
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)


@pytest.fixture
def gc_env(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("x", encoding="utf-8")
    _git(main, "add", "f.txt")
    _git(main, "commit", "-q", "-m", "init")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "keep.txt").write_text("shared data", encoding="utf-8")
    (main / ".claude" / "worktrees").mkdir(parents=True)
    monkeypatch.chdir(main)
    yield main, shared
    _drop_links(main)


def _make_three(main, shared):
    wts = main / ".claude" / "worktrees"
    merged, old, dirty = wts / "wt-merged", wts / "wt-old", wts / "wt-dirty"
    # merged: branch merged-b points at main, no own commit -> merged into main
    _git(main, "worktree", "add", "-q", "-b", "merged-b", str(merged), "main")
    # clean and old: one own commit (not merged), files back-dated 4 days
    _git(main, "worktree", "add", "-q", "-b", "old-b", str(old), "main")
    (old / "old.txt").write_text("committed", encoding="utf-8")
    _git(old, "add", "old.txt")
    _git(old, "commit", "-q", "-m", "old work")
    back = time.time() - 4 * DAY
    for p in (old / "old.txt", old / "f.txt"):
        os.utime(p, (back, back))
    # dirty: one own commit, plus an uncommitted file
    _git(main, "worktree", "add", "-q", "-b", "dirty-b", str(dirty), "main")
    (dirty / "d.txt").write_text("committed", encoding="utf-8")
    _git(dirty, "add", "d.txt")
    _git(dirty, "commit", "-q", "-m", "dirty work")
    (dirty / "uncommitted.txt").write_text("not yet committed", encoding="utf-8")
    for wt in (merged, old, dirty):
        _junction(wt / "var", shared)
    return merged, old, dirty


def test_dry_run_lists_three_rows_and_removes_nothing(gc_env, capsys):
    main, shared = gc_env
    merged, old, dirty = _make_three(main, shared)
    assert cli_main(["worktree", "gc"]) == 0
    out = capsys.readouterr().out
    rows = {}
    for line in out.splitlines():
        if line.startswith("wt-"):
            cells = [c.strip() for c in line.split(" | ")]
            rows[cells[0]] = cells[2]
    assert rows == {"wt-merged": "merged", "wt-old": "clean and old", "wt-dirty": "kept: dirty"}, out
    assert merged.exists() and old.exists() and dirty.exists()
    assert os.path.isjunction(merged / "var") and os.path.isjunction(old / "var") and os.path.isjunction(dirty / "var")
    assert "merged-b" in _git(main, "branch", "--format=%(refname:short)")


def test_apply_removes_merged_and_clean_old_keeps_dirty(gc_env, capsys):
    main, shared = gc_env
    merged, old, dirty = _make_three(main, shared)
    assert cli_main(["worktree", "gc", "--apply"]) == 0
    assert not merged.exists()
    assert not old.exists()
    assert dirty.exists() and os.path.isjunction(dirty / "var")
    assert sorted(os.listdir(shared)) == ["keep.txt"]
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared data"
    branches = _git(main, "branch", "--format=%(refname:short)").split()
    assert "merged-b" not in branches
    assert "old-b" in branches and "dirty-b" in branches


def test_run_stops_with_exit_2_when_a_junction_survives(gc_env, monkeypatch):
    main, shared = gc_env
    merged, old, dirty = _make_three(main, shared)
    monkeypatch.setattr(wtmod, "remove_junction", lambda link: None)  # the rmdir step does nothing
    assert cli_main(["worktree", "gc", "--apply"]) == 2
    assert merged.exists() and old.exists() and dirty.exists()
    assert os.path.isjunction(merged / "var") and os.path.isjunction(old / "var")
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared data"


def test_apply_removes_a_worktree_that_tracks_a_file_under_a_shared_name(gc_env, capsys):
    """Old worktrees hold a TRACKED file under `var/`; once the `var` link is gone git sees it deleted and
    refuses `worktree remove` without --force. The run restores it first, so the worktree goes and the shared
    folder is untouched (the two stops of 2026-10-14, `gpt-2026-09-19-*`)."""
    main, shared = gc_env
    wts = main / ".claude" / "worktrees"
    (main / "var").mkdir()
    (main / "var" / "tracked.md").write_text("tracked", encoding="utf-8")
    _git(main, "add", "var/tracked.md")
    _git(main, "commit", "-q", "-m", "tracks var/tracked.md")
    tracked = wts / "wt-tracked"
    _git(main, "worktree", "add", "-q", "-b", "tracked-b", str(tracked), "main")
    import shutil
    shutil.rmtree(tracked / "var")  # a real folder with a tracked file, nothing is linked yet
    _junction(tracked / "var", shared)
    assert cli_main(["worktree", "gc", "--apply"]) == 0
    assert not tracked.exists()
    assert sorted(os.listdir(shared)) == ["keep.txt"]
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared data"
    assert "removed wt-tracked" in capsys.readouterr().out


def test_keep_newer_than_hours_keeps_a_fresh_worktree_even_when_merged(gc_env, capsys):
    """A worker's fresh worktree sits at main's head and counts as merged; the flag keeps it (kept: live)."""
    main, shared = gc_env
    merged, old, dirty = _make_three(main, shared)
    assert cli_main(["worktree", "gc", "--apply", "--keep-newer-than-hours", "1"]) == 0
    out = capsys.readouterr().out
    assert merged.exists() and old.exists() and dirty.exists()
    rows = {}
    for line in out.splitlines():
        if line.startswith("wt-"):
            cells = [c.strip() for c in line.split(" | ")]
            rows[cells[0]] = cells[2]
    assert rows == {"wt-merged": "kept: live", "wt-old": "kept: live", "wt-dirty": "kept: dirty"}, out
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared data"


def test_apply_goes_on_past_a_row_whose_folder_went_with_a_removed_parent(gc_env, capsys):
    """A worktree nested in one removed earlier in the same run has no folder left; the run goes on (the crash
    of 2026-10-14 23:04: `git checkout` with a cwd that is gone)."""
    main, shared = gc_env
    gone = main / ".claude" / "worktrees" / "wt-gone"
    row = wtmod.GcRow(gone, "gone-b", wtmod.GC_REASON_MERGED, True, True, [], None)
    assert wtmod.apply_worktree_gc(main, [row]) == 0
    assert "gone already wt-gone" in capsys.readouterr().out
