"""#224 unit 2: the wiring of the machine berth into the merge suite and the line check (acceptance rows 1, 2, 5)
and the waiters shown by `merge status`."""

import threading
import time

import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms
from tools.agentops import merge_machine as mm
from tools.agentops import merge_status


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


def wait_until(predicate, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Run:
    """A fake heavy run in a thread: records when its body starts, ends when told."""

    def __init__(self, repo, tests, full=False, priority=False, berths=1, fail=False):
        self.started = threading.Event()
        self.go = threading.Event()
        self.done = threading.Event()
        self.waits = []
        self.error = None
        self.fail = fail
        self.thread = threading.Thread(
            target=self._main, args=(repo, tests, full, priority, berths), daemon=True)

    def _body(self):
        self.started.set()
        assert self.go.wait(10)
        if self.fail:
            raise RuntimeError("suite crashed")
        return "result"

    def _main(self, repo, tests, full, priority, berths):
        try:
            self.result = mm.run_heavy(
                repo, "run", tests, full, priority, self._body, on_wait=self.waits.append,
                berths=berths, poll=0.01)
        except RuntimeError as exc:
            self.error = exc
        finally:
            self.done.set()

    def start(self):
        self.thread.start()
        return self


def test_row1_second_heavy_run_starts_only_after_first_frees(tmp_path):
    a = Run(tmp_path / "repoA", 4251, full=True).start()
    assert a.started.wait(10)
    b = Run(tmp_path / "repoB", 4255, full=True).start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    assert not b.started.wait(0.3)  # the first still holds
    assert b.waits and b.waits[0].startswith("waits for the machine: repoB runs 4255 tests since ")
    a.go.set()
    assert b.started.wait(10)
    b.go.set()
    assert a.done.wait(10) and b.done.wait(10)
    assert ms.max_simultaneous_holders() == 1
    assert ms.waiting_rows() == []


def test_wait_line_is_the_exact_text(tmp_path):
    a = Run(tmp_path / "repoA", 600).start()
    assert a.started.wait(10)
    b = Run(tmp_path / "some-repo", 501).start()
    assert wait_until(lambda: b.waits)
    line = b.waits[0]
    prefix = "waits for the machine: some-repo runs 501 tests since "
    assert line.startswith(prefix) and len(line) == len(prefix) + 5 and line[-3] == ":"
    a.go.set()
    b.go.set()
    assert b.done.wait(10)


def test_row2_a_20_test_run_starts_at_once_while_a_heavy_one_holds(tmp_path):
    a = Run(tmp_path / "repoA", 4251, full=True).start()
    assert a.started.wait(10)
    small = Run(tmp_path / "repoB", 20).start()
    assert small.started.wait(2)
    assert small.waits == []
    edge = Run(tmp_path / "repoC", 500).start()
    assert edge.started.wait(2)
    for run in (small, edge, a):
        run.go.set()
    assert a.done.wait(10) and small.done.wait(10) and edge.done.wait(10)
    assert ms.max_simultaneous_holders() == 1  # only the heavy one ever took the berth


def test_row5_berths_zero_both_start_at_once(tmp_path):
    a = Run(tmp_path / "repoA", 4251, full=True, berths=0).start()
    assert a.started.wait(10)
    b = Run(tmp_path / "repoB", 4255, full=True, berths=0).start()
    assert b.started.wait(2)
    assert b.waits == []
    a.go.set()
    b.go.set()
    assert a.done.wait(10) and b.done.wait(10)
    assert not ms.log_path().exists()


def test_the_berth_is_freed_when_the_body_raises(tmp_path):
    a = Run(tmp_path / "repoA", 4251, full=True, fail=True).start()
    assert a.started.wait(10)
    b = Run(tmp_path / "repoB", 4255, full=True).start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    a.go.set()  # a's body raises
    assert a.done.wait(10) and isinstance(a.error, RuntimeError)
    assert b.started.wait(10)
    b.go.set()
    assert b.done.wait(10)
    assert ms.max_simultaneous_holders() == 1


def test_a_body_that_stands_still_keeps_the_berth(tmp_path):
    """The hold engine freezes the run's process (play mode): the body just does not return. The berth stays
    until the body ends; nothing else frees it."""
    a = Run(tmp_path / "repoA", 4251, full=True).start()
    assert a.started.wait(10)
    b = Run(tmp_path / "repoB", 4255, full=True).start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    time.sleep(0.5)  # many polls of b pass while a is frozen
    assert not b.started.is_set()
    holders = ms.holders()
    assert len(holders) == 1 and holders[0]["repo"] == "repoA"
    a.go.set()
    assert b.started.wait(10)
    b.go.set()
    assert b.done.wait(10)


def test_the_berths_come_from_the_setting_machine_heavy_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"machine_heavy_runs": 0})
    assert mm.configured_berths() == 0
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"machine_heavy_runs": 2})
    assert mm.configured_berths() == 2

    def broken(*a, **k):
        raise RuntimeError("missing settings file")
    monkeypatch.setattr(core_settings, "load", broken)
    assert mm.configured_berths() == 1  # unreadable settings: the default


def test_the_test_count_of_a_run_comes_from_its_selection_line(tmp_path):
    assert mm.run_size("selected 20 of 4251 tests: 3 files changed", tmp_path) == (20, False)
    assert mm.run_size("re-check after main moved: 12 tests", tmp_path) == (12, False)
    tests, full = mm.run_size("full suite: no map", tmp_path)
    assert full and tests > ms.HEAVY_TEST_LIMIT
    tests, full = mm.run_size("", tmp_path)
    assert full and tests > ms.HEAVY_TEST_LIMIT


def test_merge_status_shows_each_waiter(tmp_path):
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1)
    waiter = Run(tmp_path / "repoB", 4255, full=True, priority=True).start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    snap = merge_status.collect(tmp_path / "empty-root")
    assert [(w["repo"], w["tests"], w["priority"]) for w in snap["machine_waiting"]] == [("repoB", 4255, True)]
    text = merge_status.render_text(snap)
    assert f"waits for the machine: repoB runs 4255 tests since {snap['machine_waiting'][0]['since']}" in text
    holder.release()
    assert waiter.started.wait(10)
    waiter.go.set()
    assert waiter.done.wait(10)
    assert merge_status.collect(tmp_path / "empty-root")["machine_waiting"] == []
    assert "waits for the machine" not in merge_status.render_text(merge_status.collect(tmp_path / "empty-root"))


# -- the line check ------------------------------------------------------------------------------------

def make_check(tmp_path, name="repoB", priority=False):
    from tools.agentops.line_check import LineCheck
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    return LineCheck(repo, tmp_path / "locks" / "merge.lock", "1-1-1", "branch-b", "sha", "pytest tests",
                     priority=priority, berth_poll=0.01)


def test_a_heavy_line_check_waits_for_the_berth_and_frees_it_at_its_end(tmp_path):
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1)
    chk = make_check(tmp_path)
    out = tmp_path / "check.out"
    result = []
    t = threading.Thread(target=lambda: result.append(chk._take_berth("full suite: no map", out)), daemon=True)
    t.start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    start = "waits for the machine: repoB runs "
    # the waiter row exists before the wait line is written to out (#238): wait for the text, bounded
    assert wait_until(lambda: out.exists() and out.read_text(encoding="utf-8").startswith(start), seconds=10.0)
    assert out.read_text(encoding="utf-8").startswith(start)
    holder.release()
    t.join(10)
    assert result == [True]
    assert out.read_text(encoding="utf-8") == ""  # line 1 of the output is left to the selection line
    assert [rec["repo"] for rec in ms.holders()] == ["repoB"]
    chk._end("green", "")
    assert ms.holders() == []
    assert ms.max_simultaneous_holders() == 1


def test_a_small_line_check_takes_no_berth(tmp_path):
    chk = make_check(tmp_path)
    assert chk._take_berth("selected 20 of 4251 tests", tmp_path / "check.out")
    assert chk._berth is None
    assert not ms.log_path().exists()


def test_stopping_a_waiting_line_check_leaves_no_waiter(tmp_path):
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1)
    chk = make_check(tmp_path)
    result = []
    t = threading.Thread(target=lambda: result.append(chk._take_berth("full suite: no map", tmp_path / "c.out")),
                         daemon=True)
    t.start()
    assert wait_until(lambda: len(ms.waiting_rows()) == 1)
    chk.stop()
    t.join(10)
    assert result == [False]
    assert ms.waiting_rows() == []
    holder.release()
    assert ms.max_simultaneous_holders() == 1


# -- review fixes ----------------------------------------------------------------------------------------

def test_the_hold_is_checked_after_the_berth_is_taken_and_the_berth_is_kept_while_held(tmp_path):
    seen = []

    def hold():  # play mode: waits here for normal, with the berth taken
        seen.append([h["repo"] for h in ms.holders()])

    result = mm.run_heavy(tmp_path / "repoA", "run", 4251, True, False, lambda: seen.append("body") or "ok",
                          hold=hold, berths=1, poll=0.01)
    assert result == "ok"
    assert seen == [["repoA"], "body"]
    assert ms.holders() == []


def test_a_hold_that_raises_frees_the_berth(tmp_path):
    def hold():
        raise RuntimeError("mode unreadable")

    with pytest.raises(RuntimeError):
        mm.run_heavy(tmp_path / "repoA", "run", 4251, True, False, lambda: "never", hold=hold, berths=1, poll=0.01)
    assert ms.holders() == []


def test_wait_hooks_say_the_line_in_the_output_and_the_log_and_stay_alive(tmp_path):
    out = tmp_path / "suite.out"
    logged, beats, naps = [], [], []
    on_wait, sleep = mm.wait_hooks(out, log=logged.append, alive=lambda: beats.append(1), nap=naps.append)
    on_wait("waits for the machine: repoB runs 600 tests since 12:03")
    assert out.read_text(encoding="utf-8") == "waits for the machine: repoB runs 600 tests since 12:03" + chr(10)
    assert logged == ["waits for the machine: repoB runs 600 tests since 12:03"]
    sleep(5.0)
    assert beats == [1] and naps == [5.0]


def test_wait_hooks_stop_raises_the_wait_off(tmp_path):
    stopped = []
    _, sleep = mm.wait_hooks(tmp_path / "suite.out", stop=lambda: bool(stopped), nap=lambda s: None)
    sleep(1.0)
    stopped.append(1)
    with pytest.raises(mm.WaitStopped):
        sleep(1.0)


def test_the_commit_suite_gate_takes_a_berth_for_a_full_suite(tmp_path, monkeypatch):
    from tests.test_prequeue import _fixture
    from tools.agentops import merge

    repo, base = _fixture(tmp_path, with_map=False)
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)
    held = []

    def fake_suite(*args, **kwargs):
        held.append([h["repo"] for h in ms.holders()])
        return merge.SuiteRun(True, "", 1)

    monkeypatch.setattr(merge, "_run_merge_suite", fake_suite)

    class Lock:
        def set_progress(self, path): pass
        def heartbeat(self): pass
        def assert_held(self): pass

    gate = merge._commit_suite_gate(repo, base, "python -m pytest tests", {}, (), tmp_path / "out",
                                    tmp_path / "merge.log", Lock())
    assert gate() is None
    assert held == [[repo.name]]
    assert ms.holders() == []
