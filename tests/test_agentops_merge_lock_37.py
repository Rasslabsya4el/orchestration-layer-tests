"""#37: the merge lock's record names its holder: a commit by its message and ticket, a merge by its branch.

Real acquire and release on a tmp lock file; no suite, no heavy tool."""
from __future__ import annotations

import json
from pathlib import Path

from tools.agentops import merge
from tools.agentops.merge import MergeLock

MSG = "#29: third repo joins the home's targets (3 entries)\n\nbody line cites #99"


def _record(lock_path: Path) -> dict:
    return json.loads(lock_path.read_text(encoding="utf-8"))


def test_row1_message_issues_are_the_ticket_numbers_of_the_first_line_only():
    assert merge.message_issues(MSG) == [29]
    assert merge.message_issues("fix #5 and #6\n#7 is later") == [5, 6]
    assert merge.message_issues("no ticket here") == []


def test_row1_commit_acquire_writes_kind_message_and_issues_into_the_record(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    info = {"kind": "commit", "needs_suite": True, "message": MSG, "issues": merge.message_issues(MSG)}
    lock = MergeLock(lock_path)
    assert lock.acquire(ticket_info=info) is None
    try:
        rec = _record(lock_path)
        assert rec["kind"] == "commit"
        assert rec["message"] == MSG
        assert rec["issues"] == [29]
        assert rec["pid"] and rec["started"] and rec["token"] == lock.token
    finally:
        lock.release()


def test_row1_merge_acquire_writes_kind_and_branch_into_the_record(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    lock = MergeLock(lock_path)
    assert lock.acquire(ticket_info={"kind": "merge", "branch": "orch-37"}) is None
    try:
        rec = _record(lock_path)
        assert rec["kind"] == "merge" and rec["branch"] == "orch-37"
        assert "message" not in rec
    finally:
        lock.release()


def test_row1_heartbeat_keeps_the_kind_and_message_of_the_record(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    lock = MergeLock(lock_path)
    lock.acquire(ticket_info={"kind": "commit", "needs_suite": True, "message": MSG, "issues": [29]})
    try:
        lock.heartbeat()
        rec = _record(lock_path)
        assert rec["kind"] == "commit" and rec["message"] == MSG and rec["issues"] == [29]
    finally:
        lock.release()


def test_row1_acquire_without_ticket_info_keeps_the_old_record_shape(tmp_path: Path):
    lock_path = tmp_path / "merge.lock"
    lock = MergeLock(lock_path)
    assert lock.acquire() is None
    try:
        assert set(_record(lock_path)) == {"pid", "started", "token"}
    finally:
        lock.release()
