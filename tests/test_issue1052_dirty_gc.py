"""#1052: `python -m tools.agentops worktree gc --dirty` saves a dirty worktree (commits.bundle, tracked.patch,
untracked/, head.txt under `<main>/var/agentops/worktree-save/<name>/`) and then removes it link by link, unless
its branch's issue is OPEN. Four made-up worktrees in a temp repo: (a) 2 commits ahead, issue 7 CLOSED; (b) a
tracked edit and an untracked file, issue 9 CLOSED; (c) 1 commit ahead, issue 11 OPEN; (d) an untracked file, no
issue number. The issue lookup is a fake map (no gh). Rows 1 to 4 of the ticket."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.agentops import worktree as wtmod  # noqa: E402
from tools.agentops.cli import main as cli_main  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows")

ISSUES = {7: "CLOSED", 9: "CLOSED", 11: "OPEN"}
EDIT_B = b"edited line\nsecond\n"
UNTRACKED_B = b"untracked b\n"
UNTRACKED_D = b"untracked d\n"


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


def _fake_state(number, main):
    return ISSUES.get(number, "")


@pytest.fixture
def gc_env(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    # the machine's gitconfig has core.autocrlf=true: a restore would write CRLF and the byte compare of row 2
    # would test the machine's line endings, not the save; the throwaway repo keeps bytes as written
    _git(main, "config", "core.autocrlf", "false")
    (main / "f.txt").write_bytes(b"base line\n")
    _git(main, "add", "f.txt")
    _git(main, "commit", "-q", "-m", "init")
    shared = tmp_path / "shared"
    shared.mkdir()
    (main / ".claude" / "worktrees").mkdir(parents=True)
    monkeypatch.chdir(main)
    monkeypatch.setattr(wtmod, "gh_issue_state", _fake_state)
    yield main, shared
    _drop_links(main)


def _build(main, shared=None):
    wts = main / ".claude" / "worktrees"
    a, b, c, d = wts / "wt-a", wts / "wt-b", wts / "wt-c", wts / "wt-d"
    _git(main, "worktree", "add", "-q", "-b", "issue-7-u1", str(a), "main")
    for i in (1, 2):
        (a / f"a{i}.txt").write_bytes(f"commit {i}\n".encode())
        _git(a, "add", f"a{i}.txt")
        _git(a, "commit", "-q", "-m", f"a{i}")
    _git(main, "worktree", "add", "-q", "-b", "orch-9", str(b), "main")
    (b / "f.txt").write_bytes(EDIT_B)
    (b / "b-new.txt").write_bytes(UNTRACKED_B)
    _git(main, "worktree", "add", "-q", "-b", "o11", str(c), "main")
    (c / "c1.txt").write_bytes(b"c\n")
    _git(c, "add", "c1.txt")
    _git(c, "commit", "-q", "-m", "c1")
    _git(main, "worktree", "add", "-q", "-b", "free-b", str(d), "main")
    (d / "d-new.txt").write_bytes(UNTRACKED_D)
    if shared is not None:
        for wt in (a, b, c, d):
            _junction(wt / "var", shared)
    return a, b, c, d


def _dirty_decisions(out):
    """{worktree: decision} from the second pass's table (the table after its header line)."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("worktree") and "decision" in line)
    rows = {}
    for line in lines[start + 2:]:
        if not line.startswith("wt-"):
            break
        cells = [c.strip() for c in line.split(" | ")]
        rows[cells[0]] = cells[2]
    return rows


def _save_root(main):
    return main / "var" / "agentops" / "worktree-save"


def _head_sha(save_folder):
    for line in (save_folder / "head.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("head: "):
            return line[len("head: "):]
    raise AssertionError("no head line")


def test_row1_dry_run_writes_nothing_and_apply_saves_then_removes(gc_env, capsys):
    main, _shared = gc_env
    a, b, c, d = _build(main)
    assert cli_main(["worktree", "gc", "--dirty"]) == 0
    out = capsys.readouterr().out
    assert _dirty_decisions(out) == {"wt-a": "save+remove", "wt-b": "save+remove",
                                     "wt-c": "kept: open issue #11", "wt-d": "save+remove"}, out
    assert "dry run" in out
    assert not (main / "var").exists()
    assert a.exists() and b.exists() and c.exists() and d.exists()

    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    assert not a.exists() and not b.exists() and not d.exists()
    assert c.exists()
    save = _save_root(main)
    assert sorted(os.listdir(save / "wt-a")) == ["commits.bundle", "head.txt"]
    assert sorted(os.listdir(save / "wt-b")) == ["head.txt", "tracked.patch", "untracked"]
    assert os.listdir(save / "wt-b" / "untracked") == ["b-new.txt"]
    assert sorted(os.listdir(save / "wt-d")) == ["head.txt", "untracked"]
    assert os.listdir(save / "wt-d" / "untracked") == ["d-new.txt"]
    assert not (save / "wt-c").exists()


def test_row2_saves_restore_the_commits_the_edit_and_the_file(gc_env, capsys, tmp_path):
    main, _shared = gc_env
    a, b, c, d = _build(main)
    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    save = _save_root(main)

    fresh_a = tmp_path / "fresh-a"
    _git(main, "worktree", "add", "-q", "--detach", str(fresh_a), "main")
    _git(fresh_a, "fetch", "-q", str(save / "wt-a" / "commits.bundle"), "HEAD")
    assert _git(fresh_a, "rev-parse", "FETCH_HEAD") == _head_sha(save / "wt-a")

    fresh_b = tmp_path / "fresh-b"
    _git(main, "worktree", "add", "-q", "--detach", str(fresh_b), "main")
    _git(fresh_b, "apply", str(save / "wt-b" / "tracked.patch"))
    assert (fresh_b / "f.txt").read_bytes() == EDIT_B
    assert (save / "wt-b" / "untracked" / "b-new.txt").read_bytes() == UNTRACKED_B

    assert (save / "wt-d" / "untracked" / "d-new.txt").read_bytes() == UNTRACKED_D
    assert _head_sha(save / "wt-b") == _git(main, "rev-parse", "main")


def test_row3_junctions_are_removed_as_links_and_the_shared_folder_keeps_its_files(gc_env, capsys):
    main, shared = gc_env
    for name in ("keep1.txt", "keep2.txt", "keep3.txt"):
        (shared / name).write_bytes(b"shared " + name.encode())
    a, b, c, d = _build(main, shared)
    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "STOP" not in out and "SKIP" not in out, out
    assert sorted(os.listdir(shared)) == ["keep1.txt", "keep2.txt", "keep3.txt"]
    assert (shared / "keep2.txt").read_bytes() == b"shared keep2.txt"
    assert not a.exists() and not b.exists() and not d.exists()
    assert c.exists() and os.path.isjunction(c / "var")


def test_row4_a_failed_bundle_verify_keeps_its_worktree_and_the_others_go_on(gc_env, capsys, monkeypatch):
    main, _shared = gc_env
    a, b, c, d = _build(main)
    real_git = wtmod.git

    def failing_verify(*args, cwd=wtmod.ROOT):
        if args[:2] == ("bundle", "verify"):
            return subprocess.CompletedProcess(args, 1, "", "forced failure")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(wtmod, "git", failing_verify)
    assert cli_main(["worktree", "gc", "--dirty", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "SKIP: wt-a: save failed (bundle)" in out, out
    assert a.exists() and c.exists()
    assert not b.exists() and not d.exists()


def test_an_issue_gh_cannot_tell_keeps_its_worktree(gc_env, capsys, monkeypatch):
    main, _shared = gc_env
    a, b, c, d = _build(main)
    monkeypatch.setattr(wtmod, "gh_issue_state", lambda number, main: "")
    assert cli_main(["worktree", "gc", "--dirty"]) == 0
    assert _dirty_decisions(capsys.readouterr().out) == {
        "wt-a": "kept: issue #7 state unknown", "wt-b": "kept: issue #9 state unknown",
        "wt-c": "kept: issue #11 state unknown", "wt-d": "save+remove"}
