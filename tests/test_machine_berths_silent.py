"""#249: a live PC holder whose output has gone silent is ended, so the head waiter takes the berth (acceptance rows 1-4).

Seams: `machine_berths.acquire` with a fake clock and a fake liveness (`pid_exists`); the holder is a real short-lived
process tree (sys.executable sleep), stopped by the default tree killer, and every such process is cleaned up after."""

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms

NOON = time.mktime((2026, 10, 19, 12, 0, 0, 0, 0, -1))

SLEEPER = (
    "import subprocess, sys, time\n"
    "f = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], creationflags=f)\n"
    "time.sleep(600)\n"
)

_spawned = []


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    # the settings file of the repo is not read: the defaults (silent 15 min) hold
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {})
    yield path
    for proc in _spawned:
        try:
            proc.kill()
            proc.wait(10)
        except Exception:  # noqa: BLE001 -- cleanup only
            pass
    _spawned.clear()


class Clock:
    def __init__(self, start=NOON):
        self.t = start
        self.sleeps = 0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds


class Stop(Exception):
    pass


def live(pid, started):
    """Liveness of a fake or real holder: its pid exists."""
    return psutil.pid_exists(pid)


def _start_tree():
    """A real holder process with one child process, both sleeping; no console window."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-c", SLEEPER], creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _spawned.append(proc)
    deadline = time.time() + 20
    while not psutil.Process(proc.pid).children():
        assert time.time() < deadline, "the holder's child never started"
        time.sleep(0.05)
    return proc


def _tree_of(pid):
    root = psutil.Process(pid)
    return [root] + root.children(recursive=True)


def _set_mtime(path, epoch):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("suite output\n", encoding="utf-8")
    os.utime(path, (epoch, epoch))


def _log_rows(mdir):
    lines = (mdir / "heavy_runs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _free_reasons(mdir):
    return [row.get("reason") for row in _log_rows(mdir) if row["action"] == "free"]


def _write_play_mode(mdir):
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "machine_mode.json").write_text(
        json.dumps({"mode": "play", "since": "2026-10-19T12:00:00+00:00"}), encoding="utf-8"
    )


def test_the_setting_berth_silent_max_minutes_defaults_to_15():
    assert core_settings.DEFAULTS["berth_silent_max_minutes"] == 15


def test_a_holder_record_carries_the_out_path_it_writes_to(tmp_path, mdir):
    out = tmp_path / "repoA" / "out.txt"
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, out_path=out)
    record = json.loads((mdir / "heavy_runs" / f"holder-{holder._record['id']}.json").read_text(encoding="utf-8"))
    assert record["out_path"] == str(out)
    holder.release()


def test_row1_silent_holder_is_ended_and_the_head_waiter_takes_the_berth(tmp_path, mdir):
    out = tmp_path / "repoA" / "out.txt"
    _set_mtime(out, NOON - 16 * 60)  # output untouched for 16 min, setting 15
    run = _start_tree()
    tree = _tree_of(run.pid)
    clock = Clock()
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, pid=run.pid, out_path=out,
               clock=Clock(NOON - 16 * 60))  # taken 16 min ago, silent since

    waiter = ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=live, clock=clock, sleep=clock.sleep)

    assert clock.sleeps <= 1  # freed within one poll
    gone, alive = psutil.wait_procs(tree, timeout=10)
    assert alive == []  # the holder's whole tree is stopped
    assert "holder silent 16 min" in _free_reasons(mdir)
    assert [row["action"] for row in _log_rows(mdir)] == ["take", "free", "take"]
    waiter.release()


def test_row2_a_holder_frozen_by_the_hold_engine_keeps_its_berth(tmp_path, mdir):
    out = tmp_path / "repoA" / "out.txt"
    _set_mtime(out, NOON - 16 * 60)
    run = _start_tree()
    tree = _tree_of(run.pid)
    _write_play_mode(mdir)
    hold_record = tmp_path / "repoA" / "var" / "agentops" / "hold_record.json"
    hold_record.parent.mkdir(parents=True, exist_ok=True)
    hold_record.write_text(json.dumps([{"pid": run.pid, "create_time": 0.0}]), encoding="utf-8")
    clock = Clock()
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, pid=run.pid, out_path=out, clock=clock)

    polls = [0]

    def sleep(seconds):
        polls[0] += 1
        clock.t += seconds
        if polls[0] == 3:
            raise Stop

    with pytest.raises(Stop):
        ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=live, clock=clock, sleep=sleep)

    assert psutil.Process(run.pid).is_running()  # nothing stopped
    _gone, alive = psutil.wait_procs(tree, timeout=0.5)
    assert {p.pid for p in alive} == {p.pid for p in tree}
    assert (mdir / "heavy_runs" / f"holder-{_holder_id(mdir)}.json").exists()  # the berth is kept
    assert _free_reasons(mdir) == []


def test_row3_output_grew_a_minute_ago_keeps_a_holder_held_50_min(tmp_path, mdir):
    out = tmp_path / "repoA" / "out.txt"
    _set_mtime(out, NOON - 60)  # grew 1 min ago
    run = _start_tree()
    tree = _tree_of(run.pid)
    holder_clock = Clock(start=NOON - 50 * 60)  # taken 50 min ago
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, pid=run.pid, out_path=out,
               clock=holder_clock)

    polls = [0]

    def sleep(seconds):
        polls[0] += 1
        if polls[0] == 3:
            raise Stop

    with pytest.raises(Stop):
        ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=live, clock=Clock(), sleep=sleep)

    _gone, alive = psutil.wait_procs(tree, timeout=0.5)
    assert {p.pid for p in alive} == {p.pid for p in tree}
    assert _free_reasons(mdir) == []


def test_row4_a_record_without_out_path_held_3h1min_is_freed_by_held_time(tmp_path, mdir):
    run = _start_tree()
    tree = _tree_of(run.pid)
    holder_clock = Clock(start=NOON - (3 * 60 + 1) * 60)
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, pid=run.pid, clock=holder_clock)

    waiter = ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=live, clock=Clock(), sleep=Clock().sleep)

    gone, alive = psutil.wait_procs(tree, timeout=10)
    assert alive == []
    assert "holder held 3 h 1 min, no output path" in _free_reasons(mdir)
    waiter.release()


def _holder_id(mdir):
    names = sorted(p.name for p in (mdir / "heavy_runs").glob("holder-*.json"))
    assert len(names) == 1, names
    return names[0][len("holder-"):-len(".json")]


def test_the_merge_suite_holder_carries_its_output_path(monkeypatch, tmp_path):
    """#249 rule 1: the merge's heavy suite hands its output file to the berth, so it is judged by silence."""
    from tools.agentops import merge as m
    seen = {}
    monkeypatch.setattr(m.merge_machine, "run_size", lambda line, cwd: (900, False))
    monkeypatch.setattr(m.merge_machine, "wait_hooks", lambda *a, **k: (None, lambda s: None))
    monkeypatch.setattr(m.merge_machine, "run_heavy", lambda *a, **k: seen.update(k) or "ran")

    class Lock:
        def heartbeat(self):
            return None

    out = tmp_path / "suite.out"
    assert m._run_heavy_suite(tmp_path, "b", "900 tests", False, lambda: None, out, tmp_path / "log", Lock()) == "ran"
    assert seen["out_path"] == out


def test_play_time_is_not_silence(tmp_path, mdir, monkeypatch):
    """GLOSSARY 'Machine mode': stood time is subtracted by every watchdog. 16 min since the last write, 10 of them
    stood in play -> 6 min of silence, the holder keeps its berth."""
    monkeypatch.setattr(ms, "stood_between", lambda start, end: 10 * 60.0)
    out = tmp_path / "repoA" / "out.txt"
    _set_mtime(out, NOON - 16 * 60)
    run = _start_tree()
    tree = _tree_of(run.pid)
    clock = Clock()
    ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1, is_live=live, pid=run.pid, out_path=out, clock=clock)
    polls = [0]

    def sleep(seconds):
        polls[0] += 1
        clock.t += seconds
        if polls[0] == 3:
            raise Stop

    with pytest.raises(Stop):
        ms.acquire(tmp_path / "repoB", "line-2", 4255, berths=1, is_live=live, clock=clock, sleep=sleep)
    _gone, alive = psutil.wait_procs(tree, timeout=0.5)
    assert {p.pid for p in alive} == {p.pid for p in tree}
    assert _free_reasons(mdir) == []


def test_a_holder_taken_a_minute_ago_is_not_silent_for_an_older_output_file(tmp_path):
    """Review of #249: silence counts from the later of the take and the last write, not from a stale file of an earlier run."""
    out = tmp_path / "old.out"
    out.write_text("x", encoding="utf-8")
    os.utime(out, (NOON - 3600, NOON - 3600))
    record = {"taken": NOON - 60, "out_path": str(out)}
    assert ms._silent_reason(record, NOON, 15 * 60) is None
    record["taken"] = NOON - 3600 + 1  # taken long ago, file untouched since: still silent
    assert ms._silent_reason(record, NOON, 15 * 60) == "holder silent 59 min"
