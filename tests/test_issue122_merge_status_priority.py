"""#122 unit 4: `merge-status` shows the merge line in rule 3's order (Priority waiters first by rank, then arrival),
batches follow rule 4 (a Priority batch holds only Priority waiters), `position`, `batch` and `eta_start_s` follow.

Fixtures are built in tmp_path; the clock is injected through ``ms.collect(root, clock=...)``; no heavy tool, no gh.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tools.agentops import merge_status as ms

LINE = "." * 72
SUITE_MIN = 20  # every landed suite in the fixture log: median 1200 s


@pytest.fixture
def root(tmp_path):
    (tmp_path / "var" / "locks" / "merge.lock.queue").mkdir(parents=True)
    (tmp_path / "var" / "locks" / "merge.lock.batch").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "results").mkdir(parents=True)
    return tmp_path


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _log(root, minutes=(SUITE_MIN,) * 6):
    lines = [f"2026-10-17 10:0{i} | orch-{i} | pushed | suite {m} min, 6 workers | 0 failed | output x"
             for i, m in enumerate(minutes)]
    (root / "var" / "agentops" / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _running_batch(root, t0, claimed_names, percent=50):
    """A running suite started at t0, at `percent` done (no plan): its eta is elapsed * (100 - p) / p.
    The holder's branch is `orch-1`; `claimed_names` are the queue names of its batch (the `.claim` files)."""
    d = root / "var" / "agentops" / "merge_suite"
    cur = d / "20261017T100000-000001-orch-1.out"
    cur.write_text(f"{LINE} [{percent:3d}%]\n", encoding="utf-8")
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(t0), "token": "t", "progress": str(cur)}),
        encoding="utf-8")
    (root / "var" / "locks" / "merge.lock.holding").write_text(
        json.dumps({"pid": os.getpid(), "ns": 1, "info": {"kind": "merge", "branch": "orch-1", "issues": [1]}}),
        encoding="utf-8")
    for name in claimed_names:
        (root / "var" / "locks" / "merge.lock.batch" / f"{name}.claim").write_text("", encoding="utf-8")


def _name(ns: int, n: int) -> str:
    return f"{ns:020d}-{os.getpid()}-{n}"


def _ticket(root, ns: int, n: int, branch: str, issues: list[int], priority=None) -> str:
    info = {"kind": "merge", "branch": branch, "issues": issues, "suite": "s", "remote": "r", "main": "main", "cwd": "c"}
    if priority is not None:
        info["priority"] = priority
    name = _name(ns, n)
    (root / "var" / "locks" / "merge.lock.queue" / name).write_text(json.dumps(info), encoding="utf-8")
    return name


def _flow(marked: str = "2026-10-17T10:30:00Z") -> dict:
    return {"class": "flow-fix", "marked": marked, "hard": False}


def test_row8_line_at_t10_priority_first_then_ordinary_batch(root):
    """Row 8: batch [O1,O2] runs from t=0, suite 20 min; O3 queued t=5, F1 (flow-fix) t=6, O4 t=7; clock t=10.
    Waiting order F1 (position 1, batch 1), then O3, O4 (batch 2); ETAs 10 and 30 minutes from t=10."""
    _log(root)
    t0 = int(time.time()) - 600
    o1 = _ticket(root, int((t0 - 600) * 1e9), 1, "orch-1", [1])
    o2 = _ticket(root, int((t0 - 500) * 1e9), 2, "orch-2", [2])
    _running_batch(root, t0, [o1, o2], percent=50)  # 50% at 10 min: eta 10 min
    _ticket(root, int((t0 + 300) * 1e9), 3, "orch-3", [3])                 # O3, t=5
    _ticket(root, int((t0 + 360) * 1e9), 4, "orch-4", [4], _flow())         # F1, t=6
    _ticket(root, int((t0 + 420) * 1e9), 5, "orch-5", [5])                 # O4, t=7

    snap = ms.collect(root, clock=t0 + 600)
    assert snap["running"]["eta_s"] == 600
    w = snap["waiting"]
    assert [x["branch"] for x in w] == ["orch-4", "orch-3", "orch-5"]
    assert [x["issues"] for x in w] == [[4], [3], [5]]
    assert [x["position"] for x in w] == [1, 2, 3]
    assert [x["batch"] for x in w] == [1, 2, 2]
    assert [x["eta_start_s"] for x in w] == [600, 1800, 1800]  # 10 and 30 minutes from t=10
    assert snap["queue_end_s"] == 600 + 2 * SUITE_MIN * 60


def test_priority_batch_holds_only_priority_waiters(root):
    """Rule 4: a Priority batch holds only Priority waiters; ordinary waiters form the later batches."""
    _log(root)
    t0 = int(time.time()) - 600
    _running_batch(root, t0, [], percent=50)
    _ticket(root, int((t0 + 100) * 1e9), 1, "orch-1x", [11])                     # ordinary, arrived first
    _ticket(root, int((t0 + 200) * 1e9), 2, "orch-2x", [12], _flow())            # Priority
    _ticket(root, int((t0 + 300) * 1e9), 3, "orch-3x", [13], _flow())            # Priority, arrived after

    w = ms.collect(root, clock=t0 + 600)["waiting"]
    assert [x["branch"] for x in w] == ["orch-2x", "orch-3x", "orch-1x"]
    assert [x["batch"] for x in w] == [1, 1, 2]
    assert [x["eta_start_s"] for x in w] == [600, 600, 600 + SUITE_MIN * 60]


def test_priority_rank_orders_flow_fix_after_priority_then_older_mark_first(root):
    _log(root)
    t0 = int(time.time()) - 600
    _running_batch(root, t0, [], percent=50)
    _ticket(root, int((t0 + 100) * 1e9), 1, "orch-f", [21], _flow("2026-10-17T10:40:00Z"))
    _ticket(root, int((t0 + 200) * 1e9), 2, "orch-p", [22],
            {"class": "priority", "marked": "2026-10-17T10:50:00Z", "hard": False})
    _ticket(root, int((t0 + 300) * 1e9), 3, "orch-f0", [23], _flow("2026-10-17T10:20:00Z"))

    w = ms.collect(root, clock=t0 + 600)["waiting"]
    assert [x["branch"] for x in w] == ["orch-p", "orch-f0", "orch-f"]
    assert [x["position"] for x in w] == [1, 2, 3]


def test_barrier_splits_the_line_and_each_segment_is_ordered_on_its_own(root):
    """A suite commit is a barrier: the priority order runs inside each segment between barriers."""
    _log(root)
    t0 = int(time.time()) - 600
    _running_batch(root, t0, [], percent=50)
    _ticket(root, int((t0 + 100) * 1e9), 1, "orch-o1", [31])
    _ticket(root, int((t0 + 200) * 1e9), 2, "orch-c", [32])
    (root / "var" / "locks" / "merge.lock.queue" / _name(int((t0 + 200) * 1e9), 2)).write_text(
        json.dumps({"kind": "commit", "needs_suite": True, "message": "m", "issues": [32]}), encoding="utf-8")
    _ticket(root, int((t0 + 300) * 1e9), 3, "orch-o2", [33])
    _ticket(root, int((t0 + 400) * 1e9), 4, "orch-f2", [34], _flow())

    w = ms.collect(root, clock=t0 + 600)["waiting"]
    assert [x["branch"] for x in w] == ["orch-o1", "orch-f2", "orch-o2"]
    assert [x["position"] for x in w] == [1, 2, 3]
    assert [x["batch"] for x in w] == [1, 3, 4]  # batch 2 is the barrier commit's own suite
    assert [x["eta_start_s"] for x in w] == [600, 600 + 2 * SUITE_MIN * 60, 600 + 3 * SUITE_MIN * 60]


def test_batch_starts_priority_and_ordinary_keys_never_share_a_batch():
    keys_p = ("s", "r", "m", "c", True)
    keys_o = ("s", "r", "m", "c", False)
    starts, end = ms.batch_starts([keys_p, keys_o, keys_p], 100, 1000)
    assert starts == [100, 1100, 100]  # the two Priority entries share batch 1, the ordinary one batch 2
    assert end == 2100


def test_flow_fix_line_carries_a_plain_words_mark_and_ordinary_lines_do_not(root):
    _log(root)
    t0 = int(time.time()) - 600
    _running_batch(root, t0, [], percent=50)
    _ticket(root, int((t0 + 100) * 1e9), 1, "orch-o", [41])
    _ticket(root, int((t0 + 200) * 1e9), 2, "orch-f", [42], _flow())

    text = ms.render_text(ms.collect(root, clock=t0 + 600))
    lines = {ln.split(":")[0]: ln for ln in text.splitlines() if ln.startswith("waiting")}
    assert "flow fix" in lines["waiting 1"]
    assert "flow" not in lines["waiting 2"] and "priority" not in lines["waiting 2"]
    assert "flow-fix" not in text  # no raw code on the card
