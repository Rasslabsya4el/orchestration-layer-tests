"""#142 unit 1: `collect.py serve` is one long-lived collector per machine.

Every `monitor.refresh_s` it computes `fast()` once and writes `CACHE_DIR/collector-fast.json` atomically as
`{"at_ms", "refresh_s", "data"}`; it holds `CACHE_DIR/collector.lock` as `{"pid"}` so a second start exits at once while
the first is alive; a dead or unreadable lock is taken over.

No PoB, no network: `fast` is a counting fake, the config and the cache live in tmp_path. The one subprocess here is a
sleeping child that is started without a console window (CREATE_NO_WINDOW) and killed at the end.

    python -m pytest -q -p no:faulthandler tests/test_issue142_collector.py
"""
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
LOCK = "collector.lock"
OUT = "collector-fast.json"


def _use_root(monkeypatch, tmp_path, monitor=None):
    """A project root in tmp_path: `config/orchestration.json` with the given `monitor` block (or none)."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    body = {"repo": "owner/name"}
    if monitor is not None:
        body["monitor"] = monitor
    (cfg / "orchestration.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(collect, "REPO", tmp_path)
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    return tmp_path / ".cache"


def _set_refresh(tmp_path, value):
    cfg = tmp_path / "config" / "orchestration.json"
    body = json.loads(cfg.read_text(encoding="utf-8"))
    body["monitor"] = {"refresh_s": value}
    cfg.write_text(json.dumps(body), encoding="utf-8")


def _dead_pid() -> int:
    """The pid of a process that has already exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=NO_WINDOW)
    child.wait(timeout=30)
    return child.pid


def _sleeping_child():
    """A live process of ours that sleeps for a while; the caller kills it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], creationflags=NO_WINDOW)


class _Counter:
    """A counting fake `fast`: each call returns a new dict and is recorded."""

    def __init__(self, result=None):
        self.calls = 0
        self.result = result if result is not None else {"at": "x", "n": 1}

    def __call__(self):
        self.calls += 1
        return dict(self.result, n=self.calls)


def _no_sleep(seconds):
    return None


# ---- (1) the loop: fast() once per iteration, the file written, the interval slept


def test_ten_iterations_compute_fast_ten_times(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    fake = _Counter()
    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=10, sleep=_no_sleep)
    assert fake.calls == 10, f"fast() ran {fake.calls} times in 10 iterations"


def test_the_file_holds_at_ms_refresh_s_and_the_fast_result(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    fake = _Counter({"at": "12:00:00", "agents": []})
    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=10, sleep=_no_sleep)
    doc = json.loads((cache / OUT).read_text(encoding="utf-8"))
    assert set(doc) == {"at_ms", "refresh_s", "data"}
    assert isinstance(doc["at_ms"], int) and not isinstance(doc["at_ms"], bool)
    assert doc["refresh_s"] == 1
    assert doc["data"] == {"at": "12:00:00", "agents": [], "n": 10}


def test_at_ms_is_epoch_milliseconds_from_the_clock(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    monkeypatch.setattr(collect, "fast", _Counter())
    collect.serve(iterations=1, sleep=_no_sleep, now=lambda: 1_700_000_000.25)
    doc = json.loads((cache / OUT).read_text(encoding="utf-8"))
    assert doc["at_ms"] == 1_700_000_000_250


def test_the_file_is_never_seen_half_written(tmp_path, monkeypatch):
    """A reader thread parses the file in a loop while serve writes it 10 times: never invalid JSON."""
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    big = {"blob": "x" * 200_000}

    def fake():
        return dict(big)

    monkeypatch.setattr(collect, "fast", fake)
    path = cache / OUT
    bad: list = []
    seen: list = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue  # not there yet, or Windows holds it during the swap: try again
            try:
                seen.append(json.loads(text)["at_ms"])
            except ValueError as exc:
                bad.append(repr(exc))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        collect.serve(iterations=10, sleep=_no_sleep)
    finally:
        stop.set()
        t.join(20)
    assert not bad, f"a reader saw invalid JSON: {bad[:2]}"
    assert seen, "the reader never read the file"


def test_the_sleep_is_the_interval_minus_the_computation_and_never_below_0_2(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    monkeypatch.setattr(collect, "fast", _Counter())
    slept: list = []
    collect.serve(iterations=3, sleep=slept.append)
    # two sleeps between three iterations; each is the interval less a few ms of work, never under 0.2 s
    assert len(slept) == 2
    for s in slept:
        assert 0.2 <= s <= 1.0, f"sleep {s} is not the interval less the computation"


def test_a_slow_computation_leaves_the_floor_of_0_2_seconds(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"refresh_s": 1})

    def slow():
        time.sleep(1.5)  # longer than the interval
        return {"n": 1}

    monkeypatch.setattr(collect, "fast", slow)
    slept: list = []
    collect.serve(iterations=2, sleep=slept.append)
    assert slept == [0.2]


def test_changing_refresh_s_between_iterations_changes_the_sleep(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"refresh_s": 5})
    monkeypatch.setattr(collect, "fast", _Counter())
    slept: list = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 1:
            _set_refresh(tmp_path, 2)  # the owner changes the cadence while the loop runs

    collect.serve(iterations=3, sleep=sleep)
    assert len(slept) == 2
    assert slept[0] > 1.0, f"first sleep {slept[0]} should follow refresh_s 5"
    assert slept[1] <= 2.0 and slept[1] > 1.0, f"second sleep {slept[1]} should follow refresh_s 2"


def test_no_refresh_key_sleeps_the_default_interval(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(collect, "fast", _Counter())
    slept: list = []
    collect.serve(iterations=2, sleep=slept.append)
    doc = json.loads((cache / OUT).read_text(encoding="utf-8"))
    assert doc["refresh_s"] == collect.REFRESH_S_DEFAULT
    assert len(slept) == 1 and 0.2 <= slept[0] <= collect.REFRESH_S_DEFAULT


def test_each_iteration_refreshes_now_so_durations_in_data_move_with_the_clock(tmp_path, monkeypatch):
    """`NOW` is set once at import; a long-lived serve reassigns it at the start of each iteration, so a duration or age
    derived from it in `data` moves on. The fake clock advances a minute per call; the assertion is only that the second
    iteration's NOW is later than the first's."""
    _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    monkeypatch.setattr(collect, "NOW", collect.NOW)  # restored after the test: serve reassigns the module global
    seen: list = []

    def fake():
        seen.append(collect.NOW)
        return {"now": collect.NOW.isoformat()}

    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=2, sleep=_no_sleep, now=itertools.count(1_700_000_000, 60).__next__)
    assert len(seen) == 2
    assert seen[1] > seen[0], f"NOW froze at the collector's start: {seen[0]} then {seen[1]}"


# ---- (2) a fast() that raises: one stderr line, no file for that iteration, the loop goes on


def test_an_exception_in_fast_keeps_the_loop_and_writes_nothing_that_iteration(tmp_path, monkeypatch, capsys):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"n": calls["n"]}

    monkeypatch.setattr(collect, "fast", flaky)
    collect.serve(iterations=2, sleep=_no_sleep)
    assert calls["n"] == 2, "the loop stopped after the exception"
    doc = json.loads((cache / OUT).read_text(encoding="utf-8"))
    assert doc["data"] == {"n": 2}, "the file holds the second iteration, the first wrote nothing"
    err = capsys.readouterr().err
    assert "boom" in err and err.count("\n") >= 1


def test_an_exception_with_no_good_iteration_leaves_no_file(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})

    def broken():
        raise RuntimeError("always")

    monkeypatch.setattr(collect, "fast", broken)
    collect.serve(iterations=3, sleep=_no_sleep)
    assert not (cache / OUT).exists()


def test_the_sleep_still_runs_after_an_exception(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"refresh_s": 1})

    def broken():
        raise RuntimeError("always")

    monkeypatch.setattr(collect, "fast", broken)
    slept: list = []
    collect.serve(iterations=2, sleep=slept.append)
    assert len(slept) == 1 and 0.2 <= slept[0] <= 1.0


def test_a_write_error_keeps_the_loop_and_the_lock(tmp_path, monkeypatch, capsys):
    """A disk-full or locked-file OSError from the write is one stderr line; the loop goes on and the lock stays held."""
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    calls = {"n": 0}
    lock_held: list = []

    def fake():
        calls["n"] += 1
        lock_held.append((cache / LOCK).exists())
        return {"n": calls["n"]}

    def full_disk(doc):
        raise OSError(28, "disk full")

    monkeypatch.setattr(collect, "fast", fake)
    monkeypatch.setattr(collect, "_write_collector_fast", full_disk)
    collect.serve(iterations=2, sleep=_no_sleep)
    assert calls["n"] == 2, "a write error ended the loop"
    assert lock_held == [True, True], "a write error dropped the lock"
    assert "disk full" in capsys.readouterr().err
    assert not (cache / LOCK).exists(), "the lock was not removed on normal exit"


# ---- (3) single instance: collector.lock


def test_a_live_other_pid_in_the_lock_makes_serve_return_without_computing(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    cache.mkdir(parents=True, exist_ok=True)
    child = _sleeping_child()
    try:
        (cache / LOCK).write_text(json.dumps({"pid": child.pid}), encoding="utf-8")
        fake = _Counter()
        monkeypatch.setattr(collect, "fast", fake)
        assert collect.serve(iterations=5, sleep=_no_sleep) is None
        assert fake.calls == 0, "a second collector computed beside a live one"
        assert not (cache / OUT).exists()
        assert json.loads((cache / LOCK).read_text(encoding="utf-8")) == {"pid": child.pid}, \
            "a second start removed the live collector's lock"
    finally:
        child.kill()
        child.wait(timeout=30)


def test_a_dead_pid_in_the_lock_is_taken_over_and_the_lock_is_removed_after(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    cache.mkdir(parents=True, exist_ok=True)
    (cache / LOCK).write_text(json.dumps({"pid": _dead_pid()}), encoding="utf-8")
    seen_lock: list = []

    def fake():
        seen_lock.append(json.loads((cache / LOCK).read_text(encoding="utf-8")))
        return {"n": 1}

    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=2, sleep=_no_sleep)
    assert seen_lock == [{"pid": os.getpid()}, {"pid": os.getpid()}], "the lock did not hold our pid during the loop"
    assert not (cache / LOCK).exists(), "the lock was not removed on normal exit"


def test_an_unreadable_lock_is_taken_over(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    cache.mkdir(parents=True, exist_ok=True)
    (cache / LOCK).write_text("not json {", encoding="utf-8")
    fake = _Counter()
    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=1, sleep=_no_sleep)
    assert fake.calls == 1
    assert not (cache / LOCK).exists()


def test_our_own_pid_in_the_lock_is_not_a_live_other(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    cache.mkdir(parents=True, exist_ok=True)
    (cache / LOCK).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    fake = _Counter()
    monkeypatch.setattr(collect, "fast", fake)
    collect.serve(iterations=1, sleep=_no_sleep)
    assert fake.calls == 1


def test_the_lock_is_removed_when_the_loop_raises(tmp_path, monkeypatch):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})

    def stop_now(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(collect, "fast", _Counter())
    with pytest.raises(KeyboardInterrupt):
        collect.serve(sleep=stop_now)
    assert not (cache / LOCK).exists()


# ---- (4) main() maps `serve` to the loop and prints nothing to stdout


def test_main_serve_runs_the_loop_and_prints_nothing(tmp_path, monkeypatch, capsys):
    cache = _use_root(monkeypatch, tmp_path, {"refresh_s": 1})
    fake = _Counter()
    monkeypatch.setattr(collect, "fast", fake)
    real = collect.serve
    monkeypatch.setattr(collect, "serve", lambda: real(iterations=1, sleep=_no_sleep))
    monkeypatch.setattr(sys, "argv", ["collect.py", "serve"])
    collect.main()
    assert capsys.readouterr().out == ""
    assert fake.calls == 1
    assert (cache / OUT).exists()
