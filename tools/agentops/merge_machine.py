"""The wiring of the machine berth into a heavy test run (#224): one function, called by the merge suite and by the
line check, so neither needs more than one call around its pytest.

``run_heavy`` calls ``body`` and returns its result. A heavy run (a full suite or more than
``machine_berths.HEAVY_TEST_LIMIT`` selected tests) takes a machine berth first and frees it in a ``finally``, whatever
the body does; a run of 500 tests or fewer calls nothing. The setting ``machine_heavy_runs`` (default 1, 0 = no
limit) says how many heavy runs may hold a berth. A run the hold engine freezes in ``play`` mode is just a body that
does not return: it keeps its berth until it ends.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, TypeVar

from . import machine_berths

T = TypeVar("T")

#: the test count of a full suite whose size is not known (above ``HEAVY_TEST_LIMIT``, so it is heavy).
UNKNOWN_FULL_TESTS = machine_berths.HEAVY_TEST_LIMIT + 1

_SELECTED = re.compile(r"selected (\d+) of (\d+)")
_RECHECK = re.compile(r"re-check after main moved: (\d+) tests")
_PC_ONLY = re.compile(r"pc-only tests: (\d+) files")  # #232: the PC-only files that run beside a GitHub suite


class WaitStopped(Exception):
    """Raised by the sleep of ``wait_hooks`` when its ``stop`` says the wait is over."""


def wait_hooks(
    output_path,
    *,
    log: Callable[[str], None] | None = None,
    alive: Callable[[], None] | None = None,
    stop: Callable[[], bool] | None = None,
    nap: Callable[[float], None] = time.sleep,
) -> tuple[Callable[[str], None], Callable[[float], None]]:
    """``(on_wait, sleep)`` for a heavy run that waits for a berth, shared by the merge suite and the line check.
    ``on_wait(text)`` writes the wait line into the run's output file and to ``log``. ``sleep(seconds)`` is
    what the wait does between two looks: raise ``WaitStopped`` when ``stop()`` is true, call ``alive()`` (a
    merge-lock heartbeat), touch the output file (so a waiter does not read the holder as silent), then ``nap``."""
    output_path = Path(output_path)

    def on_wait(text: str) -> None:
        output_path.write_text(text + "\n", encoding="utf-8")
        if log is not None:
            log(text)

    def sleep(seconds: float) -> None:
        if stop is not None and stop():
            raise WaitStopped()
        if alive is not None:
            alive()
        try:
            os.utime(output_path)
        except OSError:
            pass
        nap(seconds)

    return on_wait, sleep


def configured_berths() -> int:
    """The setting ``machine_heavy_runs``; an unreadable settings file or a bad value is the default, 1."""
    try:
        from . import core_settings
        value = core_settings.load().get("machine_heavy_runs", core_settings.DEFAULTS["machine_heavy_runs"])
        return int(value) if not isinstance(value, bool) else 1
    except Exception:  # noqa: BLE001 -- the berth never breaks a merge
        return 1


def run_size(tests_line: str, cwd) -> tuple[int, bool]:
    """``(tests, full)`` of a run from its selection line (line 1 of the suite output): ``selected N of M`` and
    ``re-check after main moved: N tests`` and ``pc-only tests: N files`` (#232) are a partial run of N; anything
    else is the full suite, whose size is the test map's total when the map is known, else ``UNKNOWN_FULL_TESTS``."""
    line = (tests_line or "").splitlines()[0] if tests_line else ""
    for pattern in (_SELECTED, _RECHECK, _PC_ONLY):
        found = pattern.match(line)
        if found:
            return int(found.group(1)), False
    total = 0
    try:
        from .test_select import map_dir, read_meta
        total = int((read_meta(map_dir(cwd)) or {}).get("total") or 0)
    except Exception:  # noqa: BLE001
        pass
    return (total if total > machine_berths.HEAVY_TEST_LIMIT else UNKNOWN_FULL_TESTS), True


def hold_berth(
    repo,
    run: str,
    tests: int,
    full: bool,
    priority: bool,
    on_wait: Callable[[str], None] | None = None,
    *,
    berths: int | None = None,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> machine_berths.HeavyBerth | None:
    """The berth of a heavy run, taken (blocking until it is free); ``None`` for a run of 500 tests or fewer, which
    takes nothing. The caller frees it with ``release()``. ``repo`` is the repo folder, ``run`` the branch or issue
    name, ``tests`` the number of tests, ``full`` True for a full suite, ``priority`` True for a Priority ticket's
    run. ``on_wait`` gets ``waits for the machine: <repo> runs <N> tests since HH:MM`` once, when the run has to
    wait. ``berths`` overrides the setting (tests); ``sleep`` is called between two looks, so a caller can keep
    itself alive or stop the wait by raising."""
    if not machine_berths.is_heavy(tests, full):
        return None
    return machine_berths.acquire(
        repo, run, tests, priority=priority, berths=configured_berths() if berths is None else berths,
        on_wait=on_wait, poll=poll, sleep=sleep)


def run_heavy(
    repo,
    run: str,
    tests: int,
    full: bool,
    priority: bool,
    body: Callable[[], T],
    on_wait: Callable[[str], None] | None = None,
    *,
    berths: int | None = None,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    hold: Callable[[], None] | None = None,
) -> T:
    """Run ``body()`` holding a machine berth when the run is heavy (``hold_berth``); the berth is freed in a
    ``finally``, whatever the body does. ``hold`` is called once the berth is taken, before the body: it blocks
    while the machine is in ``play`` mode, and the run keeps its berth meanwhile (a held holder keeps it)."""
    berth = hold_berth(repo, run, tests, full, priority, on_wait, berths=berths, poll=poll, sleep=sleep)
    try:
        if hold is not None:
            hold()
        return body()
    finally:
        if berth is not None:
            berth.release()
