"""#223: whether a failed merge test ran on a loaded machine, and the one-word verdict that says so.

A reading is the machine's CPU % over the last 60 s and the other pytest sessions live on the machine (counted by
process, any repo; a run is its root process, never its xdist workers or children). A reading is loaded when the CPU
is at least 85 % or two or more other runs are live. It is quiet when the CPU is under 85 % and no heavy run is
live. A run is light only when its command line names 1 to 50 explicit node ids (`path::test`) and no bare file or
folder; any other run is heavy (a cheap stand-in for "more than 50 tests"). The verdict is `load`, `code` or
`unclear` (see `verdict`).
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

CPU_LOADED = 85.0
RUNS_LOADED = 2
CPU_WINDOW_SECONDS = 60.0
CPU_SAMPLE_SECONDS = 5.0
POLL_SECONDS = 30.0
MAX_WAIT_SECONDS = 600.0
MAX_LIGHT_NODE_IDS = 50
LOAD, CODE, UNCLEAR = "load", "code", "unclear"
RE_CHECK_UNDER_LOAD = "re-check under load"
NOT_RUN = "not-run"  # the reason of an id the re-check did not run
_PYTEST_SCRIPTS = ("pytest", "pytest.exe")
_CANDIDATE_PREFIXES = ("python", "pytest", "py", "uv", "pipx")
# pytest options that take the next word as their value (so it is not a path)
_VALUE_OPTIONS = frozenset({"-n", "-k", "-m", "-p", "-c", "-o", "-W", "--maxfail", "--rootdir", "--basetemp",
                            "--junit-xml", "--junitxml"})


def _busy(cpu_percent: float | None) -> bool:
    return cpu_percent is not None and cpu_percent >= CPU_LOADED


@dataclass(frozen=True)
class LoadReading:
    cpu_percent: float | None
    runs: int
    at: float
    heavy_runs: int | None = None  # None: every other run is heavy
    loaded: bool = field(init=False)

    def __post_init__(self) -> None:
        heavy = self.runs if self.heavy_runs is None else self.heavy_runs
        object.__setattr__(self, "heavy_runs", heavy)
        object.__setattr__(self, "loaded", _busy(self.cpu_percent) or self.runs >= RUNS_LOADED)

    @property
    def quiet(self) -> bool:
        return self.cpu_percent is not None and not _busy(self.cpu_percent) and self.heavy_runs == 0


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    cmdline: tuple[str, ...]


class CpuSampler:
    """Machine CPU % over the last `window` seconds, from samples kept by a daemon thread every `interval` seconds
    (start, stop). A caller that reads rarely still gets the last window, not the time since its last read. A
    reading needs two samples, so before start() it answers None.

    `own_busy` (optional) returns the cumulative CPU seconds of the caller's own process tree; the reading is then
    the busy share of the others only: the machine's busy seconds in the window minus the own tree's, clamped at 0.
    A merge's own suite fills the CPU after a red run, so without it every failed test would read as loaded."""

    def __init__(self, sample: Callable[[], tuple[float, float] | None] | None = None,
                 clock: Callable[[], float] = time.monotonic, interval: float = CPU_SAMPLE_SECONDS,
                 window: float = CPU_WINDOW_SECONDS, own_busy: Callable[[], float] | None = None):
        self._sample = sample or _psutil_cpu_times
        self.own_busy = own_busy
        self._clock = clock
        self._interval = interval
        self._window = window
        self._samples: deque[tuple[float, float, float, float]] = deque()  # (time, total, idle, own busy)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        """Takes one sample and drops the ones that fell out of the window (the newest one at or before its start stays
        as the base)."""
        cur = self._sample()
        if cur is None:
            return
        now = self._clock()
        own = 0.0
        if self.own_busy is not None:
            try:
                own = float(self.own_busy())
            except Exception:  # noqa: BLE001 -- an unreadable own tree counts as nothing own, never hurts the merge
                own = self._samples[-1][3] if self._samples else 0.0
        with self._lock:
            self._samples.append((now, cur[0], cur[1], own))
            start = now - self._window
            while len(self._samples) > 1 and self._samples[1][0] <= start:
                self._samples.popleft()

    def read(self) -> float | None:
        with self._lock:
            if len(self._samples) < 2:
                return None
            _, base_total, base_idle, base_own = self._samples[0]
            _, total, idle, own = self._samples[-1]
        span = total - base_total
        if span <= 0:
            return None
        busy = span - (idle - base_idle) - (own - base_own)
        return max(0.0, min(100.0, 100.0 * busy / span))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.tick()  # a first sample now, so the first read is not empty
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cpu-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Ends the thread; True when it has ended within `timeout` seconds."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()


def _psutil_cpu_times() -> tuple[float, float] | None:
    import psutil

    t = psutil.cpu_times()
    return float(sum(t)), float(t.idle)


MACHINE_CPU = CpuSampler()  # the suite starts it (`MACHINE_CPU.start()`); read_load reads it


def process_table(pids: Iterable[int] | None = None, open_process: Callable | None = None) -> list[ProcInfo]:
    """Every live process by pid, read one pid at a time (psutil.process_iter skips some live pids). The command
    line is read only for a process whose name starts with a launcher or interpreter name (python, pytest, py, uv,
    pipx); a pid that vanishes is left out."""
    import psutil

    opener = open_process or psutil.Process
    out = []
    for pid in (psutil.pids() if pids is None else pids):
        try:
            proc = opener(pid)
            name = proc.name()
            ppid = proc.ppid()
        except psutil.Error:
            continue
        cmdline: tuple[str, ...] = ()
        if name.lower().removesuffix(".exe").startswith(_CANDIDATE_PREFIXES):
            try:
                cmdline = tuple(proc.cmdline())
            except psutil.Error:
                pass
        out.append(ProcInfo(pid, ppid or 0, cmdline))
    return out


def _pytest_args(cmdline: tuple[str, ...]) -> list[str] | None:
    """The words after pytest's own name, or None when the command line is not a pytest run."""
    for i in range(1, len(cmdline)):
        if cmdline[i - 1] == "-m" and cmdline[i] == "pytest":
            return list(cmdline[i + 1:])
    if cmdline and os.path.basename(cmdline[0]).lower() in _PYTEST_SCRIPTS:
        return list(cmdline[1:])
    if len(cmdline) >= 2 and os.path.basename(cmdline[1]).lower() in _PYTEST_SCRIPTS:
        return list(cmdline[2:])
    return None


def _is_heavy(cmdline: tuple[str, ...]) -> bool:
    args = _pytest_args(cmdline) or []
    positional: list[str] = []
    skip_value = False
    for word in args:
        if skip_value:
            skip_value = False
        elif word in _VALUE_OPTIONS:
            skip_value = True
        elif not word.startswith("-"):
            positional.append(word)
    light = 0 < len(positional) <= MAX_LIGHT_NODE_IDS and all("::" in word for word in positional)
    return not light


def _runs_of_others(own_pid: int, table: dict[int, ProcInfo]) -> list[int]:
    """The root pids of the pytest sessions other than the caller's: a pytest process with no pytest ancestor, and
    neither the caller, nor its ancestors, nor anything under the caller."""
    excluded: set[int] = set()
    pid = own_pid
    while pid and pid not in excluded:
        excluded.add(pid)
        if pid not in table:
            break
        pid = table[pid].ppid
    children: dict[int, list[int]] = {}
    for p in table.values():
        children.setdefault(p.ppid, []).append(p.pid)
    frontier = [own_pid]
    while frontier:
        for kid in children.get(frontier.pop(), []):
            if kid not in excluded:
                excluded.add(kid)
                frontier.append(kid)
    roots = []
    for p in table.values():
        if p.pid in excluded or _pytest_args(p.cmdline) is None or _is_under_counted_run(p, table, excluded):
            continue
        roots.append(p.pid)
    return sorted(roots)


def _is_under_counted_run(proc: ProcInfo, table: dict[int, ProcInfo], excluded: set[int]) -> bool:
    """True when the nearest pytest ancestor is another run that is counted (not the caller's own tree)."""
    seen = {proc.pid}
    parent = table.get(proc.ppid)
    while parent is not None and parent.pid not in seen:
        if _pytest_args(parent.cmdline) is not None:
            return parent.pid not in excluded
        seen.add(parent.pid)
        parent = table.get(parent.ppid)
    return False


def _table(procs: Iterable[ProcInfo] | None) -> dict[int, ProcInfo]:
    return {p.pid: p for p in (process_table() if procs is None else procs)}


def other_pytest_run_pids(own_pid: int, procs: Iterable[ProcInfo] | None = None) -> list[int]:
    return _runs_of_others(own_pid, _table(procs))


def heavy_run_pids(own_pid: int, procs: Iterable[ProcInfo] | None = None) -> list[int]:
    table = _table(procs)
    return [pid for pid in _runs_of_others(own_pid, table) if _is_heavy(table[pid].cmdline)]


def count_other_pytest_sessions(own_pid: int, procs: Iterable[ProcInfo] | None = None) -> int:
    return len(other_pytest_run_pids(own_pid, procs))


def read_load(own_pid: int, *, cpu: Callable[[], float | None] | None = None,
              procs: Iterable[ProcInfo] | None = None, clock: Callable[[], float] = time.time) -> LoadReading:
    table = _table(procs)
    runs = _runs_of_others(own_pid, table)
    heavy = [pid for pid in runs if _is_heavy(table[pid].cmdline)]
    cpu_percent = cpu() if cpu is not None else MACHINE_CPU.read()
    return LoadReading(cpu_percent=cpu_percent, runs=len(runs), heavy_runs=len(heavy), at=clock())


class QuietWait:
    """The one wait for a quiet machine, as a stepper (a caller that must not block calls `step` from its own loop).
    `step(read)` reads at most once per `poll_seconds` and answers None while the machine is busy, else
    `(reading, '')` once it is quiet or `(reading, 're-check under load')` when `max_wait` seconds of busy readings
    have passed. `remaining()` is how long is left until the next reading is due."""

    def __init__(self, clock: Callable[[], float], poll_seconds: float = POLL_SECONDS,
                 max_wait: float = MAX_WAIT_SECONDS):
        self._clock = clock
        self._poll = poll_seconds
        self._max = max_wait
        self._start = clock()
        self.next_at = self._start

    def step(self, read: Callable[[], LoadReading]) -> tuple[LoadReading, str] | None:
        if self._clock() < self.next_at:
            return None
        reading = read()
        if reading.quiet:
            return reading, ""
        remaining = self._max - (self._clock() - self._start)
        if remaining <= 0:
            return reading, RE_CHECK_UNDER_LOAD
        self.next_at = self._clock() + min(self._poll, remaining)
        return None

    def remaining(self) -> float:
        return max(0.0, self.next_at - self._clock())


def wait_until_quiet(read: Callable[[], LoadReading], clock: Callable[[], float], sleep: Callable[[float], None],
                     poll_seconds: float = POLL_SECONDS,
                     max_wait: float = MAX_WAIT_SECONDS) -> tuple[LoadReading, str]:
    """Polls `read` until the machine is quiet; returns (last reading, '') or, after `max_wait` seconds of busy
    readings, (that reading, 're-check under load') at exactly `max_wait`."""
    wait = QuietWait(clock, poll_seconds, max_wait)
    while True:
        answer = wait.step(read)
        if answer is not None:
            return answer
        left = wait.remaining()  # a sleep that returned early is not repeated in full: only what is left
        if left > 0:
            sleep(left)


def verdict(failure_reading: LoadReading, recheck_passed: bool, recheck_reading: LoadReading) -> str:
    """The word of one failed test. A re-check that did not run on a quiet machine tells nothing either way
    (`unclear`). On a quiet one: failed again is `code`; passed is `load` when the failure was read loaded, `unclear`
    when it was not or when its CPU could not be read."""
    if not recheck_reading.quiet:
        return UNCLEAR
    if not recheck_passed:
        return CODE
    if failure_reading.cpu_percent is None or not failure_reading.loaded:
        return UNCLEAR
    return LOAD


def failfast_verdict(failure_reading: LoadReading, confirm_reading: LoadReading) -> str:
    """The word of a test the #219 fail-fast confirmed (it failed again alone, so the confirm is the re-check and
    there is no third run): the failed-again case of `verdict`."""
    return verdict(failure_reading, False, confirm_reading)


def _describe(reading: LoadReading) -> str:
    cpu = "others' CPU n/a" if reading.cpu_percent is None else f"others' CPU {reading.cpu_percent:.0f}%"
    return f"{cpu}, {reading.runs} run{'' if reading.runs == 1 else 's'}"


def human_line(test_id: str, reading: LoadReading, word: str, recheck_reading: LoadReading | None = None,
               reason: str = "") -> str:
    """The one human format of a verdict (merge.log, the suite output, the record's `line`):
    ``verdict <id>: <word> (others' CPU N%, M runs; re-check others' CPU N%, M runs)`` (the CPU of the machine
    without the merge's own process tree); a test that was not re-checked ends with its
    one-word reason instead of the re-check."""
    detail = _describe(reading)
    if recheck_reading is not None:
        detail += f"; re-check {_describe(recheck_reading)}"
    elif reason:
        detail += f"; {reason}"
    return f"verdict {test_id}: {word} ({detail})"


def _numbers(cpu_percent: float | None, runs: int) -> LoadReading:
    return LoadReading(cpu_percent=cpu_percent, runs=runs, at=0.0)


def record(test_id: str, failure_reading: LoadReading, recheck_reading: LoadReading | None, word: str,
           reason: str = "") -> dict:
    if word not in (LOAD, CODE, UNCLEAR):
        raise ValueError(f"not a verdict word: {word!r}")
    rec = {
        "test": test_id,
        "verdict": word,
        "cpu_percent": failure_reading.cpu_percent,
        "runs": failure_reading.runs,
        "loaded": failure_reading.loaded,
        "at": failure_reading.at,
        "recheck": None if recheck_reading is None else {
            "cpu_percent": recheck_reading.cpu_percent,
            "runs": recheck_reading.runs,
            "loaded": recheck_reading.loaded,
            "at": recheck_reading.at,
        },
        "reason": reason,
    }
    rec["line"] = verdict_line(rec)
    return rec


def unjudged(test_id: str, reason: str, failure_reading: LoadReading | None = None) -> dict:
    """A failed test that no re-check judged (the run was stuck, a PC-only or mod test, the re-check could not tell,
    the re-check did not run it): `unclear` with a one-word reason, never `load`."""
    reading = failure_reading if failure_reading is not None else LoadReading(cpu_percent=None, runs=0, at=time.time())
    return record(test_id, reading, None, UNCLEAR, reason)


def verdict_line(rec: dict) -> str:
    """The merge.log line and the suite-output line of one record (see `human_line`)."""
    again = rec["recheck"]
    return human_line(rec["test"], _numbers(rec["cpu_percent"], rec["runs"]), rec["verdict"],
                      None if again is None else _numbers(again["cpu_percent"], again["runs"]),
                      rec.get("reason", ""))


def verdict_summary(rec: dict) -> dict:
    """One record as the result JSON holds it: the word and the raw numbers (and the reason of an unjudged test)."""
    again = rec["recheck"] or {"cpu_percent": None, "runs": None}
    out = {"word": rec["verdict"], "cpu_percent": rec["cpu_percent"], "runs": rec["runs"],
           "recheck_cpu_percent": again["cpu_percent"], "recheck_runs": again["runs"]}
    if rec.get("reason"):
        out["reason"] = rec["reason"]
    return out


def judged_recheck(failed_ids: list[str], recheck: Callable[[list[str]], list[str]], *,
                   read_failure: Callable[[], LoadReading],
                   wait_quiet: Callable[[], tuple[LoadReading, str]],
                   now_note: str = RE_CHECK_UNDER_LOAD,
                   not_run: Callable[[], Iterable[str]] | None = None,
                   failure_readings: dict[str, LoadReading] | None = None) -> tuple[list[str], dict[str, dict], str]:
    """#223: the failed ids re-checked once the machine is quiet, each with its verdict.

    Takes the failure reading (`read_failure`), waits (`wait_quiet`: the reading it ends on and, when the wait hit
    its cap, a non-empty note), runs `recheck(ids)` (the ids that still fail), and gives
    `(still_failing_ids, {id: record(...)}, note)`; `note` is `now_note` when the wait hit the cap, else ''. The
    machine reading the wait ends on is the re-check's reading. A test that fails again stays failing: verdicts only add
    information. `not_run` (called after the re-check) names the ids the re-check did not run (their test file is
    gone, pytest could not find them): they are not "passed on re-check", they are `unclear` with the reason
    `not-run`. `failure_readings` holds the reading taken when an id's failure first showed; an id without one is
    judged on `read_failure()`. A `recheck` that raises raises here, before any verdict."""
    failure = read_failure()
    early = failure_readings or {}
    quiet, wait_note = wait_quiet()
    still = list(recheck(list(failed_ids)))
    skipped = set(not_run()) if not_run is not None else set()
    verdicts = {i: unjudged(i, NOT_RUN, early.get(i, failure)) if i in skipped
                else record(i, early.get(i, failure), quiet, verdict(early.get(i, failure), i not in still, quiet))
                for i in failed_ids}
    return still, verdicts, now_note if wait_note else ""
