"""#145 unit 1: a detached merge with no `--issue` is refused at once, before anything is queued.

Seam: ``cli.main(["merge", <branch>, "--detach"])`` and ``cli.main(["commit", ...,"--detach"])``. The detached
launcher is replaced by a recorder (as in test_issue110_queue_target_check.py), so no process starts."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import cli, detach, merge as merge_module


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Recorders: detached launches, and the results / merge log the refusal must leave untouched."""
    launched = []
    results = tmp_path / "results"
    monkeypatch.setattr(detach, "launch", lambda argv, kind, branch: launched.append(argv) or (results / "r.json", 1))
    log = tmp_path / "merge.log"
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", log)
    return launched, results, log


def _no_trace(results: Path, log: Path):
    assert not results.exists() or list(results.iterdir()) == []
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_detached_merge_without_issue_is_refused_before_anything_is_queued(queue, capsys):
    launched, results, log = queue
    rc = cli.main(["merge", "somebranch", "--detach"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert len(out) == 1 and "--issue" in out[0]
    assert launched == []
    _no_trace(results, log)


def test_detached_merge_with_issue_is_queued_as_before(queue, monkeypatch, capsys):
    launched, results, log = queue
    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)  # the git checks are not under test here
    monkeypatch.setattr(merge_module, "queue_target_refusals", lambda branch, *, cwd: None)  # None = not checked
    rc = cli.main(["merge", "somebranch", "--detach", "--issue", "5"])
    assert rc == 0, capsys.readouterr().out
    assert len(launched) == 1
    assert "--issue" in launched[0] and "5" in launched[0]
    assert capsys.readouterr().out.startswith("detached: result ")


def test_detached_commit_without_issue_is_not_refused(queue, capsys):
    launched, results, log = queue
    rc = cli.main(["commit", "-m", "State: x", "docs/STATE.md", "--detach"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "--issue" not in out
    assert len(launched) == 1
