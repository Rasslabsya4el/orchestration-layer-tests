"""#454 unit 2: `agentops commit` refuses a closing keyword before a ticket reference in its -m before anything is
written (no lock wait, no git add, no git commit), and --detach shows the refusal at once."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import merge
from tools.agentops.merge import run_commit_command
from tools.agentops.util import no_window_flags

ROOT = Path(__file__).resolve().parents[1]
BASE = "# State\n\nUpdated: 2026-10-01 00:00 old\n\nPadding: old rule\n\nLast review: r0\n"


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          check=True).stdout.strip()


def _world(tmp_path):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    co = tmp_path / "co"
    _git(tmp_path, "clone", "-q", str(bare), str(co))
    _git(co, "config", "user.email", "t@t.invalid")
    _git(co, "config", "user.name", "t")
    (co / "docs").mkdir()
    (co / "docs" / "STATE.md").write_text(BASE, encoding="utf-8")
    (co / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(co, "add", ".")
    _git(co, "commit", "-q", "-m", "init")
    _git(co, "push", "-q", "origin", "HEAD:main")
    _git(co, "fetch", "-q", "origin")
    _git(co, "branch", "-q", "--set-upstream-to=origin/main")
    return co


def _args(co, tmp_path, paths, message, **kw):
    args = argparse.Namespace(paths=paths, message=message, cwd=co, lock=tmp_path / "lock", timeout=None,
                              state_entry=None, state_as="", review_entry=None)
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def _staged(co):
    return _git(co, "diff", "--cached", "--name-only")


# (a) the state-entry form: refused before the state entry is even read
def test_state_entry_with_closing_keyword_is_refused(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "STATE.md").write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    args = _args(co, tmp_path, ["docs/STATE.md"], "State: test closed #425 and #424",
                 state_entry="x", state_as="test")
    rc = run_commit_command(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "closed" in captured.err and "#425" in captured.err
    assert "closing-check" in captured.err
    assert _git(co, "rev-parse", "HEAD") == head
    assert "new rule" in (co / "docs" / "STATE.md").read_text(encoding="utf-8")
    assert "new rule" not in _git(co, "show", ":docs/STATE.md")  # index still holds the committed text
    assert _staged(co) == ""


# (b) the review-entry form and the plain form: each refused, nothing committed
def test_review_entry_with_closing_keyword_is_refused(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "STATE.md").write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    rc = run_commit_command(_args(co, tmp_path, ["docs/STATE.md"], "Review: resolves #9",
                                  review_entry="y"))
    assert rc == 1
    assert "resolves" in capsys.readouterr().err
    assert _git(co, "rev-parse", "HEAD") == head
    assert _staged(co) == ""


def test_plain_commit_with_closing_keyword_is_refused(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    rc = run_commit_command(_args(co, tmp_path, ["docs/notes.md"], "fix #3"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "closing-check" in err and "#3" in err
    assert _git(co, "rev-parse", "HEAD") == head
    assert _staged(co) == ""


# (c) messages with no closing keyword before a reference get past the check
@pytest.mark.parametrize("message", ["finished #425", "#9 done", "for #3"])
def test_clean_message_passes_the_check(tmp_path, monkeypatch, capsys, message):
    co = _world(tmp_path)
    (co / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")
    # the sentinel stops the commit right after the check, before any push: a result the command renders
    monkeypatch.setattr(merge, "_sync_up", lambda cwd, remote, main: (False, "sentinel", "SENTINEL-REACHED"))
    rc = run_commit_command(_args(co, tmp_path, ["docs/notes.md"], message))
    captured = capsys.readouterr()
    assert "SENTINEL-REACHED" in captured.out
    assert "closing-check" not in captured.err
    assert rc == 1


# (d) --detach: the refusal is printed by the foreground process, which starts no detached child
def test_detach_refusal_is_visible_at_once(tmp_path):
    co = _world(tmp_path)
    (co / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "tools.agentops", "commit", "-m", "fix #3", "docs/notes.md",
                           "--detach", "--cwd", str(co), "--lock", str(tmp_path / "lock")],
                          capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120,
                          creationflags=no_window_flags())
    assert proc.returncode == 1
    assert "closing-check" in proc.stderr
    assert "detached:" not in proc.stdout
    assert _git(co, "rev-parse", "HEAD") == head
    assert _staged(co) == ""


# Row 2 of #454: a clean message commits as today, in each form. docs/*.md paths are suite-free, so no suite
# runs; the push goes to the tmp bare remote. A real commit moves HEAD, its subject is the -m, and origin/main
# follows it.
def test_clean_state_entry_commits(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "STATE.md").write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    rc = run_commit_command(_args(co, tmp_path, ["docs/STATE.md"], "finished #425",
                                  state_entry="2026-10-11 02:00 mine", state_as="w"))
    assert rc == 0, capsys.readouterr()
    assert "closing-check" not in capsys.readouterr().err
    assert _git(co, "rev-parse", "HEAD") != head
    assert _git(co, "log", "-1", "--format=%s") == "finished #425"
    assert _git(co, "rev-parse", "origin/main") == _git(co, "rev-parse", "HEAD")


def test_clean_review_entry_commits(tmp_path, capsys):
    co = _world(tmp_path)
    head = _git(co, "rev-parse", "HEAD")
    rc = run_commit_command(_args(co, tmp_path, ["docs/STATE.md"], "#9 done",
                                  review_entry="2026-10-11 02:00 r1"))
    assert rc == 0, capsys.readouterr()
    assert "closing-check" not in capsys.readouterr().err
    assert _git(co, "rev-parse", "HEAD") != head
    assert _git(co, "log", "-1", "--format=%s") == "#9 done"
    assert _git(co, "rev-parse", "origin/main") == _git(co, "rev-parse", "HEAD")


def test_clean_plain_commit_commits(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")
    head = _git(co, "rev-parse", "HEAD")
    rc = run_commit_command(_args(co, tmp_path, ["docs/notes.md"], "for #3"))
    assert rc == 0, capsys.readouterr()
    assert "closing-check" not in capsys.readouterr().err
    assert _git(co, "rev-parse", "HEAD") != head
    assert _git(co, "log", "-1", "--format=%s") == "for #3"
    assert _git(co, "rev-parse", "origin/main") == _git(co, "rev-parse", "HEAD")
