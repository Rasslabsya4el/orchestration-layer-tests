"""#122 unit 6, the hard cut (rule 5) on the real run_merge path: a Priority merge cuts a running batch
ahead by a hard-push; the fake merge suite runs on a simulated clock in minutes, and the waiting Priority
merge is the real ``HardWatch``. The machine record of rule 6 is gone (#205)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_issue95_target_check_eject import SUITE, _branch, _world
from tools.agentops import core_targets, merge, merge_priority
from tools.agentops.merge import MergeLock, SuiteRun
from tools.agentops.merge_priority import HardWatch, clear_cut, cut_path, pending_cut, request_cut

F_MARK = "2026-10-17T11:30:00Z"
CLEAN = "# core\nplain\nmore\n"
FLOW = {"class": "flow-fix", "marked": F_MARK, "hard": False}
HARD = {"class": "flow-fix", "marked": F_MARK, "hard": True}


def _ticket(lock_path: Path, name: str, info: dict | None = None) -> Path:
    qdir = lock_path.with_name(lock_path.name + ".queue")
    qdir.mkdir(parents=True, exist_ok=True)
    t = qdir / name
    t.write_text(json.dumps(info or {"kind": "merge"}), encoding="utf-8")
    return t


# -- the cut request file and the waiter's label watch -------------------------------------------------------------

def test_request_is_atomic_json_beside_the_lock_and_pending_reads_it(tmp_path):
    lock = tmp_path / "merge.lock"
    t = _ticket(lock, "0001-%d-1" % os.getpid())
    request_cut(lock, t.name, os.getpid(), 122, wall=lambda: 1000.0)
    assert cut_path(lock).name == "merge.lock.cut"
    assert json.loads(cut_path(lock).read_text(encoding="utf-8")) == {
        "ticket": t.name, "pid": os.getpid(), "issue": 122, "at": 1000.0}
    got = pending_cut(lock, wall=lambda: 1060.0, max_age_s=1800, pid_alive=lambda p: True)
    assert got and got["issue"] == 122 and got["ticket"] == t.name
    clear_cut(lock)
    assert not cut_path(lock).exists()


@pytest.mark.parametrize("why", ["requester dead", "ticket left the queue", "older than the ceiling", "garbage"])
def test_a_stale_request_is_ignored_and_removed(tmp_path, why):
    lock = tmp_path / "merge.lock"
    t = _ticket(lock, "0001-%d-1" % os.getpid())
    request_cut(lock, t.name, os.getpid(), 122, wall=lambda: 1000.0)
    alive, wall = (lambda p: True), (lambda: 1100.0)
    if why == "requester dead":
        alive = lambda p: False  # noqa: E731
    elif why == "ticket left the queue":
        t.unlink()
    elif why == "older than the ceiling":
        wall = lambda: 1000.0 + 1801  # noqa: E731
    else:
        cut_path(lock).write_text("{not json", encoding="utf-8")
    assert pending_cut(lock, wall=wall, max_age_s=1800, pid_alive=alive) is None
    assert not cut_path(lock).exists()


def test_no_request_file_is_no_request(tmp_path):
    assert pending_cut(tmp_path / "merge.lock", wall=lambda: 0.0, max_age_s=10, pid_alive=lambda p: True) is None


def _watch(tmp_path, reads, priority=FLOW, clock=None):
    lock = tmp_path / "merge.lock"
    t = _ticket(lock, "0001-%d-1" % os.getpid())
    state = {"t": 0.0}
    seen: list[int] = []

    def reader(issue):
        seen.append(issue)
        return reads(state["t"])

    w = HardWatch(lock, priority, [122], reader=reader, clock=lambda: state["t"], every=120.0, wall=lambda: 5.0)
    return lock, t, state, seen, w


def test_hardwatch_rereads_labels_every_two_minutes_and_asks_for_the_cut_on_hard_push(tmp_path):
    lock, t, state, seen, w = _watch(
        tmp_path, lambda now: {"flow-fix": F_MARK, **({"hard-push": F_MARK} if now >= 480 else {})})
    for sec in (0, 60, 119):
        state["t"] = sec
        assert w.poll(t.name) is False
    assert seen == []  # not read before the two minutes are out
    for sec in (120, 240, 360):
        state["t"] = sec
        assert w.poll(t.name) is False
    assert seen == [122, 122, 122] and not cut_path(lock).exists()
    state["t"] = 480  # the label is on from minute 8
    assert w.poll(t.name) is True
    assert json.loads(cut_path(lock).read_text(encoding="utf-8"))["ticket"] == t.name
    assert len(seen) == 4
    state["t"] = 700
    assert w.poll(t.name) is True and len(seen) == 4  # asked once: no more reads
    w.withdraw()
    assert not cut_path(lock).exists()


def test_hardwatch_a_mark_that_is_already_hard_asks_at_once_without_a_read(tmp_path):
    lock, t, state, seen, w = _watch(tmp_path, lambda now: {}, priority=HARD)
    assert w.poll(t.name) is True and seen == [] and cut_path(lock).exists()


def test_hardwatch_ordinary_merge_and_failed_reads_never_ask(tmp_path):
    lock, t, state, seen, w = _watch(tmp_path, lambda now: {}, priority=None)
    state["t"] = 9999
    assert w.poll(t.name) is False and seen == [] and not cut_path(lock).exists()

    def broken(now):
        raise RuntimeError("gh down")
    lock, t, state, seen, w = _watch(tmp_path, broken)
    state["t"] = 130
    assert w.poll(t.name) is False and not cut_path(lock).exists()


# -- row 4 on the real run_merge path -----------------------------------------------------------------------------------

def _env(tmp_path, monkeypatch):
    for name, value in (("SUITE_OUTPUT_DIR", tmp_path / "suite_out"), ("MERGE_LOG_PATH", tmp_path / "merge.log")):
        monkeypatch.setattr(merge, name, value)
    monkeypatch.setenv(merge.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])


def _ordinary_info(checkout: Path, ref: str, priority=None) -> dict:
    return {"kind": "merge", "branch": ref, "suite": SUITE, "remote": merge.DEFAULT_REMOTE,
            "main": merge.DEFAULT_MAIN, "cwd": str(checkout.resolve()), "issues": [], "result": "",
            "priority": priority}


def test_row4_hard_push_kills_the_running_suite_by_minute_10_and_the_priority_merge_takes_the_lock(
        tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    bare, checkout, lockfile = _world(tmp_path)
    o1, o2, o3 = (_branch(tmp_path, bare, n, CLEAN) for n in ("f1", "f2", "f3"))
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {})  # the holder is ordinary
    qdir = lockfile.with_name(lockfile.name + ".queue")
    qdir.mkdir(parents=True, exist_ok=True)
    base_ns = time.time_ns()
    o2_file = qdir / f"{base_ns + 10**9:020d}-{os.getpid()}-2"
    o3_file = qdir / f"{base_ns + 2 * 10**9:020d}-{os.getpid()}-3"
    o2_file.write_text(json.dumps(_ordinary_info(checkout, o2)), encoding="utf-8")
    o3_file.write_text(json.dumps(_ordinary_info(checkout, o3)), encoding="utf-8")
    sim = {"t": 0}
    seen: dict = {"suite_calls": 0, "killed": []}
    monkeypatch.setattr(merge, "_kill_tree", lambda pid: seen["killed"].append(pid))
    f1_file = qdir / f"{base_ns + 5 * 10**9:020d}-{os.getpid()}-9"
    hold_path = lockfile.with_name(lockfile.name + ".holding")

    def fake_suite(*a, cut_check=None, **k):
        seen["suite_calls"] += 1
        ns = json.loads(hold_path.read_text(encoding="utf-8"))["ns"]
        if seen["suite_calls"] == 2:  # the batch that runs after the cut
            seen["second_ns"] = ns
            seen["second_claims"] = sorted(p.name for p in lockfile.with_name(lockfile.name + ".batch").glob("*.claim"))
            return SuiteRun(True, "", 1)
        seen["first_ns"] = ns
        seen["first_claims"] = sorted(p.name for p in lockfile.with_name(lockfile.name + ".batch").glob("*.claim"))
        assert cut_check is not None
        watch = None
        for t in range(0, 21):  # a 20-minute suite, one look per minute
            sim["t"] = t
            if t == 6:  # F1 queues; it has no label yet
                f1_file.write_text(json.dumps(_ordinary_info(checkout, "origin/f1", FLOW)), encoding="utf-8")
                watch = HardWatch(lockfile, FLOW, [122], clock=lambda: sim["t"] * 60.0, every=120.0,
                                  reader=lambda n: {"flow-fix": F_MARK, **({"hard-push": F_MARK} if sim["t"] >= 8 else {})})
            if watch is not None:
                watch.poll(f1_file.name)
            cut = cut_check()
            if cut:
                seen["cut_at"] = t
                return SuiteRun(False, "", 1, cut_for=cut)
        return SuiteRun(True, "", 1)

    monkeypatch.setattr(merge, "_run_merge_suite", fake_suite)
    real_acquire = merge.acquire_merge_line
    calls: list[dict] = []

    def spy(lock_path, timeout, ticket_info, hook=None, **kw):
        calls.append(kw)
        if len(calls) == 2:  # the holder has released the lock and goes back to the line
            lk = MergeLock(lockfile)
            lk._queue_dir = qdir
            seen["lock_free"] = not lockfile.exists()
            seen["request_gone"] = not cut_path(lockfile).exists()
            seen["f1_head"] = lk._is_head(f1_file)
            seen["o2_kept"] = o2_file.exists() and o3_file.exists()
            seen["claims_at_requeue"] = sorted(p.name for p in lockfile.with_name(lockfile.name + ".batch").glob("*.claim"))
            f1_file.unlink()  # F1 takes the lock and lands: its ticket leaves the line
        return real_acquire(lock_path, timeout, ticket_info, hook, **kw)

    monkeypatch.setattr(merge, "acquire_merge_line", spy)
    main_before = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], capture_output=True, text=True).stdout
    result = merge.run_merge(o1, SUITE, cwd=checkout, lock_path=lockfile, lock_timeout=60, issues=(1,))

    assert seen["cut_at"] <= 10 and seen["cut_at"] >= 8  # label at minute 8, killed by minute 10
    assert seen["lock_free"] and seen["request_gone"] and seen["f1_head"]  # F1 heads the line once the lock is free
    assert seen["o2_kept"] and seen["claims_at_requeue"] == []  # members keep their queue files, no claim left
    assert len(seen["first_claims"]) == 2  # O2 and O3 were the batch that got cut
    assert "arrival_ns" not in calls[0] and calls[1]["arrival_ns"] == seen["first_ns"]  # the holder keeps its place
    assert seen["second_ns"] == seen["first_ns"] < int(o3_file.name.split("-")[0])  # ahead of O3
    assert result.ok and result.pushed and result.batch_branches == [o1, o2, o3]  # the next batch holds O1, O2, O3
    log = (tmp_path / "merge.log").read_text(encoding="utf-8")
    assert f"{o1} | cut |" in log
    for ref in (o1, o2, o3):
        assert f"{ref} | cut for a Priority ticket (#122)" in log  # worded, naming the ticket that cut
    assert main_before != subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], capture_output=True,
                                         text=True).stdout


def test_a_priority_batch_is_never_cut(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    bare, checkout, lockfile = _world(tmp_path)
    p1 = _branch(tmp_path, bare, "f1", CLEAN)
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {"flow-fix": F_MARK})
    t = _ticket(lockfile, f"{time.time_ns() + 10**9:020d}-{os.getpid()}-7")
    request_cut(lockfile, t.name, os.getpid(), 9)
    got: dict = {}

    def fake_suite(*a, cut_check=None, **k):
        got["cut_check"] = cut_check
        return SuiteRun(True, "", 1)

    monkeypatch.setattr(merge, "_run_merge_suite", fake_suite)
    result = merge.run_merge(p1, SUITE, cwd=checkout, lock_path=lockfile, lock_timeout=60, issues=(1,))
    assert result.ok and got["cut_check"] is None
    assert cut_path(lockfile).exists()  # not the holder's to take; a Priority merge's own end clears nothing of others


def test_a_stale_request_does_not_cut_an_ordinary_batch(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    bare, checkout, lockfile = _world(tmp_path)
    o1 = _branch(tmp_path, bare, "f1", CLEAN)
    monkeypatch.setattr(merge_priority, "gh_marks", lambda issue: {})
    request_cut(lockfile, "no-such-ticket", os.getpid(), 9)
    seen: dict = {}

    def fake_suite(*a, cut_check=None, **k):
        seen["cut"] = cut_check()
        return SuiteRun(True, "", 1)

    monkeypatch.setattr(merge, "_run_merge_suite", fake_suite)
    result = merge.run_merge(o1, SUITE, cwd=checkout, lock_path=lockfile, lock_timeout=60, issues=(1,))
    assert result.ok and seen["cut"] == "" and not cut_path(lockfile).exists()


def test_the_suite_watcher_kills_the_tree_on_a_cut_and_reports_it(monkeypatch):
    """``StallWatch`` is the suite wait loop's watcher: a cut check that answers kills through ``kill`` once."""
    class Proc:
        pid = 4242
        def poll(self):
            return None

    killed: list[int] = []
    answers = iter(["", "", "122"])  # the shape ``merge._cut_checker`` answers: the issue number as text
    watch = merge.StallWatch(None, interval=0.01, kill=killed.append, cut_check=lambda: next(answers),
                             machine_cpu=lambda: None)
    import threading
    watch.watch(Proc(), lambda: "", threading.Event())
    assert killed == [4242] and watch.cut == "122" and watch.reason == ""


# -- review fixes -------------------------------------------------------------------------------------------------------

def test_a_cut_suite_line_is_not_a_yardstick_for_the_eta_median(tmp_path):
    from tools.agentops import merge_status
    log = tmp_path / "merge.log"
    for minutes in (5, 7, 6):
        merge._append_merge_log(f"b{minutes}", merge.MergeResult(ok=True, reason="pushed", suite_duration=minutes * 60.0),
                                log_path=log)
    merge._append_merge_log("cut-one", merge.MergeResult(ok=False, reason="cut", suite_duration=2 * 60.0), log_path=log)
    durations, _workers = merge_status._log_facts(log)
    assert durations == [300.0, 420.0, 360.0]  # the 2-minute cut line is not among them
