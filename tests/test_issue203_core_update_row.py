"""#203 unit 1: a core update's merge-lock ticket and holder show in `merge-status` as their own rows.

A live queue ticket ``{"kind": "core-update", "sha": ...}`` is one ``waiting`` row (``kind``, ``sha``, ``position``,
``queued``, ``waited_s``, ``eta_start_s``) and stays a barrier in the batch walk: the merge waiters' ``eta_start_s``
and ``queue_end_s`` equal those of the same queue with today's no-kind barrier. A ``core-update`` holder gives a
``running`` row with ``kind``, ``sha``, ``branches == []``. ``_target_lock`` hands the sha to the merge lock.

Fixtures are built in tmp_path; the clock is injected through ``ms.collect(root, clock=...)``; no heavy tool, no gh.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tools.agentops import core_update
from tools.agentops import merge_status as ms

SUITE_MIN = 20  # every landed suite in the fixture log: median 1200 s
ROW_KEYS = {"kind", "sha", "position", "queued", "waited_s", "eta_start_s"}


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


def _name(ns: int, n: int) -> str:
    return f"{ns:020d}-{os.getpid()}-{n}"


def _merge_ticket(root, ns: int, n: int, branch: str, issues: list[int]) -> str:
    info = {"kind": "merge", "branch": branch, "issues": issues, "suite": "s", "remote": "r", "main": "main", "cwd": "c"}
    name = _name(ns, n)
    (root / "var" / "locks" / "merge.lock.queue" / name).write_text(json.dumps(info), encoding="utf-8")
    return name


def _ticket(root, ns: int, n: int, info: dict) -> str:
    name = _name(ns, n)
    (root / "var" / "locks" / "merge.lock.queue" / name).write_text(json.dumps(info), encoding="utf-8")
    return name


def _queue(base: Path, barrier_info: dict) -> dict:
    """Two merge waiters with a barrier between them (arrival t=1 min, barrier t=2 min, t=3 min), at clock t0+10 min."""
    for sub in ("var/locks/merge.lock.queue", "var/locks/merge.lock.batch", "var/agentops/merge_suite",
                "var/agentops/results"):
        (base / sub).mkdir(parents=True)
    _log(base)
    t0 = int(time.time()) - 600
    _merge_ticket(base, int((t0 + 60) * 1e9), 1, "orch-1", [1])
    _ticket(base, int((t0 + 120) * 1e9), 2, barrier_info)
    _merge_ticket(base, int((t0 + 180) * 1e9), 3, "orch-3", [3])
    return ms.collect(base, clock=t0 + 600)


def test_core_update_ticket_is_one_waiting_row_between_merge_waiters(root):
    """Row 1: the queue ``merge, core-update, merge`` gives 3 ``waiting`` rows in queue order; the middle one is the
    core update row with its sha; the merge waiters' eta_start_s equal those of the same queue with a no-kind barrier."""
    snap = _queue(root / "now", {"kind": "core-update", "sha": "abcd1234"})
    w = snap["waiting"]
    assert [x.get("kind") for x in w] == [None, "core-update", None]
    assert w[1]["sha"] == "abcd1234"
    assert set(w[1]) == ROW_KEYS
    assert [x["position"] for x in w] == [1, 2, 3]
    assert [x["branch"] for x in (w[0], w[2])] == ["orch-1", "orch-3"]

    # the same queue, the barrier written as today's no-kind ticket
    today = _queue(root / "today", {})
    assert snap["queue_end_s"] == today["queue_end_s"]
    assert len(today["waiting"]) == 2  # today's barrier has no row
    assert [x["eta_start_s"] for x in (w[0], w[2])] == [x["eta_start_s"] for x in today["waiting"]]
    assert [x["eta_start_s"] for x in (w[0], w[2])] == [0, 2 * SUITE_MIN * 60]  # the barrier takes one suite of its own


def test_core_update_holder_is_the_running_row(root):
    """Row 2: a ``core-update`` holder gives ``running`` with kind and sha, no branches, the tests fields empty."""
    _log(root)
    t0 = time.time() - 60
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(t0), "token": "t",
                    "kind": "core-update", "sha": "abcd1234"}), encoding="utf-8")
    running = ms.collect(root, clock=t0 + 60)["running"]
    assert running["kind"] == "core-update"
    assert running["sha"] == "abcd1234"
    assert running["branches"] == []
    assert running["issues"] == []
    assert running["phase"]
    assert running["started"]
    assert running["tests_done"] is None
    assert running["tests_total"] is None


def test_merge_holder_running_row_has_no_sha_key(root):
    """A merge holder's running row keeps today's keys: no sha key is added to a merge row."""
    _log(root)
    t0 = time.time() - 60
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(t0), "token": "t", "kind": "merge", "branch": "orch-1"}),
        encoding="utf-8")
    (root / "var" / "locks" / "merge.lock.holding").write_text(
        json.dumps({"pid": os.getpid(), "ns": 1, "info": {"kind": "merge", "branch": "orch-1", "issues": [1]}}),
        encoding="utf-8")
    running = ms.collect(root, clock=t0 + 60)["running"]
    assert running["kind"] == "merge"
    assert "sha" not in running
    assert running["branches"] == ["orch-1"]


def test_core_update_waiting_row_renders_in_text(root):
    """The text view (`merge-status` without --json) names the core update row instead of failing on a row with no branch."""
    snap = _queue(root / "text", {"kind": "core-update", "sha": "abcd1234"})
    text = ms.render_text(snap)
    assert "core update abcd1234" in text


def test_target_lock_passes_core_update_ticket_to_the_merge_lock(tmp_path, monkeypatch):
    """Row 3: `_target_lock` hands ``{"kind": "core-update", "sha": ...}`` to the merge lock; before the head is read
    the ticket carries no ``sha`` key (#207)."""
    recorded = []

    from contextlib import contextmanager

    @contextmanager
    def fake_lock(path, *args, **kwargs):
        recorded.append({"path": path, "wait_s": kwargs.get("wait_s"), **kwargs})
        yield None, None

    monkeypatch.setattr(core_update, "merge_then_checkout_locked", fake_lock)

    with core_update._target_lock(tmp_path, 5.0, "abcd1234"):
        pass
    assert recorded[-1]["merge_ticket_info"] == {"kind": "core-update", "sha": "abcd1234"}
    assert recorded[-1]["wait_s"] == 5.0
    assert recorded[-1]["path"] == Path(tmp_path) / "var" / "locks" / "merge.lock"

    with core_update._target_lock(tmp_path, None):
        pass
    assert recorded[-1]["merge_ticket_info"] == {"kind": "core-update"}  # #207: no sha key before the head is read


def test_holder_record_names_the_core_update_and_its_sha():
    """The holder file's record: kind and sha; no sha key when the home head is not read yet (#207)."""
    from tools.agentops import merge

    assert merge._holder_record({"kind": "core-update", "sha": "abcd1234"}) == {"kind": "core-update", "sha": "abcd1234"}
    assert merge._holder_record({"kind": "core-update", "sha": None}) == {"kind": "core-update"}


def test_real_core_update_holder_written_by_the_lock_shows_as_running(root):
    """A holder taken through the real merge lock (MergeLock.acquire with a core-update ticket) is read back by merge-status
    as ``running.kind == "core-update"`` with its sha, no branches."""
    from tools.agentops.merge import MergeLock

    _log(root)
    lock_path = root / "var" / "locks" / "merge.lock"
    lock = MergeLock(lock_path)
    assert lock.acquire(ticket_info={"kind": "core-update", "sha": "abcd1234"}) is None  # the lock is taken
    try:
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder["kind"] == "core-update" and holder["sha"] == "abcd1234"
        running = ms.collect(root, clock=time.time())["running"]
        assert running["kind"] == "core-update"
        assert running["sha"] == "abcd1234"
        assert running["branches"] == []
        assert "core update abcd1234" in ms.render_text(ms.collect(root, clock=time.time()))
    finally:
        lock.release()
