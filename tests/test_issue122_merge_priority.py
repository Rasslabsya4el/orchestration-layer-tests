"""#122 unit 3: Priority tickets head the merge line and run in a batch of their own.

Acceptance rows 2 (soft line), 3 (two priorities) and 5 (failed batch) through the pure functions of
``merge_priority`` on a simulated clock in minutes (the suite is a fake that runs the given minutes), plus
the real queue-file path (``MergeLock._is_head`` / ``waiting_line``) in a tmp dir."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.agentops import merge, merge_priority
from tools.agentops.merge import MergeLock, Waiter
from tools.agentops.merge_priority import batch_for, order_waiters, priority_of

F_MARK = "2026-10-17T11:30:00Z"
P_MARK = "2026-10-17T12:10:00Z"


def _flow(marked: str = F_MARK, hard: bool = False) -> dict:
    return {"class": "flow-fix", "marked": marked, "hard": hard}


def _prio(marked: str = P_MARK, hard: bool = False) -> dict:
    return {"class": "priority", "marked": marked, "hard": hard}


def _w(name: str, priority: dict | None = None, n: int = 0) -> Waiter:
    return Waiter(f"{n:020d}-1-{n}", 1, {"kind": "merge", "branch": name, "priority": priority})


def _name(w: Waiter) -> str:
    return w.info["branch"]


# -- priority_of ---------------------------------------------------------------------------------

def test_priority_of_ordinary_and_unreadable_are_none():
    assert priority_of([]) is None
    assert priority_of([{}]) is None
    assert priority_of([{"ready-for-agent": "2026-10-17T10:00:00Z"}]) is None


def test_priority_of_class_mark_and_hard():
    assert priority_of([{"flow-fix": F_MARK}]) == _flow()
    assert priority_of([{"priority": P_MARK, "hard-push": "2026-10-17T13:00:00Z"}]) == _prio(hard=True)


def test_priority_of_both_labels_ranks_as_priority_with_that_labels_time():
    assert priority_of([{"flow-fix": F_MARK, "priority": P_MARK}]) == _prio()


def test_priority_of_highest_ranked_issue_of_the_branch_counts():
    older_flow = {"flow-fix": "2026-10-17T07:00:00Z"}
    newer_flow = {"flow-fix": F_MARK}
    assert priority_of([newer_flow, older_flow])["marked"] == "2026-10-17T07:00:00Z"
    assert priority_of([older_flow, {"priority": P_MARK}]) == _prio()  # priority before flow-fix, older or not
    assert priority_of([{}, newer_flow]) == _flow()


def test_marks_from_timeline_newest_labeled_event_of_a_label_still_on():
    timeline = [
        {"event": "labeled", "label": {"name": "flow-fix"}, "created_at": "2026-10-17T07:00:00Z"},
        {"event": "unlabeled", "label": {"name": "flow-fix"}, "created_at": "2026-10-17T08:00:00Z"},
        {"event": "labeled", "label": {"name": "flow-fix"}, "created_at": F_MARK},
        {"event": "labeled", "label": {"name": "priority"}, "created_at": P_MARK},
        {"event": "unlabeled", "label": {"name": "priority"}, "created_at": "2026-10-17T12:20:00Z"},
        {"event": "labeled", "label": {"name": "bug"}, "created_at": "2026-10-17T09:00:00Z"},
        {"event": "commented"},
    ]
    assert merge_priority.marks_from_timeline(timeline) == {"flow-fix": F_MARK}


def test_priority_for_issues_reader_failure_is_ordinary_and_read_is_per_issue():
    seen: list[int] = []

    def reader(issue: int):
        seen.append(issue)
        if issue == 7:
            raise RuntimeError("gh down")
        return {"flow-fix": F_MARK}

    assert merge_priority.priority_for_issues([7], reader) is None
    assert merge_priority.priority_for_issues([5, 6], reader) == _flow()
    assert merge_priority.priority_for_issues([], reader) is None
    assert seen == [7, 5, 6]


def test_priority_for_issues_logs_each_failed_read_once():
    logged: list[tuple[int, str]] = []

    def reader(issue: int):
        raise RuntimeError("gh down")

    assert merge_priority.priority_for_issues([7, 8], reader, log=lambda n, exc: logged.append((n, str(exc)))) is None
    assert logged == [(7, "gh down"), (8, "gh down")]


def test_gh_marks_reads_the_timeline_through_core_table_and_joins_paginated_arrays():
    page1 = json.dumps([{"event": "labeled", "label": {"name": "flow-fix"}, "created_at": "2026-10-17T07:00:00Z"},
                        {"event": "commented"}])
    page2 = json.dumps([{"event": "unlabeled", "label": {"name": "flow-fix"}, "created_at": "2026-10-17T08:00:00Z"},
                        {"event": "labeled", "label": {"name": "flow-fix"}, "created_at": F_MARK}])
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        return page1 + "\n" + page2 + "\n"  # `gh api --paginate`: one array per page, concatenated

    assert merge_priority.gh_marks(122, runner=runner, repo="o/r") == {"flow-fix": F_MARK}
    assert calls == [["api", "repos/o/r/issues/122/timeline", "--paginate"]]
    assert merge_priority.join_pages("") == "[]"


# -- ordering and batching -----------------------------------------------------------------------

def test_order_waiters_priority_by_rank_then_the_rest_in_arrival_order():
    line = [_w("O1", None, 1), _w("F2", _flow("2026-10-17T12:00:00Z"), 2), _w("O2", None, 3),
            _w("F1", _flow(), 4), _w("P1", _prio(), 5)]
    assert [_name(w) for w in order_waiters(line)] == ["P1", "F1", "F2", "O1", "O2"]


def test_batch_for_priority_holder_takes_only_priority_ordinary_holder_only_ordinary():
    ordered = [_w("P1", _prio(), 1), _w("F1", _flow(), 2), _w("O1", None, 3)]
    assert [_name(w) for w in batch_for(_flow(), ordered)] == ["P1", "F1"]
    assert [_name(w) for w in batch_for(None, ordered)] == ["O1"]


# -- simulated clock -----------------------------------------------------------------------------

SUITE = 20


def _simulate(first_batch_end: int, arrivals: list[tuple[int, str, dict | None]]):
    """The line after a running batch ends at ``first_batch_end``: each turn the next holder is the first of
    ``order_waiters``, its batch is ``batch_for`` it, and the suite takes SUITE minutes. Returns
    (batches as [(start, [names])], wait of each name = batch start - arrival)."""
    pending = [(t, _w(name, pr, i)) for i, (t, name, pr) in enumerate(arrivals)]
    clock = first_batch_end
    batches: list[tuple[int, list[str]]] = []
    waits: dict[str, int] = {}
    while pending:
        here = [w for t, w in pending if t <= clock]
        assert here, "an idle gap is not part of these rows"
        ordered = order_waiters(here)
        holder, rest = ordered[0], ordered[1:]
        batch = [holder] + batch_for(holder.info["priority"], rest)
        batches.append((clock, [_name(w) for w in batch]))
        for t, w in pending:
            if w in batch:
                waits[_name(w)] = clock - t
        pending = [(t, w) for t, w in pending if w not in batch]
        clock += SUITE
    return batches, waits


def test_row2_soft_line():
    # batch [O1, O2] runs from t=0 (suite 20); O3 queues t=5, F1 t=6, O4 t=7
    batches, waits = _simulate(20, [(5, "O3", None), (6, "F1", _flow()), (7, "O4", None)])
    assert batches == [(20, ["F1"]), (40, ["O3", "O4"])]
    assert waits["F1"] == 14  # the running batch's time left at t=6


def test_row3_two_priorities():
    batches, _ = _simulate(20, [(3, "F1", _flow()), (4, "O3", None), (5, "P1", _prio())])
    assert batches == [(20, ["P1", "F1"]), (40, ["O3"])]


# -- the real queue files ------------------------------------------------------------------------

def _queue(lock: MergeLock, n: int, branch: str, priority: dict | None, kind: str = "merge") -> Path:
    qdir = lock.path.with_name(lock.path.name + ".queue")
    qdir.mkdir(parents=True, exist_ok=True)
    t = qdir / f"{n:020d}-{os.getpid()}-{n}"
    info = {"kind": kind, "branch": branch, "suite": "s", "remote": "origin", "main": "main", "cwd": "c",
            "issues": [], "result": "", "priority": priority}
    t.write_text(json.dumps(info), encoding="utf-8")
    return t


def test_queue_files_waiting_line_follows_rule_3_order(tmp_path):
    lock = MergeLock(tmp_path / "merge.lock")
    lock.acquire(max_age_seconds=60)
    try:
        _queue(lock, 1, "O1", None)
        _queue(lock, 2, "F1", _flow())
        _queue(lock, 3, "O2", None)
        _queue(lock, 4, "P1", _prio())
        _queue(lock, 5, "FOLD", None)  # a ticket from before this change has no "priority" key at all
        line = lock.waiting_line()
        assert [_name(w) for w in line] == ["P1", "F1", "O1", "O2", "FOLD"]
        assert [_name(w) for w in batch_for(_prio(), line)] == ["P1", "F1"]
    finally:
        lock.release()


def test_queue_files_next_holder_is_the_first_of_the_order(tmp_path):
    lock = MergeLock(tmp_path / "merge.lock")
    o1 = _queue(lock, 1, "O1", None)
    f1 = _queue(lock, 2, "F1", _flow())
    p1 = _queue(lock, 3, "P1", _prio())
    lock._queue_dir = o1.parent
    assert lock._is_head(p1) and not lock._is_head(f1) and not lock._is_head(o1)
    p1.unlink()
    assert lock._is_head(f1) and not lock._is_head(o1)
    f1.unlink()
    assert lock._is_head(o1)


def test_queue_files_without_priority_stay_in_arrival_order(tmp_path):
    lock = MergeLock(tmp_path / "merge.lock")
    a = _queue(lock, 1, "A", None)
    b = _queue(lock, 2, "B", None)
    lock._queue_dir = a.parent
    assert lock._is_head(a) and not lock._is_head(b)


def test_ticket_info_of_a_merge_carries_priority(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_acquire(lock_path, timeout, ticket_info, hook):
        seen.update(ticket_info)
        raise RuntimeError("stop after the queue entry is built")

    monkeypatch.setattr(merge, "acquire_merge_line", fake_acquire)
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {"flow-fix": F_MARK})
    with pytest.raises(RuntimeError, match="stop after"):
        merge._run_merge_locked("b", "true", cwd=tmp_path, lock_path=tmp_path / "l", remote="origin",
                                main="main", lock_timeout=None, max_rebase_attempts=1, suite_env=None,
                                suite_output_dir=tmp_path / "out", merge_log_path=tmp_path / "log", issues=(122,))
    assert seen["priority"] == _flow()


def test_a_priority_merge_jumps_a_suite_commit_and_an_ordinary_merge_does_not(tmp_path):
    """A commit that needs a suite holds back ordinary merges only: `_is_head` gives the line to the Priority
    merge F1 and keeps the commit waiting; once F1 is gone, the commit heads the line over the ordinary O1."""
    lock = MergeLock(tmp_path / "merge.lock")
    c = _queue(lock, 1, "C", None, kind="commit")
    c.write_text(json.dumps({"kind": "commit", "needs_suite": True}), encoding="utf-8")
    f1 = _queue(lock, 2, "F1", _flow())
    lock._queue_dir = c.parent
    assert lock._is_head(f1) and not lock._is_head(c)
    f1.unlink()
    o1 = _queue(lock, 3, "O1", None)
    assert lock._is_head(c) and not lock._is_head(o1)


def test_a_code_free_commit_does_not_hold_back_a_priority_merge_in_is_head(tmp_path):
    lock = MergeLock(tmp_path / "merge.lock")
    c = _queue(lock, 1, "C", None, kind="commit")
    c.write_text(json.dumps({"kind": "commit", "needs_suite": False}), encoding="utf-8")
    f1 = _queue(lock, 2, "F1", _flow())
    lock._queue_dir = c.parent
    assert lock._is_head(f1) and not lock._is_head(c)


# -- row 5 on the real red-batch path ------------------------------------------------------------

def test_row5_real_red_batch_priority_holder_is_the_culprit(tmp_path, monkeypatch):
    """Batch [P1, F1] (O3 waits): the suite is red and triage names P1. P1's run ends `red-suite`; F1 keeps its
    queue file with `priority` intact and heads the re-run alone (O3 is not in its batch); P1 queued again heads
    the line with the same mark."""
    from tests.test_issue95_target_check_eject import _branch, _world, SUITE
    from tools.agentops import core_targets
    from tools.agentops.merge import SuiteRun

    for name, value in (("SUITE_OUTPUT_DIR", tmp_path / "suite_out"), ("MERGE_LOG_PATH", tmp_path / "merge.log")):
        monkeypatch.setattr(merge, name, value)
    monkeypatch.setenv(merge.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {"priority": P_MARK})
    bare, checkout, lockfile = _world(tmp_path)
    clean = "# core\nplain\nmore\n"
    p1, f1_ref, o3_ref = (_branch(tmp_path, bare, n, clean) for n in ("f1", "f2", "f3"))
    red_id = "tests/test_x.py::test_boom"
    monkeypatch.setattr(merge, "_run_merge_suite", lambda *a, **k: SuiteRun(
        False, f"FAILED {red_id} - boom\n", 1, rechecked_done=True, red_ids=[red_id], still=[red_id]))
    monkeypatch.setattr(merge, "_triage_red_batch", lambda *a, **k: ([], {p1: [red_id]}))

    real_line = MergeLock.waiting_line
    queued: dict[str, Path] = {}

    def line_with_waiters(self):  # the other two merges are queued (real files) when the holder polls the line
        base = {"kind": "merge", "suite": SUITE, "remote": merge.DEFAULT_REMOTE, "main": merge.DEFAULT_MAIN,
                "cwd": str(checkout.resolve()), "issues": [], "result": ""}
        for n, (ref, pr) in enumerate(((o3_ref, None), (f1_ref, _flow())), start=1):
            qdir = self.path.with_name(self.path.name + ".queue")
            t = qdir / f"{9_000_000_000_000_000_000 + n:020d}-{os.getpid()}-{n}"
            t.write_text(json.dumps({**base, "branch": ref, "priority": pr}), encoding="utf-8")
            queued[ref] = t
        MergeLock.waiting_line = real_line
        return real_line(self)

    monkeypatch.setattr(MergeLock, "waiting_line", line_with_waiters)
    result = merge.run_merge(p1, SUITE, cwd=checkout, lock_path=lockfile, lock_timeout=60, issues=(1,))

    assert not result.ok and result.reason == "red-suite" and red_id in result.message
    assert not result.batched  # P1 is the holder: its own result, back to ORCH
    log = (tmp_path / "merge.log").read_text(encoding="utf-8")
    assert f"batch of 2: {p1}, {f1_ref} | red-suite" in log and o3_ref not in log.split("red-suite")[0]  # O3 not in it
    f1_file, o3_file = queued[f1_ref], queued[o3_ref]
    assert json.loads(f1_file.read_text(encoding="utf-8"))["priority"] == _flow()  # the innocent keeps its mark
    assert o3_file.exists()
    batch_dir = lockfile.with_name(lockfile.name + ".batch")
    assert not list(batch_dir.glob(f"{f1_file.name}.claim")) and not list(batch_dir.glob(f"{o3_file.name}.claim"))
    assert not list(batch_dir.glob(f"{f1_file.name}.result"))  # nothing posted: F1 goes again, it was not ejected
    lock = MergeLock(lockfile)
    lock._queue_dir = f1_file.parent
    assert lock._is_head(f1_file) and not lock._is_head(o3_file)  # F1 heads the re-run
    waiting = [Waiter(t.name, 1, json.loads(t.read_text(encoding="utf-8"))) for t in (o3_file, f1_file)]
    ordered = order_waiters(waiting)
    assert [w.info["branch"] for w in ordered] == [f1_ref, o3_ref]
    assert batch_for(ordered[0].info["priority"], ordered[1:]) == []  # alone
    # P1 comes back, queues again: the same label mark gives the same entry, and it heads the line
    again = merge_priority.priority_for_issues([1])
    assert again == {"class": "priority", "marked": P_MARK, "hard": False}
    p1_file = _queue(lock, 9, p1, again)
    assert lock._is_head(p1_file) and not lock._is_head(f1_file)
