"""#351: the holder's line walk and the next-holder check claim a Priority merge queued behind a commit that needs
a suite; an ordinary merge queued behind that commit still stops there, and a commit that needs no suite is passed."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tools.agentops.merge import MergeLock, _append_line_poll_log
from tools.agentops.merge_priority import batch_for

_PRIO = {"class": "priority", "marked": "2026-10-17T12:10:00Z", "hard": False}
_SUITE_COMMIT = {"kind": "commit", "needs_suite": True}
_FREE_COMMIT = {"kind": "commit", "needs_suite": False}


def _holder(tmp_path: Path) -> MergeLock:
    lock = MergeLock(tmp_path / "merge.lock")
    assert lock.acquire(max_age_seconds=60) is None
    return lock


def _queue_dir(lock: MergeLock) -> Path:
    return lock.path.with_name(lock.path.name + ".queue")


def _put(lock: MergeLock, seq: int, info: dict) -> str:
    _queue_dir(lock).mkdir(parents=True, exist_ok=True)
    name = f"{seq:020d}-{os.getpid()}-{seq}"  # this process's pid: alive for the whole test
    (_queue_dir(lock) / name).write_text(json.dumps(info), encoding="utf-8")
    return name


def _merge(branch: str, priority: dict | None = None) -> dict:
    return {"kind": "merge", "branch": branch, "priority": priority}


def _line(lock: MergeLock) -> list[str]:
    return [w.info["branch"] for w in lock.waiting_line()]


def test_priority_merge_behind_a_suite_commit_is_claimed_and_the_commit_waits(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        commit = _put(lock, 1, _SUITE_COMMIT)
        _put(lock, 2, _merge("P1", _PRIO))
        assert _line(lock) == ["P1"]
        claimed = [w.info["branch"] for w in lock.claim(batch_for(_PRIO, lock.waiting_line()))]
        assert claimed == ["P1"]
        assert (_queue_dir(lock) / commit).exists()
        assert not (lock.path.with_name(lock.path.name + ".batch") / f"{commit}.claim").exists()
    finally:
        lock.release()


def test_is_head_gives_the_line_to_a_priority_merge_behind_a_suite_commit(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        commit = _put(lock, 1, _SUITE_COMMIT)
        p1 = _put(lock, 2, _merge("P1", _PRIO))
        assert lock._is_head(_queue_dir(lock) / p1)
        assert not lock._is_head(_queue_dir(lock) / commit)
    finally:
        lock.release()


def test_is_head_keeps_a_suite_commit_ahead_of_a_free_commit_queued_behind_it(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        commit = _put(lock, 1, _SUITE_COMMIT)
        free = _put(lock, 2, _FREE_COMMIT)
        assert lock._is_head(_queue_dir(lock) / commit)
        assert not lock._is_head(_queue_dir(lock) / free)
    finally:
        lock.release()


def test_ordinary_merge_behind_a_suite_commit_stops_the_walk_there_and_logs_it(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        commit = _put(lock, 1, _SUITE_COMMIT)
        _put(lock, 2, _merge("O1"))
        assert _line(lock) == []
        assert lock.line_stop == f"{commit} kind=commit needs_suite=True"
        log = tmp_path / "merge.log"
        _append_line_poll_log("O1", lock.line_stop, [], 0, log)
        assert log.read_text(encoding="utf-8").endswith(
            f"line poll: 0 claimable; stopped at {commit} kind=commit needs_suite=True; filter dropped 0\n")
    finally:
        lock.release()


def test_priority_merge_passes_a_suite_commit_and_an_ordinary_merge_queued_with_it_is_not_claimed(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock, 1, _SUITE_COMMIT)
        _put(lock, 2, _merge("O1"))
        _put(lock, 3, _merge("P1", _PRIO))
        assert _line(lock) == ["P1"]
    finally:
        lock.release()


def test_no_stop_is_logged_at_a_suite_commit_once_a_priority_merge_is_claimed_past_it(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock, 1, _SUITE_COMMIT)
        _put(lock, 2, _merge("O1"))
        _put(lock, 3, _merge("P1", _PRIO))
        assert _line(lock) == ["P1"]
        assert lock.line_stop is None
    finally:
        lock.release()


def test_a_free_commit_is_passed_over_and_ordinary_merges_behind_it_join_the_line(tmp_path: Path):
    lock = _holder(tmp_path)
    try:
        _put(lock, 1, _FREE_COMMIT)
        _put(lock, 2, _merge("O1"))
        _put(lock, 3, _merge("P1", _PRIO))
        assert _line(lock) == ["P1", "O1"]
        assert lock.line_stop is None
    finally:
        lock.release()
