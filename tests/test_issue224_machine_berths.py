"""#224 unit 1: the machine-wide berth a heavy run takes before it starts (acceptance rows 1-5)."""

import json
import os
import threading
import time

import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


NOON = time.mktime((2026, 10, 19, 12, 3, 0, 0, 0, -1))


class Clock:
    """A clock a test moves by hand; `sleep` advances it and runs the test's hook (the other repo's action)."""

    def __init__(self, start=NOON, on_sleep=None):
        self.t = start
        self.sleeps = 0
        self.on_sleep = on_sleep

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def events():
    """The berth log as (action, repo) in the order written."""
    lines = (ms.log_path()).read_text(encoding="utf-8").splitlines()
    return [(r["action"], r["repo"]) for r in map(json.loads, lines)]


def alive(pid, started):
    return True


def test_row1_second_heavy_run_starts_only_after_first_frees(tmp_path):
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    clock = Clock(on_sleep=lambda n: first.release())
    second = ms.acquire(
        tmp_path / "repoB", "line-2", 4255, berths=1, is_live=alive, clock=clock, sleep=clock.sleep
    )
    second.release()
    assert clock.sleeps == 1
    assert events() == [("take", "repoA"), ("free", "repoA"), ("take", "repoB"), ("free", "repoB")]
    assert ms.max_simultaneous_holders(ms.log_path()) == 1


def test_wait_line_is_written_once_with_the_exact_text(tmp_path):
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    lines, rows = [], []

    def hook(n):
        rows.append(ms.waiting_rows(is_live=alive))
        if n == 3:
            first.release()

    clock = Clock(on_sleep=hook)
    ms.acquire(
        tmp_path / "repoB", "line-2", 600, berths=1, is_live=alive,
        clock=clock, sleep=clock.sleep, on_wait=lines.append,
    )
    assert lines == ["waits for the machine: repoB runs 600 tests since 12:03"]
    assert rows[0] == [{"repo": "repoB", "run": "line-2", "tests": 600, "since": "12:03", "priority": False}]
    assert ms.waiting_rows(is_live=alive) == []  # taking the berth ends the wait


def test_first_try_free_berth_says_nothing(tmp_path):
    lines = []
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive, on_wait=lines.append)
    assert lines == []


def test_row2_a_light_run_is_not_heavy_and_takes_no_berth(tmp_path):
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    assert ms.is_heavy(selected_tests=20, full_suite=False) is False
    assert ms.is_heavy(selected_tests=500, full_suite=False) is False
    assert ms.is_heavy(selected_tests=501, full_suite=False) is True
    assert ms.is_heavy(selected_tests=20, full_suite=True) is True
    assert events() == [("take", "repoA")]  # the light run called nothing


def test_row3_a_holder_killed_mid_run_frees_the_berth_within_one_poll(tmp_path):
    dead = set()
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive, pid=111)
    clock = Clock(on_sleep=lambda n: dead.add(111))
    second = ms.acquire(
        tmp_path / "repoB", "line-2", 4255, berths=1,
        is_live=lambda pid, started: pid not in dead, clock=clock, sleep=clock.sleep,
    )
    assert clock.sleeps == 1  # one poll after the kill
    assert events() == [("take", "repoA"), ("free", "repoA"), ("take", "repoB")]
    assert ms.max_simultaneous_holders(ms.log_path()) == 1
    first.release()  # the dead run's own late release changes nothing
    assert events()[-1] == ("take", "repoB")
    second.release()


def test_row3_dead_holder_is_free_at_the_first_try(tmp_path):
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive, pid=111)
    clock = Clock()
    ms.acquire(
        tmp_path / "repoB", "line-2", 4255, berths=1,
        is_live=lambda pid, started: pid != 111, clock=clock, sleep=clock.sleep,
    )
    assert clock.sleeps == 0


def test_a_holder_suspended_by_the_hold_engine_keeps_its_berth(tmp_path):
    # a frozen process is alive: liveness is pid + start, never CPU activity
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)

    class Stop(Exception):
        pass

    def hook(n):
        if n == 5:
            raise Stop

    clock = Clock(on_sleep=hook)
    with pytest.raises(Stop):
        ms.acquire(
            tmp_path / "repoB", "line-2", 4255, berths=1, is_live=alive, clock=clock, sleep=clock.sleep
        )
    assert events() == [("take", "repoA")]
    assert ms.waiting_rows(is_live=alive) == []  # an aborted wait leaves no waiter behind


def _wait_until(predicate, what):
    deadline = time.time() + 10
    while not predicate():
        assert time.time() < deadline, what
        time.sleep(0.005)


def _spawn(results, name, tmp_path, tests, priority, since=None):
    def run():
        kwargs = {"clock": (lambda: since)} if since is not None else {}
        berth = ms.acquire(
            tmp_path / name, name, tests, priority=priority, berths=1, is_live=alive, poll=0.005, **kwargs
        )
        results.append(name)
        berth.release()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_row4_a_priority_run_that_began_waiting_later_starts_first(tmp_path):
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    results = []
    ordinary = _spawn(results, "ordinary", tmp_path, 4255, False)
    _wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 1, "ordinary waits")
    urgent = _spawn(results, "urgent", tmp_path, 4255, True)
    _wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 2, "both wait")
    assert [r["repo"] for r in ms.waiting_rows(is_live=alive)] == ["urgent", "ordinary"]
    first.release()
    for thread in (ordinary, urgent):
        thread.join(10)
    assert results == ["urgent", "ordinary"]
    assert ms.max_simultaneous_holders(ms.log_path()) == 1


def test_same_priority_goes_by_the_time_it_began_waiting(tmp_path):
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    results = []
    late = _spawn(results, "late", tmp_path, 4255, False, since=NOON + 600)
    _wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 1, "late waits")
    early = _spawn(results, "early", tmp_path, 4255, False, since=NOON)
    _wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 2, "both wait")
    first.release()
    for thread in (late, early):
        thread.join(10)
    assert results == ["early", "late"]


def test_row5_zero_berths_means_no_limit_and_records_nothing(tmp_path, mdir):
    clock = Clock()
    a = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=0, is_live=alive, clock=clock, sleep=clock.sleep)
    b = ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=0, is_live=alive, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == 0
    a.release()
    b.release()
    assert not mdir.exists() or not any(mdir.iterdir())
    assert ms.max_simultaneous_holders(ms.log_path()) == 0


def test_two_berths_let_two_run_and_the_third_wait(tmp_path):
    ms.acquire(tmp_path / "repoA", "a", 600, berths=2, is_live=alive)
    ms.acquire(tmp_path / "repoB", "b", 600, berths=2, is_live=alive)
    clock = Clock(on_sleep=lambda n: (_ for _ in ()).throw(KeyboardInterrupt) if n == 2 else None)
    with pytest.raises(KeyboardInterrupt):
        ms.acquire(tmp_path / "repoC", "c", 600, berths=2, is_live=alive, clock=clock, sleep=clock.sleep)
    assert ms.max_simultaneous_holders(ms.log_path()) == 2


def test_taking_is_atomic_across_threads(tmp_path):
    taken = []

    def run(i):
        berth = ms.acquire(tmp_path / f"repo{i}", f"run{i}", 4251, berths=1, is_live=alive, poll=0.002)
        taken.append(i)
        time.sleep(0.005)
        berth.release()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(taken) == list(range(8))
    assert ms.max_simultaneous_holders(ms.log_path()) == 1


def test_a_stale_lock_left_by_a_dead_process_does_not_block(tmp_path, mdir):
    folder = mdir / "heavy_runs"
    folder.mkdir(parents=True)
    (folder / "lock").write_text("999999999")
    old = time.time() - 3600
    os.utime(folder / "lock", (old, old))
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    assert events() == [("take", "repoA")]


def test_max_simultaneous_holders_reads_the_log_only(tmp_path):
    log = tmp_path / "log.jsonl"
    rows = [
        '{"id": "a", "action": "take"}',
        '{"id": "b", "action": "take"}',
        '{"id": "a", "action": "free"}',
        '{"id": "c", "action": "take"}',
    ]
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert ms.max_simultaneous_holders(log) == 2


def test_the_setting_machine_heavy_runs_defaults_to_one():
    assert core_settings.DEFAULTS["machine_heavy_runs"] == 1


def test_two_racers_on_one_stale_lock_never_both_enter(tmp_path, mdir, monkeypatch):
    """Both see the same stale lock; the slow staleness check lets each decide before the other has acted."""
    real_stale = ms._lock_is_stale

    def slow_stale(lock):
        verdict = real_stale(lock)
        time.sleep(0.01)
        return verdict

    monkeypatch.setattr(ms, "_lock_is_stale", slow_stale)
    folder = mdir / "heavy_runs"
    folder.mkdir(parents=True)
    inside, most, guard = [0], [0], threading.Lock()

    def racer():
        with ms._locked():
            with guard:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            time.sleep(0.05)
            with guard:
                inside[0] -= 1

    for _ in range(20):
        (folder / "lock").write_text("999999999")  # a dead process' lock
        threads = [threading.Thread(target=racer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert inside[0] == 0
    assert most[0] == 1
    assert sorted(p.name for p in folder.iterdir()) == []  # no lock, guard or renamed leftover


def test_a_lost_own_waiter_file_is_rewritten_and_the_poll_goes_on(tmp_path, mdir):
    first = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)

    def hook(n):
        if n == 1:  # the berth frees in the same moment the waiter file is lost
            first.release()
            for path in (mdir / "heavy_runs").glob("waiter-*.json"):
                path.unlink()

    clock = Clock(on_sleep=hook)
    second = ms.acquire(
        tmp_path / "repoB", "line-2", 4255, berths=1, is_live=alive, clock=clock, sleep=clock.sleep
    )
    assert clock.sleeps == 2  # the poll that found no file rewrote it; the next one took
    second.release()
    assert events() == [("take", "repoA"), ("free", "repoA"), ("take", "repoB"), ("free", "repoB")]


def test_an_interrupt_right_after_the_take_does_not_keep_the_berth(tmp_path, mdir, monkeypatch):
    real_take = ms._try_take

    def take_then_interrupt(*args, **kwargs):
        real_take(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(ms, "_try_take", take_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=alive)
    monkeypatch.setattr(ms, "_try_take", real_take)
    assert events() == [("take", "repoA"), ("free", "repoA")]
    assert list((mdir / "heavy_runs").glob("*-*.json")) == []
    ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=alive).release()
    assert ms.max_simultaneous_holders(ms.log_path()) == 1
