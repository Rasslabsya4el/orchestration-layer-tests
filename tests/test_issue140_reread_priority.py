"""#140: a label (`priority` / `flow-fix`) given to a merge that already waits moves it ahead at the next batch pick.

The holder re-reads the labels of every waiting merge's issues (`MergeLock.refresh_priorities`) and writes the new
`priority` back into that waiter's queue entry, so `waiting_line`, the waiter-side head check and `merge-status`
all order from the same entry. The label reader is a fake, never `gh`; no heavy tool."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tools.agentops import merge_status as ms
from tools.agentops.merge import MergeLock
from tests.test_issue122_merge_status_priority import _log, _running_batch, _ticket

F_MARK = "2026-10-17T11:30:00Z"
FLOW = {"class": "flow-fix", "marked": F_MARK, "hard": False}
ISSUE = {"A": 11, "B": 12, "C": 13}


def _queue(lock: MergeLock, n: int, branch: str, priority: dict | None = None) -> Path:
    qdir = lock.path.with_name(lock.path.name + ".queue")
    qdir.mkdir(parents=True, exist_ok=True)
    t = qdir / f"{n:020d}-{os.getpid()}-{n}"
    info = {"kind": "merge", "branch": branch, "suite": "s", "remote": "origin", "main": "main", "cwd": "c",
            "issues": [ISSUE[branch]], "result": "", "priority": priority}
    t.write_text(json.dumps(info), encoding="utf-8")
    return t


def _entry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def held(tmp_path):
    lock = MergeLock(tmp_path / "merge.lock")
    lock.acquire(max_age_seconds=60)
    yield lock
    lock.release()


def _order(lock: MergeLock) -> list[str]:
    return [w.info["branch"] for w in lock.waiting_line()]


def test_row1_label_given_while_waiting_moves_the_waiter_first_and_is_written_to_its_entry(held, tmp_path):
    _queue(held, 1, "A")
    _queue(held, 2, "B")
    c = _queue(held, 3, "C")
    assert _order(held) == ["A", "B", "C"]

    held.refresh_priorities(lambda issue: {"flow-fix": F_MARK} if issue == ISSUE["C"] else {},
                            tmp_path / "merge.log")

    assert _order(held) == ["C", "A", "B"]
    assert _entry(c)["priority"]["class"] == "flow-fix"
    assert _entry(c)["priority"] == FLOW
    # the waiter-side head check reads the same entry
    held._queue_dir = c.parent
    assert held._is_head(c)


def test_row2_label_removed_returns_the_waiter_to_arrival_order(held, tmp_path):
    _queue(held, 1, "A")
    _queue(held, 2, "B")
    c = _queue(held, 3, "C", FLOW)
    assert _order(held) == ["C", "A", "B"]

    held.refresh_priorities(lambda issue: {}, tmp_path / "merge.log")

    assert _order(held) == ["A", "B", "C"]
    assert _entry(c)["priority"] is None


def test_row3_failed_read_keeps_the_previous_priority_and_logs_one_plain_line(held, tmp_path):
    _queue(held, 1, "A")
    _queue(held, 2, "B")
    c = _queue(held, 3, "C", FLOW)
    log = tmp_path / "merge.log"

    def reader(issue: int):
        if issue == ISSUE["C"]:
            raise RuntimeError("gh down")
        return {}

    held.refresh_priorities(reader, log)

    assert _entry(c)["priority"] == FLOW
    assert _order(held) == ["C", "A", "B"]
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "priority not read" in ln]
    assert len(lines) == 1 and f"#{ISSUE['C']}" in lines[0] and " | C | " in lines[0]


def test_one_label_read_per_waiting_issue_and_an_unchanged_entry_is_not_rewritten(held, tmp_path):
    a = _queue(held, 1, "A")
    _queue(held, 2, "B")
    seen: list[int] = []
    before = a.stat().st_mtime_ns

    def reader(issue: int):
        seen.append(issue)
        return {}

    held.refresh_priorities(reader, tmp_path / "merge.log")

    assert sorted(seen) == [ISSUE["A"], ISSUE["B"]]
    assert a.stat().st_mtime_ns == before


def test_row4_merge_status_over_the_rewritten_entries_shows_flow_fix_first(tmp_path):
    root = tmp_path
    (root / "var" / "locks" / "merge.lock.queue").mkdir(parents=True)
    (root / "var" / "locks" / "merge.lock.batch").mkdir(parents=True)
    (root / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (root / "var" / "agentops" / "results").mkdir(parents=True)
    _log(root)
    t0 = int(time.time()) - 600
    lock = MergeLock(root / "var" / "locks" / "merge.lock")
    lock.acquire(max_age_seconds=60)
    for n, branch in enumerate(("A", "B", "C"), start=1):
        _ticket(root, int((t0 + 100 * n) * 1e9), n, f"orch-{branch}", [ISSUE[branch]])
    lock.refresh_priorities(lambda issue: {"flow-fix": F_MARK} if issue == ISSUE["C"] else {},
                            root / "var" / "agentops" / "merge.log")
    _running_batch(root, t0, [], percent=50)  # the holder file as merge-status reads a running batch

    snap = ms.collect(root, clock=t0 + 600)
    assert [w["branch"] for w in snap["waiting"]] == ["orch-C", "orch-A", "orch-B"]
    text = ms.render_text(snap)
    first = next(ln for ln in text.splitlines() if ln.startswith("waiting 1"))
    assert "orch-C" in first and ", flow fix" in first


# -- row 5: an open native blocker that waits in the line goes first ----------------------------------------------

def _blockers(table: dict[int, list[int]]):
    return lambda issue: table.get(issue, [])


def _no_labels(issue: int):
    return {}


def _queue_b_then_a(held):
    """Arrival B, then A (both ordinary)."""
    b = _queue(held, 1, "B")
    a = _queue(held, 2, "A")
    return b, a


def test_row5_blocker_waiting_in_the_line_goes_first_for_holder_head_check_and_status(held, tmp_path):
    b, a = _queue_b_then_a(held)
    assert _order(held) == ["B", "A"]

    held.refresh_priorities(_no_labels, tmp_path / "merge.log", _blockers({ISSUE["B"]: [ISSUE["A"]]}))

    assert _entry(b)["blocked_by"] == [ISSUE["A"]]
    assert _order(held) == ["A", "B"]
    held._queue_dir = a.parent
    assert held._is_head(a) and not held._is_head(b)  # the waiter-side head check reads the same entries


def test_row5_blocker_not_in_the_line_leaves_the_place(held, tmp_path):
    _queue(held, 1, "B")
    _queue(held, 2, "C")
    held.refresh_priorities(_no_labels, tmp_path / "merge.log", _blockers({ISSUE["B"]: [999]}))
    assert _order(held) == ["B", "C"]


def test_row5_cycle_falls_back_to_arrival_order_and_logs_one_line(held, tmp_path):
    _queue_b_then_a(held)
    log = tmp_path / "merge.log"
    held.refresh_priorities(_no_labels, log, _blockers({ISSUE["B"]: [ISSUE["A"]], ISSUE["A"]: [ISSUE["B"]]}))
    assert _order(held) == ["B", "A"]
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "blocker cycle" in ln]
    assert len(lines) == 1


def test_row5_chain_and_priority_order_stay_first(held, tmp_path):
    """C waits for B, B waits for A; arrival C, B, A. A flow-fix X (arrival last) still heads the line."""
    ISSUE["X"] = 14
    c = _queue(held, 1, "C")
    _queue(held, 2, "B")
    _queue(held, 3, "A")
    _queue(held, 4, "X")
    held.refresh_priorities(lambda i: {"flow-fix": F_MARK} if i == 14 else {}, tmp_path / "merge.log",
                            _blockers({ISSUE["C"]: [ISSUE["B"]], ISSUE["B"]: [ISSUE["A"]]}))
    assert _order(held) == ["X", "A", "B", "C"]


def test_row5_failed_blocker_read_changes_nothing_and_logs_one_line(held, tmp_path):
    b, _a = _queue_b_then_a(held)
    log = tmp_path / "merge.log"
    held.refresh_priorities(_no_labels, log, _blockers({ISSUE["B"]: [ISSUE["A"]]}))
    assert _order(held) == ["A", "B"]

    def failing(issue: int):
        raise RuntimeError("gh down")

    held.refresh_priorities(_no_labels, log, failing)
    assert _entry(b)["blocked_by"] == [ISSUE["A"]]  # the previous read stands
    assert _order(held) == ["A", "B"]
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "blockers not read" in ln]
    assert len(lines) == 2  # one per failed read (B's and A's issue)


def test_row5_merge_status_orders_by_the_blocker_too(tmp_path):
    root = tmp_path
    for d in ("locks/merge.lock.queue", "locks/merge.lock.batch"):
        (root / "var" / d).mkdir(parents=True)
    (root / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (root / "var" / "agentops" / "results").mkdir(parents=True)
    _log(root)
    t0 = int(time.time()) - 600
    lock = MergeLock(root / "var" / "locks" / "merge.lock")
    lock.acquire(max_age_seconds=60)
    _ticket(root, int((t0 + 100) * 1e9), 1, "orch-B", [ISSUE["B"]])
    _ticket(root, int((t0 + 200) * 1e9), 2, "orch-A", [ISSUE["A"]])
    lock.refresh_priorities(_no_labels, root / "var" / "agentops" / "merge.log",
                            _blockers({ISSUE["B"]: [ISSUE["A"]]}))
    _running_batch(root, t0, [], percent=50)
    snap = ms.collect(root, clock=t0 + 600)
    assert [w["branch"] for w in snap["waiting"]] == ["orch-A", "orch-B"]


# -- review findings --------------------------------------------------------------------------------------------

def test_mixed_read_never_ends_below_the_previous_mark(held, tmp_path):
    """The entry holds flow-fix from issue 11; the read of 11 fails, issue 12 reads `priority`: the entry ends no
    lower than flow-fix (`priority` ranks above it, so it is the better one and is kept)."""
    qdir = held.path.with_name(held.path.name + ".queue")
    qdir.mkdir(parents=True, exist_ok=True)
    t = qdir / f"{1:020d}-{os.getpid()}-1"
    info = {"kind": "merge", "branch": "A", "suite": "s", "remote": "origin", "main": "main", "cwd": "c",
            "issues": [11, 12], "result": "", "priority": FLOW}
    t.write_text(json.dumps(info), encoding="utf-8")

    def reader(issue: int):
        if issue == 11:
            raise RuntimeError("gh down")
        return {"priority": "2026-10-17T12:10:00Z"}

    held.refresh_priorities(reader, tmp_path / "merge.log", lambda i: ())
    assert _entry(t)["priority"]["class"] == "priority"

    # a weaker read (a later flow-fix mark) never replaces the previous, older flow-fix
    t.write_text(json.dumps(info), encoding="utf-8")
    held.refresh_priorities(lambda i: (_ for _ in ()).throw(RuntimeError("down")) if i == 11
                            else {"flow-fix": "2026-10-17T13:00:00Z"}, tmp_path / "merge.log", lambda i: ())
    assert _entry(t)["priority"] == FLOW


def test_a_failed_rewrite_never_fails_the_holder_and_logs_one_line(held, tmp_path, monkeypatch):
    _queue(held, 1, "A")
    c = _queue(held, 2, "C")
    log = tmp_path / "merge.log"

    def refuse(dest, text):
        raise PermissionError("a reader holds the file")

    monkeypatch.setattr(held, "_write_atomic", refuse)
    held.refresh_priorities(lambda i: {"flow-fix": F_MARK} if i == ISSUE["C"] else {}, log, lambda i: ())
    assert _entry(c)["priority"] is None  # untouched
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "priority not written" in ln]
    assert len(lines) == 1 and " | C | " in lines[0]


def test_gh_blockers_reads_open_native_blockers_like_the_dispatcher_and_skips_closed_ones():
    from tools.agentops import merge_priority

    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        return json.dumps({"number": 7, "blockedBy": {"nodes": [{"number": 5, "state": "OPEN"},
                                                                {"number": 4, "state": "CLOSED"}], "totalCount": 2}})

    assert merge_priority.gh_blockers(7, runner=runner, repo="o/r") == [5]
    assert calls == [["issue", "view", "7", "--repo", "o/r", "--json", "number,blockedBy"]]
