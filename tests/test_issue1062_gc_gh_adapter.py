"""#1062: the worktree cleanup reads a ticket's state through the shared gh adapter (`run_gh`)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.agentops import worktree


def _fake(monkeypatch, rc: int, out: str, seen: list):
    def fake_run_gh(args, *, cwd=None, timeout=None, **kw):
        seen.append((list(args), cwd))
        return subprocess.CompletedProcess(["gh", *args], rc, out, "")
    monkeypatch.setattr(worktree, "run_gh", fake_run_gh)


@pytest.mark.parametrize("rc,out,want", [
    (0, '{"state": "OPEN"}', "OPEN"),
    (0, '{"state": "closed"}', "CLOSED"),
    (1, "", ""),
    (0, "not json", ""),
])
def test_gh_issue_state_through_adapter(monkeypatch, rc, out, want):
    seen: list = []
    _fake(monkeypatch, rc, out, seen)
    assert worktree.gh_issue_state(7, Path("main-checkout")) == want
    assert seen[0][0] == ["issue", "view", "7", "--json", "state"]
