"""#233 unit 2: the machine line serves waiters in the owner's repo order (acceptance rows 1-5), the overdue bound,
the rank reason on the waiter's line and in `merge status`."""

import json
import threading
import time

import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms
from tools.agentops import merge_status
from tools.agentops import repo_order as ro


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


NOON = time.mktime((2026, 10, 19, 12, 3, 0, 0, 0, -1))
MIN = 60.0


class Fake:
    """A clock the test moves by hand (the waiters read it for their wait start and for `now`)."""

    def __init__(self, t=NOON):
        self.t = t

    def __call__(self):
        return self.t


def alive(pid, started):
    return True


def _wait_until(predicate, what):
    deadline = time.time() + 10
    while not predicate():
        assert time.time() < deadline, what
        time.sleep(0.005)


def _start(results, repo, tmp_path, clock, priority=False):
    """An ordinary or Priority waiter in a thread; it records its repo when it takes the berth, then frees it."""
    def run():
        berth = ms.acquire(tmp_path / repo, repo, 4255, priority=priority, berths=1, is_live=alive,
                           poll=0.005, clock=clock)
        results.append(repo)
        berth.release()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _line(tmp_path, clock, queue, release_at):
    """Hold the one berth, queue the waiters in turn — each `(repo, priority, wait_start)` at its own clock time —
    then at `release_at` free it. Returns the repos in the order they took the berth."""
    holder = ms.acquire(tmp_path / "holder", "hold", 4251, berths=1, is_live=alive)
    results, threads = [], []
    for repo, priority, start in queue:
        clock.t = start
        threads.append(_start(results, repo, tmp_path, clock, priority))
        _wait_until(lambda: len(ms.waiting_rows(is_live=alive, now=clock.t)) == len(threads), f"{repo} waits")
    clock.t = release_at
    holder.release()
    for thread in threads:
        thread.join(10)
    assert ms.max_simultaneous_holders(ms.log_path()) == 1
    return results


def _write_waiter(repo, since, priority=False, ident=None):
    ident = ident or f"{int(since * 1000)}-{repo}"
    record = {
        "id": ident, "repo": repo, "run": "run", "tests": 4255, "priority": priority, "since": since,
        "pid": 1, "started": "12:03:00",
    }
    path = ms._waiter_path(ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


# -- acceptance rows 1-5 -------------------------------------------------------------------------------------

def test_row1_with_order_A_then_B_the_earlier_B_waiter_is_passed_to_A(tmp_path):
    ro.set_order(["A", "B"])
    clock = Fake()
    order = _line(tmp_path, clock, [("B", False, NOON), ("A", False, NOON + 1 * MIN)], release_at=NOON + 2 * MIN)
    assert order == ["A", "B"]


def test_row2_a_priority_waiter_of_B_goes_before_an_ordinary_waiter_of_A(tmp_path):
    ro.set_order(["A", "B"])
    clock = Fake()
    order = _line(tmp_path, clock, [("A", False, NOON), ("B", True, NOON + 1 * MIN)], release_at=NOON + 2 * MIN)
    assert order == ["B", "A"]


def test_row3_at_61_minutes_the_overdue_B_waiter_goes_first(tmp_path):
    ro.set_order(["A", "B"])
    clock = Fake()
    order = _line(tmp_path, clock, [("B", False, NOON), ("A", False, NOON + 56 * MIN)],
                  release_at=NOON + 61 * MIN)
    assert order == ["B", "A"]


def test_row3_at_59_minutes_B_is_not_overdue_and_A_goes_first(tmp_path):
    ro.set_order(["A", "B"])
    clock = Fake()
    order = _line(tmp_path, clock, [("B", False, NOON), ("A", False, NOON + 54 * MIN)],
                  release_at=NOON + 59 * MIN)
    assert order == ["A", "B"]


def test_row4_changing_the_order_while_both_wait_moves_the_next_place_to_B(tmp_path, mdir):
    ro.set_order(["A", "B"])
    clock = Fake()
    holder = ms.acquire(tmp_path / "holder", "hold", 4251, berths=1, is_live=alive)
    results, threads = [], []
    for repo, start in (("A", NOON), ("B", NOON + 1 * MIN)):
        clock.t = start
        threads.append(_start(results, repo, tmp_path, clock))
        _wait_until(lambda: len(ms.waiting_rows(is_live=alive, now=clock.t)) == len(threads), f"{repo} waits")
    log = mdir / "repo_order.jsonl"
    lines_before = len(log.read_text(encoding="utf-8").splitlines())
    ro.set_order(["B", "A"])
    ro.set_order(["B", "A"])  # the same order again: no line
    lines_after = log.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == lines_before + 1
    change = json.loads(lines_after[-1])
    assert (change["old"], change["new"]) == (["A", "B"], ["B", "A"])
    holder.release()
    for thread in threads:
        thread.join(10)
    assert results == ["B", "A"]


def test_row5_empty_order_is_priority_then_wait_start(tmp_path):
    ro.set_order([])
    clock = Fake()
    order = _line(tmp_path, clock,
                  [("A", False, NOON), ("B", False, NOON + 1 * MIN), ("C", True, NOON + 2 * MIN)],
                  release_at=NOON + 3 * MIN)
    assert order == ["C", "A", "B"]


def test_row5_an_unlisted_repo_goes_after_every_listed_one(tmp_path):
    ro.set_order(["B"])
    clock = Fake()
    order = _line(tmp_path, clock, [("A", False, NOON), ("B", False, NOON + 1 * MIN)], release_at=NOON + 2 * MIN)
    assert order == ["B", "A"]


# -- the order function itself: tiers, overdue bound, unlisted ---------------------------------------------

def _rec(repo, since, priority=False):
    return (None, {"id": f"{repo}-{since}", "repo": repo, "priority": priority, "since": since})


def test_order_puts_priority_first_then_overdue_then_the_rest_by_rank(mdir):
    ro.set_order(["A", "B"])
    waiters = [_rec("A", NOON), _rec("B", NOON - 61 * MIN), _rec("B", NOON + 1, priority=True),
               _rec("A", NOON - 30 * MIN)]
    names = [(r["repo"], r["since"]) for _, r in ms._order(waiters, NOON, 60 * MIN)]
    assert names == [("B", NOON + 1), ("B", NOON - 61 * MIN), ("A", NOON - 30 * MIN), ("A", NOON)]


def test_order_with_a_bound_of_zero_minutes_makes_every_waiter_overdue(mdir):
    waiters = [_rec("B", NOON), _rec("A", NOON + 1)]
    names = [r["repo"] for _, r in ms._order(waiters, NOON + 1, 0.0)]
    assert names == ["B", "A"]  # both overdue: oldest first, whatever the repo


# -- rows 1-5 read through the line's own status: `waiting_rows` order strings -----------------------------

def test_waiting_rows_carry_the_rank_reason(mdir):
    ro.set_order(["A", "B"])
    _write_waiter("C", NOON)
    _write_waiter("B", NOON + 1)
    _write_waiter("A", NOON + 2)
    rows = ms.waiting_rows(is_live=alive, now=NOON + 3, max_wait_minutes=60)
    assert [(r["repo"], r["order"]) for r in rows] == [("A", "1 of 2"), ("B", "2 of 2"), ("C", "3 of 2")]


def test_waiting_rows_with_an_empty_order_have_no_rank_reason(mdir):
    ro.set_order([])
    _write_waiter("A", NOON)
    rows = ms.waiting_rows(is_live=alive, now=NOON + 1, max_wait_minutes=60)
    assert rows[0].get("order") is None


# -- the bound: the setting, a non-number, the argument ----------------------------------------------------

def test_the_setting_line_max_wait_minutes_defaults_to_60():
    assert core_settings.DEFAULTS["line_max_wait_minutes"] == 60


def test_the_bound_comes_from_the_setting_and_falls_back_to_60(tmp_path, monkeypatch, mdir):
    ro.set_order(["A", "B"])
    _write_waiter("B", NOON)  # waited 45 min at NOON+45 min
    _write_waiter("A", NOON + 40 * MIN)  # waited 5 min
    now = NOON + 45 * MIN

    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"line_max_wait_minutes": 30})
    assert [r["repo"] for r in ms.waiting_rows(is_live=alive, now=now)] == ["B", "A"]  # 45 > 30: overdue

    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"line_max_wait_minutes": 60})
    assert [r["repo"] for r in ms.waiting_rows(is_live=alive, now=now)] == ["A", "B"]  # 45 < 60: not

    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"line_max_wait_minutes": "soon"})
    assert [r["repo"] for r in ms.waiting_rows(is_live=alive, now=now)] == ["A", "B"]  # not a number: 60

    def broken(*a, **k):
        raise RuntimeError("no settings file")
    monkeypatch.setattr(core_settings, "load", broken)
    assert [r["repo"] for r in ms.waiting_rows(is_live=alive, now=now)] == ["A", "B"]  # unreadable: 60


def test_acquire_takes_an_explicit_max_wait_minutes(tmp_path):
    ro.set_order(["A", "B"])
    clock = Fake()
    holder = ms.acquire(tmp_path / "holder", "hold", 4251, berths=1, is_live=alive)
    results, threads = [], []
    for repo, start in (("B", NOON), ("A", NOON + 40 * MIN)):
        clock.t = start
        berth_repo = repo

        def run(name=berth_repo):
            berth = ms.acquire(tmp_path / name, name, 4255, berths=1, is_live=alive, poll=0.005, clock=clock,
                               max_wait_minutes=30)
            results.append(name)
            berth.release()
        thread = threading.Thread(target=run)
        thread.start()
        threads.append(thread)
        _wait_until(lambda: len(ms.waiting_rows(is_live=alive, now=clock.t)) == len(threads), "waits")
    clock.t = NOON + 45 * MIN
    holder.release()
    for thread in threads:
        thread.join(10)
    assert results == ["B", "A"]  # 45 min > the explicit 30: B overdue


# -- the rank reason on the waiter's own announce line -----------------------------------------------------

def test_the_announce_line_ends_with_the_rank_reason_when_an_order_is_set(tmp_path):
    ro.set_order(["A", "B"])
    holder = ms.acquire(tmp_path / "A", "merge-1", 4251, berths=1, is_live=alive)
    lines = []
    clock = Fake(NOON)
    released = []

    def sleep(seconds):  # the holder frees itself at the first poll; the waiter's own wait starts at NOON
        if not released:
            released.append(True)
            holder.release()
        clock.t += seconds

    berth = ms.acquire(tmp_path / "B", "line-2", 600, berths=1, is_live=alive, clock=clock, sleep=sleep,
                       on_wait=lines.append)
    berth.release()
    assert len(lines) == 1
    assert lines[0].startswith("waits for the machine: B runs 600 tests since ")
    assert lines[0].endswith(" (order: 2 of 2)")


# -- `merge status`: the `waits for the machine:` line carries the rank reason ---------------------------

def test_merge_status_line_appends_the_order_after_priority(tmp_path):
    base = {"repo": "B", "run": "line-2", "tests": 4255, "since": "12:03"}
    snap = merge_status.collect(tmp_path / "empty-root")
    snap["machine_waiting"] = [{**base, "priority": False, "order": "2 of 2"}]
    assert "waits for the machine: B runs 4255 tests since 12:03 (order: 2 of 2)" in merge_status.render_text(snap)

    snap["machine_waiting"] = [{**base, "priority": True, "order": "1 of 2"}]
    assert "waits for the machine: B runs 4255 tests since 12:03, priority (order: 1 of 2)" in \
        merge_status.render_text(snap)

    snap["machine_waiting"] = [{**base, "priority": False, "order": None}]
    text = merge_status.render_text(snap)
    assert "waits for the machine: B runs 4255 tests since 12:03" in text
    assert "(order:" not in text
