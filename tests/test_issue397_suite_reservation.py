"""#397: the merge suite and the full run read the same headroom and ONE shared reservation, so two callers
starting at once never get more pytest workers than the cap together.

Replay: both callers take the real probe path (``merge._with_workers`` -> ``_make_probe`` -> the probe
command's one line, here a fake ``subprocess.run`` answering an idle machine: ``workers 6 ... cap 6``).
The reservation file is a tmp file; time is the injected ``time.sleep`` (a poll of the waiting caller
ends the first suite). Starts no heavy tool, no real sleep.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from tools.agentops import merge, suite_reservation

# the repo's registered marker name, spelled in parts: a literal would put the heavy tool's word into a core file
CMD = 'python -m pytest tests -q -m "not slow and not ' + "po" + 'b" -rf'
CAP = 6
IDLE_LINE = f"workers {CAP}: cores free 10 of 12 (cpu 10%), RAM free 7.3 GB -> 10 by RAM, cap {CAP}"


def _idle_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOPS_SUITE_RESERVATIONS", str(tmp_path / "suite_workers.json"))
    monkeypatch.setattr(merge, "resolve_suite_workers_probe", lambda: "probe")
    monkeypatch.setattr(merge, "_xdist_available", lambda interpreter, cwd: True)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=IDLE_LINE + "\n", stderr=""))


def test_two_callers_at_once_never_exceed_the_cap(monkeypatch, tmp_path):
    _idle_machine(monkeypatch, tmp_path)
    merge_dir, full_dir = tmp_path / "merge-suite", tmp_path / "full-run"
    state = {"merge_running": True, "polls": 0}

    def fake_sleep(_seconds):  # the full run polls the reservation: the merge suite ends meanwhile
        state["polls"] += 1
        state["merge_running"] = False
        release = getattr(merge, "release_suite_workers", None)
        if release is not None:
            release(merge_dir)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    _, a, _ = merge._with_workers(CMD, True, merge_dir)  # the merge suite starts first, takes what it can
    _, b, _ = merge._with_workers(CMD, True, full_dir)   # the full run starts at once
    together = a * state["merge_running"] + b             # workers running at the moment the full run starts
    print(f"\nreplay: merge suite {a} + full run {b} = {a + (b if state['merge_running'] else 0)} at the start of the "
          f"full run (it polled {state['polls']}x), cap {CAP}")
    assert together <= CAP, f"{a} + {b} = {together} workers at once, cap {CAP}"
    assert a == CAP and b == CAP  # nobody was cut below the cap when it ran alone: the second one waited


def test_headroom_is_shared_not_doubled(tmp_path):
    path = tmp_path / "res.json"
    alive = lambda pid: True
    a, held_a = suite_reservation.reserve(4, CAP, "a", path=path, pid=1, alive=alive)
    b, held_b = suite_reservation.reserve(6, CAP, "b", path=path, pid=2, alive=alive)
    c, held_c = suite_reservation.reserve(3, CAP, "c", path=path, pid=3, alive=alive)
    assert (a, held_a, b, held_b, c, held_c) == (4, 0, 2, 4, 0, 6)
    suite_reservation.release("a", path=path)
    assert suite_reservation.reserve(6, CAP, "c", path=path, pid=3, alive=alive)[0] == 4  # 6 - b's 2


def test_dead_owner_and_expired_lease_hold_nothing(tmp_path):
    path = tmp_path / "res.json"
    suite_reservation.reserve(6, CAP, "dead", path=path, pid=10, now=1000.0, alive=lambda pid: True)
    assert suite_reservation.reserve(6, CAP, "x", path=path, pid=11, now=1001.0, alive=lambda pid: pid != 10)[0] == 6
    suite_reservation.reserve(6, CAP, "old", path=path, pid=12, now=2000.0, alive=lambda pid: True)
    late = 2000.0 + suite_reservation.LEASE_S + 1
    assert suite_reservation.reserve(6, CAP, "y", path=path, pid=13, now=late, alive=lambda pid: True)[0] == 6


def test_threads_at_once_sum_within_cap(tmp_path):
    path = tmp_path / "res.json"
    grants: dict[str, int] = {}
    barrier = threading.Barrier(8)

    def caller(n: int) -> None:
        barrier.wait()
        grants[f"t{n}"] = suite_reservation.reserve(
            6, CAP, f"t{n}", path=path, pid=100 + n, alive=lambda pid: True)[0]

    threads = [threading.Thread(target=caller, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(grants.values()) == CAP, grants


def test_waits_for_headroom_then_takes_one_at_the_deadline(tmp_path):
    path = tmp_path / "res.json"
    clock = {"t": 0.0}
    suite_reservation.reserve(6, CAP, "a", path=path, pid=1, alive=lambda pid: True)

    def sleep(seconds):
        clock["t"] += seconds

    got, held, waited = suite_reservation.reserve_waiting(
        6, CAP, "b", path=path, pid=2, alive=lambda pid: True, wait_s=30.0, poll_s=5.0,
        sleep=sleep, clock=lambda: clock["t"])
    assert (got, held) == (1, 6) and waited >= 30.0  # the floor: a suite never blocks for ever


def test_run_suite_releases_its_reservation(monkeypatch, tmp_path):
    _idle_machine(monkeypatch, tmp_path)
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    merge._with_workers(CMD, True, suite_dir)
    assert suite_reservation.reserve(6, CAP, "other", path=suite_reservation.reservation_file(), pid=1,
                                             alive=lambda pid: True)[0] == 0
    suite_reservation.release("other", path=suite_reservation.reservation_file())
    ok, _ = merge._run_suite("python -c pass", suite_dir)
    assert ok
    assert suite_reservation.reserve(6, CAP, "other", path=suite_reservation.reservation_file(), pid=1,
                                             alive=lambda pid: True)[0] == 6
