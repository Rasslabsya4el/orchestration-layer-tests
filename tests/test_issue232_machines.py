"""#232 unit 3: GitHub machines are counted by the machine-wide line (acceptance row 1, rule 2)."""

import json
import threading
import time

import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms

TOTAL, PER_MERGE = 20, 5


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


NOON = time.mktime((2026, 10, 19, 12, 3, 0, 0, 0, -1))


class Clock:
    """A clock a test moves by hand; `sleep` advances it and runs the test's hook (another merge's action)."""

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


def alive(pid, started):
    return True


def take(tmp_path, repo, want=None, **kw):
    kw.setdefault("is_live", alive)
    per_merge = kw.pop("per_merge", PER_MERGE)
    return ms.acquire_machines(tmp_path / repo, f"merge-{repo}", want, total=TOTAL, per_merge=per_merge, **kw)


def wait_until(predicate, what):
    deadline = time.time() + 10
    while not predicate():
        assert time.time() < deadline, what
        time.sleep(0.005)


def log_rows():
    lines = ms.log_path().read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_settings_defaults_are_20_and_5():
    assert core_settings.DEFAULTS["github_machines_total"] == 20
    assert core_settings.DEFAULTS["github_machines_per_merge"] == 5


def test_row1_five_merges_from_three_repos_four_run_the_fifth_waits_and_starts_when_one_frees(tmp_path):
    first_four = [take(tmp_path, repo) for repo in ("repoA", "repoB", "repoC", "repoA")]
    assert [b.machines for b in first_four] == [5, 5, 5, 5]
    assert ms.machines_in_use(is_live=alive) == 20

    lines, results = [], []

    def fifth():
        berth = take(tmp_path, "repoB", poll=0.005, on_wait=lines.append)
        results.append(berth.machines)
        berth.release()

    thread = threading.Thread(target=fifth)
    thread.start()
    wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 1, "the fifth waits")
    assert results == []
    assert ms.machines_in_use(is_live=alive) == 20
    first_four[1].release()
    thread.join(10)
    assert results == [5]
    for berth in (first_four[0], first_four[2], first_four[3]):
        berth.release()
    assert ms.max_simultaneous_machines(ms.log_path()) == 20
    assert ms.machines_in_use(is_live=alive) == 0
    assert len(lines) == 1 and lines[0].startswith("waits for GitHub machines: repoB wants 5 since ")


def test_wait_line_names_github_machines_and_who_it_waits_for(tmp_path):
    first = take(tmp_path, "repoA", 20, per_merge=20)
    lines = []
    clock = Clock(on_sleep=lambda n: first.release())
    take(tmp_path, "repoB", clock=clock, sleep=clock.sleep, on_wait=lines.append).release()
    assert lines == ["waits for GitHub machines: repoB wants 5 since 12:03, 20 of 20 in use by repoA (20)"]


def test_a_pc_waiter_at_the_head_does_not_block_a_github_waiter_that_finds_machines_free(tmp_path):
    pc_holder = ms.acquire(tmp_path / "repoP", "merge-P", 4251, berths=1, is_live=alive)
    order = []

    def pc_waiter():
        berth = ms.acquire(tmp_path / "repoW", "merge-W", 4251, berths=1, is_live=alive, poll=0.005,
                           clock=lambda: NOON)
        order.append("pc")
        berth.release()

    head = threading.Thread(target=pc_waiter)
    head.start()
    wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 1, "the pc waiter waits")
    berth = take(tmp_path, "repoG", poll=0.005, clock=lambda: NOON + 60)  # behind the PC waiter in the order
    assert berth.machines == 5
    assert order == []  # the PC's slot is still busy; the machines went at once
    assert [r.get("kind", "pc") for r in ms.waiting_rows(is_live=alive)] == ["pc"]
    berth.release()
    pc_holder.release()
    head.join(10)
    assert order == ["pc"]


def test_a_github_waiter_ahead_blocks_a_later_github_waiter_though_machines_are_free(tmp_path):
    for repo, want in (("repoA", 5), ("repoB", 5), ("repoC", 5), ("repoD", 2)):
        take(tmp_path, repo, want)  # 17 in use, 3 free
    # a github waiter wanting 5 that is at the head of the line and has not tried yet
    head = {"id": "0-head", "repo": "repoH", "run": "m", "kind": "github", "tests": 0, "want": 5,
            "priority": False, "since": NOON, "pid": 777, "started": "x"}
    ms.write_json(ms._waiter_path(head["id"]), head)
    dead = set()
    clock = Clock(on_sleep=lambda n: dead.add(777) if n == 4 else None)
    lines = []
    berth = take(tmp_path, "repoG", clock=clock, sleep=clock.sleep, on_wait=lines.append,
                 is_live=lambda pid, started: pid not in dead)
    assert clock.sleeps == 4  # three free the whole time, yet it took none while the head stood ahead
    assert berth.machines == 3  # the head is gone: nobody ahead and 3 free, it takes 3
    assert lines == ["waits for GitHub machines: repoG wants 5 since 12:03, waits behind repoH (5)"]
    assert ms.machines_in_use(is_live=alive) == 20


def test_with_nobody_ahead_and_three_free_the_merge_takes_three(tmp_path):
    take(tmp_path, "repoA", 5)
    take(tmp_path, "repoB", 5)
    take(tmp_path, "repoC", 5)
    take(tmp_path, "repoD", 2)
    clock = Clock()
    berth = take(tmp_path, "repoE", 5, clock=clock, sleep=clock.sleep)
    assert berth.machines == 3
    assert clock.sleeps == 0
    assert ms.machines_in_use(is_live=alive) == 20
    berth.release()
    assert ms.machines_in_use(is_live=alive) == 17


def test_zero_free_waits_even_with_nobody_ahead(tmp_path):
    take(tmp_path, "repoA", 20, per_merge=20)

    class Stop(Exception):
        pass

    def hook(n):
        if n == 3:
            raise Stop

    clock = Clock(on_sleep=hook)
    with pytest.raises(Stop):
        take(tmp_path, "repoB", clock=clock, sleep=clock.sleep)
    assert clock.sleeps == 3
    assert ms.waiting_rows(is_live=alive) == []  # an aborted wait leaves no waiter behind


def test_a_dead_holders_machines_are_freed_at_the_next_try(tmp_path):
    dead = set()
    take(tmp_path, "repoA", 5, pid=111)
    take(tmp_path, "repoB", 5, pid=222)
    take(tmp_path, "repoC", 5, pid=333)
    take(tmp_path, "repoD", 5, pid=444)
    clock = Clock(on_sleep=lambda n: dead.add(222))
    berth = take(tmp_path, "repoE", clock=clock, sleep=clock.sleep, is_live=lambda pid, started: pid not in dead)
    assert clock.sleeps == 1
    assert berth.machines == 5
    freed = [r for r in log_rows() if r["action"] == "free"]
    assert [(r["repo"], r["machines"], r["reason"]) for r in freed] == [("repoB", 5, "holder process dead")]
    assert ms.max_simultaneous_machines(ms.log_path()) == 20


def test_total_zero_means_no_limit_and_records_nothing(tmp_path, mdir):
    clock = Clock()
    berths = [
        ms.acquire_machines(tmp_path / f"repo{i}", f"m{i}", None, total=0, per_merge=5, is_live=alive,
                            clock=clock, sleep=clock.sleep)
        for i in range(6)
    ]
    assert clock.sleeps == 0
    assert [b.machines for b in berths] == [5] * 6  # the parts the caller splits into
    for berth in berths:
        berth.release()
    assert not mdir.exists() or not any(mdir.iterdir())
    assert ms.max_simultaneous_machines(ms.log_path()) == 0
    fewer = ms.acquire_machines(tmp_path / "repoX", "mx", 3, total=0, per_merge=5, is_live=alive)
    cap = ms.acquire_machines(tmp_path / "repoX", "mx", 9, total=0, per_merge=5, is_live=alive)
    assert (fewer.machines, cap.machines) == (3, 5)


def test_machines_and_the_pc_slot_are_counted_apart_in_one_folder_and_one_log(tmp_path):
    pc = ms.acquire(tmp_path / "repoP", "merge-P", 4251, berths=1, is_live=alive)
    a = take(tmp_path, "repoA", 5)
    assert ms.machines_in_use(is_live=alive) == 5
    rows = log_rows()
    assert [(r["kind"], r["action"]) for r in rows] == [("pc", "take"), ("github", "take")]
    assert rows[1]["machines"] == 5
    a.release()
    pc.release()
    assert ms.max_simultaneous_holders(ms.log_path()) == 1  # the PC's slot only
    assert ms.max_simultaneous_machines(ms.log_path()) == 5  # machines only


def test_max_simultaneous_machines_reads_the_log_only(tmp_path):
    log = tmp_path / "log.jsonl"
    rows = [
        '{"id": "a", "action": "take", "kind": "github", "machines": 5}',
        '{"id": "p", "action": "take", "kind": "pc"}',
        '{"id": "b", "action": "take", "kind": "github", "machines": 3}',
        '{"id": "a", "action": "free", "kind": "github", "machines": 5}',
        '{"id": "c", "action": "take", "kind": "github", "machines": 4}',
    ]
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert ms.max_simultaneous_machines(log) == 8
    assert ms.max_simultaneous_machines(tmp_path / "missing.jsonl") == 0


def test_priority_merge_for_machines_goes_before_an_earlier_ordinary_one(tmp_path):
    first = take(tmp_path, "repoA", 20, per_merge=20)
    results = []

    def spawn(repo, priority, since):
        def run():
            berth = take(tmp_path, repo, poll=0.005, priority=priority, clock=lambda: since)
            results.append(repo)
            time.sleep(0.05)
            berth.release()

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    ordinary = spawn("ordinary", False, NOON)
    wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 1, "ordinary waits")
    urgent = spawn("urgent", True, NOON + 600)
    wait_until(lambda: len(ms.waiting_rows(is_live=alive)) == 2, "both wait")
    first.release()
    for thread in (ordinary, urgent):
        thread.join(10)
    assert results == ["urgent", "ordinary"]


def test_many_merges_at_once_never_hold_more_than_the_total(tmp_path):
    done = []

    def run(i):
        berth = take(tmp_path, f"repo{i % 3}", poll=0.002)
        time.sleep(0.01)
        done.append(berth.machines)
        berth.release()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(done) == 10
    assert 0 < ms.max_simultaneous_machines(ms.log_path()) <= 20


def test_merge_status_shows_a_github_waiter_and_leaves_the_pc_row_as_it_was(tmp_path, monkeypatch):
    from tools.agentops import merge_status

    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"github_machines_total": 20})
    holder = take(tmp_path, "repoA", 20, per_merge=20, is_live=ms.holder_is_live)
    pc_holder = ms.acquire(tmp_path / "repoP", "merge-P", 4251, berths=1)
    stop = []

    def nap(seconds):
        time.sleep(seconds)
        if stop:
            raise KeyboardInterrupt

    def github():
        with pytest.raises(KeyboardInterrupt):
            take(tmp_path, "repoB", poll=0.005, is_live=ms.holder_is_live, sleep=nap)

    def pc():
        with pytest.raises(KeyboardInterrupt):
            ms.acquire(tmp_path / "repoC", "merge-C", 4255, berths=1, poll=0.005, sleep=nap)

    threads = [threading.Thread(target=github), threading.Thread(target=pc)]
    for t in threads:
        t.start()
    wait_until(lambda: len(ms.waiting_rows()) == 2, "both wait")
    snap = merge_status.collect(tmp_path / "empty-root")
    text = merge_status.render_text(snap)
    rows = {r["repo"]: r for r in snap["machine_waiting"]}
    assert (snap["machines_in_use"], snap["machines_total"]) == (20, 20)
    assert f"waits for GitHub machines: repoB wants 5 since {rows['repoB']['since']}, 20 of 20 in use" in text
    assert f"waits for the machine: repoC runs 4255 tests since {rows['repoC']['since']}" in text
    stop.append(1)
    for t in threads:
        t.join(10)
    holder.release()
    pc_holder.release()
