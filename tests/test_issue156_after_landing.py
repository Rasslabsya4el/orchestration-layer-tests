"""#156 rule 2: a merge whose branch is on main is reported as landed whatever happens after the landed result.

Seams: ``merge.run_merge_command`` (rc and the one ``after-landing step failed:`` line) and ``detach.run_and_record``
(``pushed_sha`` and reason of the detached result). ``run_merge`` is faked to a landed result; the step after it
(``target_check.note_lines``) raises."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.agentops import detach, merge as merge_module, target_check
from tools.agentops.merge import MergeResult

PREFIX = "after-landing step failed:"


def _landed() -> MergeResult:
    return MergeResult(ok=True, reason="", message="b -> main (batch of 2 with a)", head_after="abcdef1234567890")


def _args() -> argparse.Namespace:
    return argparse.Namespace(branch="b", cwd=Path("."), suite=None, lock=None, timeout=None,
                              queue_checked=True, issue=[])


def _fake_world(monkeypatch, result: MergeResult) -> None:
    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda s: "true")
    monkeypatch.setattr(merge_module, "resolve_suite_env", lambda: {})
    monkeypatch.setattr(merge_module, "resolve_suite_shared", lambda: ())
    monkeypatch.setattr(merge_module, "run_merge", lambda *a, **kw: result)


def _boom(_notes):
    raise ImportError("cannot import name 'CoreUpdateRateLimited'")


def test_row2_a_step_after_a_landed_result_raises_rc_0_and_one_line(monkeypatch, capsys):
    _fake_world(monkeypatch, _landed())
    monkeypatch.setattr(target_check, "note_lines", _boom)

    rc = merge_module.run_merge_command(_args())

    out = capsys.readouterr().out
    assert rc == 0
    assert out.count(PREFIX) == 1
    assert f"{PREFIX} ImportError: cannot import name 'CoreUpdateRateLimited'" in out
    assert out.startswith("merged: b -> main")


def test_row2_the_selection_step_raising_is_caught_too(monkeypatch, capsys):
    landed = _landed()
    landed.selection = "selected 3 of 9"
    _fake_world(monkeypatch, landed)

    def bad_stats(*a, **kw):
        raise KeyError("lock_at")
    monkeypatch.setattr(merge_module, "_stats_line", bad_stats)

    assert merge_module.run_merge_command(_args()) == 0
    assert capsys.readouterr().out.count(PREFIX) == 1


def test_a_not_landed_result_is_not_masked(monkeypatch):
    _fake_world(monkeypatch, MergeResult(ok=False, reason="red-suite", message="x"))
    monkeypatch.setattr(target_check, "note_lines", _boom)
    with pytest.raises(ImportError):
        merge_module.run_merge_command(_args())


def test_row3_the_detached_result_of_a_landed_run_keeps_pushed_sha(tmp_path: Path):
    def git(cwd, *a):
        return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                              capture_output=True, text=True, check=True).stdout.strip()

    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git"))
    home = tmp_path / "home"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(home))
    (home / "f.txt").write_text("x", encoding="utf-8")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "c")
    git(home, "push", "-q", "origin", "main")
    head = git(home, "rev-parse", "origin/main")

    script = textwrap.dedent("""
        import argparse, sys
        from pathlib import Path
        from tools.agentops import merge, target_check
        from tools.agentops.merge import MergeResult
        merge.worktree_refusal = lambda cwd: None
        merge.resolve_suite_cmd = lambda s: "true"
        merge.resolve_suite_env = lambda: {}
        merge.resolve_suite_shared = lambda: ()
        merge.run_merge = lambda *a, **kw: MergeResult(ok=True, reason="", message="b -> main", head_after="abc")
        def boom(notes):
            raise ImportError("cannot import name 'X'")
        target_check.note_lines = boom
        ns = argparse.Namespace(branch="b", cwd=Path("."), suite=None, lock=None, timeout=None,
                                queue_checked=True, issue=[])
        sys.exit(merge.run_merge_command(ns))
    """)
    result_file = tmp_path / "results" / "r.json"
    rc = detach.run_and_record([sys.executable, "-c", script, "--cwd", str(home)], result_file, "merge", "b",
                               merge_log=tmp_path / "merge.log")

    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert rc == 0, record["output"]
    assert record["pushed_sha"] == head
    assert record["reason"] != "failed"
    assert PREFIX in record["output"]
