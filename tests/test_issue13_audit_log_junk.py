"""#13 finding 4: a `.log` file is junk for the dirty-worktree audit (scripts/audit_dirty_worktrees.py), the same
as the other junk classes. A worktree whose only changed files are logs is clean; a real source file beside a log is dirt."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_dirty_worktrees as adw  # noqa: E402


def _audit(monkeypatch, status_out: str) -> adw.Row:
    """audit_one with git stubbed: `status` returns the given listing, the other read-only calls are fixed."""

    def fake_git(args, cwd, timeout=60.0):
        if args[0] == "log":
            return "2026-10-16T10:00:00+03:00\n", "ok"
        if args[0] == "rev-list":
            return "0\n", "ok"
        if args[0] == "ls-files":
            return "", "ok"
        if args[0] == "status":
            return status_out, "ok"
        raise AssertionError(f"unexpected git call {args}")

    monkeypatch.setattr(adw, "git", fake_git)
    return adw.audit_one("wt-x", "x-branch", ROOT, {}, 5.0)


def test_log_files_are_junk_not_dirt(monkeypatch):
    only_logs = " M logs/run.log\n?? scripts/out.log\n?? out/trace.log\n"
    row = _audit(monkeypatch, only_logs)
    assert row.dirty == 0
    assert adw.classify(row) == "K1"

    log_and_source = "?? scripts/out.log\n?? scripts/new_tool.py\n"
    row = _audit(monkeypatch, log_and_source)
    assert row.dirty == 1
    assert row.plain_examples == "scripts/new_tool.py"
