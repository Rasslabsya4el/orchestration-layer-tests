"""The check of ONE branch that waits in the merge line (#765, unit 1): while a holder runs its suite,
the tests this branch's change can affect run on base + this branch alone, so a branch that fails them
leaves the line before it costs a batch.

``LineCheck`` is a non-blocking state machine the waiter's own process polls from its wait loop::

    chk = LineCheck(cwd, lock_path, ticket, branch, target, suite_cmd,
                    suite_env=..., suite_shared=..., remote="origin", main="main")
    outcome = chk.poll(holder_live=True)   # a CheckOutcome(red=True, ...) once, else None
    chk.should_check(holder_live)          # False with no live holder: poll starts nothing
    chk.stop()                             # kill the child tree, free the seat, status "stopped"
    chk.cleanup()                          # stop() + remove this ticket's status file
    chk.state                              # idle waiting preparing running green red stopped skipped

Rules it keeps:

- The check runs ``suite_cmd`` on the merge commit of the local ``main`` head and ``target`` (merge-tree +
  commit-tree, as the merge builds it; a conflict = no check, state ``skipped``), in its own standing worktree
  ``check`` (never the merge suite worktree, never the checkout). The tests are the merge's own selection
  (``merge._plan_selection``) with the budget's workers (``merge._with_workers``), as a ``Popen`` child.
- Red first run -> the ``FAILED`` ids are re-run alone (``merge._recheck_cmd``); green on the re-run = state
  ``green`` and nothing returned. Still red = ``CheckOutcome(red=True, ids, output_path, message)``, once.
  #223: the re-run waits first until the machine is quiet (``merge_load.QuietWait``, one reading per 30 s poll of
  the waiter's own loop, never a blocking sleep, at most 10 min, then ``re-check under load``), and each failed
  test gets a verdict word (``load`` / ``code`` / ``unclear``) written to the status file (``verdicts``, ``note``),
  to the outcome and to the check's output.
  A red run that prints no ``FAILED`` id cannot be re-run alone: state ``skipped`` (inconclusive, the line's
  own suite decides), never red.
- One check at a time on a machine: the seat ``<lock>.checks/seat`` names the owner (pid + ticket). A dead
  owner's seat is taken over. Front first: a waiter may take the seat only when every OLDER live merge ticket
  of the same line (same suite / remote / main / cwd) in ``<lock>.queue/`` already has a final status.
- ``<lock>.checks/<ticket>.json`` = ``{"state": "running|green|red|stopped|skipped", "pid": <check child pid or
  null>, "branch": ..., "since": <local time of the last state change>, "ids": [...]}``, written atomically.
  Unit 3 (merge-status) reads this shape.
- Preparation (merge commit, worktree, selection, worker probe: seconds) runs in a daemon thread, so ``poll()``
  and ``stop()`` themselves stay a few ms of git/file work. The child is started only by ``poll()``.
- A heavy check (the full suite, or more than 500 selected tests) takes a machine berth (``merge_machine``, #224)
  in the preparation thread, so the waiter's loop never blocks on it, and frees it when the check ends or stops.
- Nothing here raises into the waiter's loop: an unexpected error ends the check as ``skipped``.
- #254: before it takes its seat, the check asks ``github_plan()`` (the merge's own ``merge_github.plan_for`` with
  the merge's settings and cwd, handed in by the merge). A plan means the merge suite goes to GitHub: the check
  ends ``skipped`` with reason ``merge suite on GitHub``, starts no child and takes no berth. No plan, no probe, or a
  probe that raises: the check runs as before. Asked once per check.

``merge`` is imported inside functions only (``merge`` imports this module).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .closing_keywords import ClosingKeywordError, require_clean_built
from .pytest_runs_plugin import with_no_runlog
from . import merge_load, merge_machine
from .suite_worktree import ensure_suite_worktree
from .util import holder_is_live, nested_env, no_window_flags, now, pid_alive

CHECK_WORKTREE = "check"
FINAL_STATES = ("green", "red", "stopped", "skipped")
BERTH_POLL_SECONDS = 5.0        # #224: a heavy check waiting for a machine berth looks this often
RETRY_INTERVAL_SECONDS = 0.5   # a waiting waiter looks at the queue and the seat this often
GUARD_SECONDS = 2.0            # the short OS guard around "front-first + take the seat"
KILL_WAIT_SECONDS = 5.0
GITHUB_REASON = "merge suite on GitHub"   # #254: the reason a check skips while its merge suite goes to GitHub
_SEQ = itertools.count()


@dataclass
class CheckOutcome:
    red: bool
    ids: list[str] = field(default_factory=list)
    output_path: str = ""
    message: str = ""
    verdicts: dict[str, dict] = field(default_factory=dict)  # #223: {test id: merge_load.record(...)}
    note: str = ""                                          # #223: ``re-check under load`` when the wait hit its cap


def checks_dir(lock_path: Path) -> Path:
    """``<lock>.checks/`` beside the lock's own ``.queue/`` and ``.batch/``."""
    lock_path = Path(lock_path)
    return lock_path.with_name(lock_path.name + ".checks")


def status_path(lock_path: Path, ticket: str) -> Path:
    return checks_dir(lock_path) / f"{ticket}.json"


def read_status(lock_path: Path, ticket: str) -> dict | None:
    try:
        data = json.loads(status_path(lock_path, ticket).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomic(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f"tmp-{uuid.uuid4().hex}"
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):  # Windows: a reader may hold the target a moment
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            time.sleep(0.01)
    os.replace(tmp, dest)


class _Prepared:
    """What the preparation thread hands to ``poll()``."""
    def __init__(self) -> None:
        self.skip_reason = ""        # non-empty: no check (conflict, empty selection is "green" below)
        self.green_now = False       # the selection is empty: nothing to run
        self.check_dir: Path | None = None
        self.cmd = ""
        self.env: dict[str, str] = {}
        self.head_line = ""
        self.output_path: Path | None = None
        self.selected = False        # head line 1 is "selected N of M" (pytest exit 5 = 0 run = green)
        self.berth_deferred = False  # #343: a full run whose berth waits for the caller (it may go to GitHub)
        self.berth_tests_line = ""   # the selection line the deferred berth is sized by


class LineCheck:
    """The check of one waiting branch. ``ticket`` is the waiter's file name in ``<lock>.queue/``
    (``<time_ns>-<pid>-<seq>``), ``target`` the resolved sha of its branch. ``suite_shared`` names the
    directories linked into the check worktree (default ``worktree.SHARED``); ``output_dir`` the merge
    suite output dir (default ``merge._suite_output_dir()``)."""

    def __init__(self, cwd: Path, lock_path: Path, ticket: str, branch: str, target: str, suite_cmd: str,
                 *, suite_env: dict[str, str] | None = None, suite_shared=None, remote: str = "origin",
                 main: str = "main", output_dir: Path | None = None,
                 retry_interval: float = RETRY_INTERVAL_SECONDS, priority: bool = False,
                 berth_poll: float = BERTH_POLL_SECONDS, read_machine=None, clock=time.monotonic,
                 github_plan: Callable[[], tuple] | None = None, defer_full_berth: bool = False):
        self.cwd = Path(cwd)
        self.defer_full_berth = defer_full_berth  # #343: a full run takes no berth in the preparation (``take_deferred_berth``)
        self.github_plan = github_plan  # #254: () -> (merge_github.Plan | None, note); None = no probe
        self._github_asked = False
        self.lock_path = Path(lock_path)
        self.ticket = ticket
        self.branch = branch
        self.target = target
        self.suite_cmd = suite_cmd
        self.suite_env = with_no_runlog(suite_env)  # #235: a check's pytest run is not logged
        self.suite_shared = suite_shared
        self.remote = remote
        self.main = main
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.retry_interval = retry_interval
        self.priority = priority     # the branch is a Priority ticket's: its heavy check heads the machine's line
        self.berth_poll = berth_poll
        self._berth = None            # the machine berth a heavy check holds (#224)
        self.state = "idle"
        self.message = ""            # why a check was skipped / what went wrong
        self.ids: list[str] = []
        self.outcome: CheckOutcome | None = None
        self.output_path: Path | None = None
        self.child_pid: int | None = None
        self._proc: subprocess.Popen | None = None
        self._phase = ""             # "run" or "recheck"
        self._prep: _Prepared | None = None
        self._thread: threading.Thread | None = None
        self._cancel = False
        self._release_on_thread_end = False
        self._prep_done = False
        self._prep_lock = threading.Lock()
        self._next_try = 0.0
        self._recheck_offset = 0
        self._read_machine = read_machine   # #223: the machine reading (default ``merge._machine_reading``)
        self._clock = clock                 # #223: the quiet wait's clock
        self._cpu_started = False
        self._failure: merge_load.LoadReading | None = None   # the machine when the first run ended red
        self._quiet_wait: merge_load.QuietWait | None = None
        self._recheck_reading: merge_load.LoadReading | None = None
        self.verdicts: dict[str, dict] = {}
        self.note = ""                      # ``re-check under load`` when the wait hit its cap
        self.errors: list[str] = []         # what this check could not do (a sampler, a verdict write): told, not dropped
        self._cpu_failed = False

    def notes(self) -> str:
        """The outcome's note: the wait's own note, then what could not be done."""
        return "; ".join(x for x in (self.note, *self.errors) if x)

    def _machine(self) -> merge_load.LoadReading:
        if self._cpu_failed:  # no sampler: the CPU is n/a, which is never quiet (the verdict is `unclear`)
            return merge_load.LoadReading(cpu_percent=None, runs=0, at=time.time())
        from .merge import _machine_reading
        return (self._read_machine or _machine_reading)()

    # -- paths -------------------------------------------------------------------------------

    @property
    def _dir(self) -> Path:
        return checks_dir(self.lock_path)

    @property
    def _seat(self) -> Path:
        return self._dir / "seat"

    @property
    def _queue_dir(self) -> Path:
        return self.lock_path.with_name(self.lock_path.name + ".queue")

    # -- public ------------------------------------------------------------------------------

    def should_check(self, holder_live: bool) -> bool:
        """True when a check may start now: a live holder exists and this check has not started or ended."""
        from .merge import machine_holds
        return bool(holder_live) and self.state in ("idle", "waiting") and not machine_holds()

    def poll(self, holder_live: bool = True) -> CheckOutcome | None:
        try:
            return self._poll(holder_live)
        except Exception as exc:  # noqa: BLE001 -- a check never breaks the waiter's loop
            self._end("skipped", f"check error: {type(exc).__name__}: {exc}")
            return None

    def stop(self) -> None:
        """Kill the child's whole tree, free the seat, status ``stopped`` (a finished check keeps its
        state). Idempotent."""
        self._cancel = True
        self._kill_child()
        self._stop_cpu()
        if self.state in ("idle", "waiting", "preparing", "running"):
            self.state = "stopped"
            self.child_pid = None
            self._write_status()
        self.release_berth()
        self._free_seat()

    def cleanup(self) -> None:
        """``stop()``, then remove this ticket's status file (the waiter has left)."""
        self.stop()
        try:
            status_path(self.lock_path, self.ticket).unlink()
        except OSError:
            pass

    # -- the state machine -------------------------------------------------------------------

    def _poll(self, holder_live: bool) -> CheckOutcome | None:
        if self.state in ("idle", "waiting"):
            if not self.should_check(holder_live) or time.monotonic() < self._next_try:
                return None
            self._next_try = time.monotonic() + self.retry_interval
            if self._merge_suite_on_github():  # #254: before the seat, the front and any berth
                return None
            self._try_take_seat()
            return None
        if self.state == "preparing":
            assert self._thread is not None
            if self._thread.is_alive():
                return None
            from .merge import machine_holds
            if machine_holds():  # #779: no check child starts in play; the launch waits for normal
                return None
            return self._launch()
        if self.state == "running":
            return self._advance()
        return None

    def _merge_suite_on_github(self) -> bool:
        """#254: asked once, before this check would start. A plan ends the check as ``skipped`` (no seat, no
        preparation, no child, no berth); no plan, no probe, or a probe that raises leaves it to run as before."""
        if self.github_plan is None or self._github_asked:
            return False
        self._github_asked = True
        try:
            plan, _note = self.github_plan()
        except Exception:  # noqa: BLE001 -- an unreadable setting is "no plan": the check runs as before
            return False
        if plan is None:
            return False
        self.state = "skipped"
        self.message = GITHUB_REASON
        self._write_status()
        return True

    def _try_take_seat(self) -> None:
        from .merge import MergeLockTimeout, seat_guard
        try:
            with seat_guard(self._seat, time.time() + GUARD_SECONDS):
                if not self._front_clear() or not self._seat_free():
                    self.state = "waiting"
                    return
                self._dir.mkdir(parents=True, exist_ok=True)
                _write_atomic(self._seat, json.dumps({"pid": os.getpid(), "ticket": self.ticket,
                                                      "started": now()}))
                self.state = "preparing"
                self._write_status()
        except (MergeLockTimeout, OSError):
            self.state = "waiting"
            return
        self._thread = threading.Thread(target=self._prepare, daemon=True)
        self._thread.start()

    def _seat_free(self) -> bool:
        """No seat, a seat of this ticket, or a seat whose owner process is gone."""
        try:
            owner = json.loads(self._seat.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            owner = None
        if not isinstance(owner, dict):
            return True  # unreadable under the guard: nobody is writing it
        if owner.get("ticket") == self.ticket and owner.get("pid") == os.getpid():
            return True
        return not holder_is_live(owner.get("pid"), owner.get("started"))

    def _front_clear(self) -> bool:
        """Every older live merge ticket of the same line has a final status."""
        mine = {"suite": self.suite_cmd, "remote": self.remote, "main": self.main,
                "cwd": str(self.cwd.resolve())}
        try:
            names = sorted(self._queue_dir.iterdir(), key=lambda t: t.name)
        except OSError:
            return True
        for t in names:
            if t.name >= self.ticket:
                break
            try:
                pid = int(t.name.split("-")[1])
            except (IndexError, ValueError):
                continue
            if not pid_alive(pid):
                continue
            try:
                info = json.loads(t.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict) or info.get("kind") != "merge":
                continue
            if any(info.get(k) != v for k, v in mine.items()):
                continue
            st = read_status(self.lock_path, t.name)
            if st is None or st.get("state") not in FINAL_STATES:
                return False
        return True

    def _free_seat(self) -> None:
        """Remove the seat when it is this ticket's; deferred to the preparation thread while it is alive
        (it may still be using the check worktree)."""
        if self._thread is not None:
            with self._prep_lock:
                if not self._prep_done:
                    self._release_on_thread_end = True
                    return
        from .merge import MergeLockTimeout, seat_guard
        try:
            with seat_guard(self._seat, time.time() + GUARD_SECONDS):
                try:
                    owner = json.loads(self._seat.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return
                if (isinstance(owner, dict) and owner.get("ticket") == self.ticket
                        and owner.get("pid") == os.getpid()):
                    try:
                        self._seat.unlink()
                    except OSError:
                        pass
        except (MergeLockTimeout, OSError):
            pass

    # -- preparation (thread) ------------------------------------------------------------------

    def _prepare(self) -> None:
        prep = _Prepared()
        try:
            self._prepare_into(prep)
        except Exception as exc:  # noqa: BLE001
            prep.skip_reason = f"prepare failed: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        self._prep = prep
        if self._cancel:  # stopped while preparing: a berth taken meanwhile goes back at once
            self.release_berth()
        with self._prep_lock:
            self._prep_done = True
            release = self._release_on_thread_end
        if release:
            self._free_seat()

    def _prepare_into(self, prep: _Prepared) -> None:
        from . import merge as m
        cwd = self.cwd
        base = m._rev(cwd, self.main) or m._head(cwd)
        if not base:
            prep.skip_reason = f"no base: {self.main} names no commit"
            return
        tree = m._git(cwd, "merge-tree", "--write-tree", "--name-only", base, self.target)
        if tree.returncode != 0:
            prep.skip_reason = "merge conflict" if tree.returncode == 1 else m._first_line(tree.stderr) or "merge-tree failed"
            return
        message = f"Check {self.branch}"
        try:
            require_clean_built(message, where="the line check commit")
        except ClosingKeywordError as exc:
            prep.skip_reason = str(exc)
            return
        commit = m._git(cwd, "commit-tree", tree.stdout.split()[0], "-p", base, "-p", self.target,
                        "-m", message)
        if commit.returncode != 0:
            prep.skip_reason = m._first_line(commit.stderr) or "commit-tree failed"
            return
        sha = commit.stdout.strip()
        if self._cancel:
            prep.skip_reason = "stopped"
            return
        shared = self.suite_shared
        if shared is None:
            from .worktree import SHARED
            shared = SHARED
        check_dir = ensure_suite_worktree(cwd, sha, shared=tuple(shared), lock=None, name=CHECK_WORKTREE)
        prep.check_dir = check_dir
        out_dir = self.output_dir if self.output_dir is not None else m._suite_output_dir()
        base_path = m._new_suite_output_path(self.branch, out_dir)
        # "<ts>-<seq>-check-<branch>.out": sorts with the merge outputs by time, so pruning keeps its order
        prep.output_path = base_path.with_name(base_path.name.replace(
            f"-{m._safe_branch_name(self.branch)}.out", f"-check-{m._safe_branch_name(self.branch)}.out"))
        prep.output_path.parent.mkdir(parents=True, exist_ok=True)
        is_pytest = m._is_pytest_command(self.suite_cmd)
        run_cmd = m._augmented_suite_cmd(self.suite_cmd) if is_pytest else self.suite_cmd
        tests_line = ""
        if is_pytest and m.resolve_select_tests():
            run_cmd, tests_line, skip = m._plan_selection(cwd, base, sha, run_cmd, prep.output_path)
            if skip:
                prep.green_now = True
                prep.output_path.write_text(tests_line + "\n", encoding="utf-8")
                return
        if self._cancel:
            prep.skip_reason = "stopped"
            return
        if self.defer_full_berth and merge_machine.run_size(tests_line, self.cwd)[1]:
            prep.berth_deferred, prep.berth_tests_line = True, tests_line
        elif not self._take_berth(tests_line, prep.output_path):
            prep.skip_reason = "stopped"
            return
        with m.no_suite_reservation():  # the shared reservation is the merge suite's and the full run's
            run_cmd, _workers, workers_line = m._with_workers(run_cmd, is_pytest, check_dir)
        prep.head_line = "\n".join(x for x in (tests_line, workers_line) if x)
        prep.selected = m._SELECTED_LINE_RE.match(prep.head_line) is not None
        prep.cmd = run_cmd
        tail = self.suite_env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
        prep.env = {**self.suite_env, "PYTHONPATH": str(check_dir) + (os.pathsep + tail if tail else "")}

    def _take_berth(self, tests_line: str, output_path: Path) -> bool:
        """#224: a heavy check waits here (in the preparation thread) for a machine berth and holds it until the check
        ends. False when the check was stopped meanwhile. A wait is said once in the check's output, which is
        emptied again when the berth is taken (line 1 of the output stays the selection line)."""
        tests, full = merge_machine.run_size(tests_line, self.cwd)
        waited = []
        on_wait, sleep = merge_machine.wait_hooks(output_path, log=waited.append, stop=lambda: self._cancel)

        try:
            berth = merge_machine.hold_berth(self.cwd, self.branch, tests, full, self.priority, on_wait,
                                           poll=self.berth_poll, sleep=sleep, out_path=output_path)
        except merge_machine.WaitStopped:
            return False
        if berth is None:
            return True
        self._berth = berth
        if self._cancel:
            self.release_berth()
            return False
        if waited:
            output_path.write_text("", encoding="utf-8")
        return True

    def take_deferred_berth(self, prep: _Prepared) -> bool:
        """#343: the berth of a full run whose preparation left it to the caller (the run stays on this PC); False when
        the check was stopped meanwhile. A no-op once taken."""
        if not prep.berth_deferred:
            return True
        prep.berth_deferred = False
        return self._take_berth(prep.berth_tests_line, prep.output_path)

    def release_berth(self) -> None:
        berth, self._berth = self._berth, None
        if berth is not None:
            berth.release()

    def _launch(self) -> CheckOutcome | None:
        prep = self._prep
        assert prep is not None
        if prep.skip_reason:
            self._end("skipped", prep.skip_reason)
            return None
        self.output_path = prep.output_path
        if prep.green_now:
            self._end("green", "no test the change can affect")
            return None
        if prep.head_line:
            with open(prep.output_path, "ab") as fh:
                fh.write((prep.head_line + "\n").encode("utf-8"))
        self._phase = "run"
        self._start_cpu()
        self._start_child(prep.cmd)
        return None

    def _start_cpu(self) -> None:
        """#223: the 60 s CPU sampler runs while the check does, so the reading at the failure has a window."""
        if self._read_machine is not None:
            return
        from .merge import _start_machine_cpu
        try:
            _start_machine_cpu()
            self._cpu_started = True
        except Exception as exc:  # noqa: BLE001 -- a reading is information, never a reason to break a check
            self._cpu_failed = True
            self.errors.append(f"cpu sampler not started: {type(exc).__name__}: {exc}")

    def _stop_cpu(self) -> None:
        if self._cpu_started:
            self._cpu_started = False
            merge_load.MACHINE_CPU.stop()

    def _start_child(self, cmd: str) -> None:
        prep = self._prep
        assert prep is not None and prep.check_dir is not None and prep.output_path is not None
        flags = no_window_flags() | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        with open(prep.output_path, "ab") as out:
            self._proc = subprocess.Popen(cmd, shell=True, cwd=str(prep.check_dir), env={**nested_env(), **prep.env},
                                          creationflags=flags, stdin=subprocess.DEVNULL, stdout=out,
                                          stderr=subprocess.STDOUT)
        self.child_pid = self._proc.pid
        self.state = "running"
        self._write_status()

    def _advance(self) -> CheckOutcome | None:
        from . import merge as m
        prep = self._prep
        assert prep is not None and prep.output_path is not None
        if self._phase == "wait":
            return self._wait_for_quiet(m)
        proc = self._proc
        assert proc is not None
        rc = proc.poll()
        if rc is None:
            return None
        self._proc = None
        self.child_pid = None
        text = m._strip_ansi(prep.output_path.read_bytes().decode("utf-8", errors="replace"))
        if self._phase == "run":
            if rc == 0 or (rc == 5 and prep.selected):
                self._end("green", "")
                return None
            ids = m._failed_ids(text)
            if not ids:
                self._end("skipped", f"red without a FAILED id, nothing to re-run alone (the output: {prep.output_path})")
                return None
            self.ids = ids
            self._failure = self._machine()  # #223: the machine as the failed ids became known
            self._quiet_wait = merge_load.QuietWait(self._clock)
            self._phase = "wait"
            return self._wait_for_quiet(m)
        # the re-run of the failed ids alone
        own = m._strip_ansi(prep.output_path.read_bytes()[self._recheck_offset:].decode("utf-8", errors="replace"))
        named = [i for i in m._failed_ids(own) if i in self.ids]
        still = [] if rc == 0 else named or list(self.ids)
        self._judge(still, inconclusive=rc != 0 and not named)
        if rc == 0:
            self._end("green", self.notes())
            return None
        self.ids = still
        message = m._red_suite_message(len(still), still, prep.output_path)
        if self.notes():
            message += f"; {self.notes()}"
        self.outcome = CheckOutcome(red=True, ids=still, output_path=str(prep.output_path), message=message,
                                    verdicts=self.verdicts, note=self.notes())
        self._end("red", message)
        return self.outcome

    def _wait_for_quiet(self, m) -> CheckOutcome | None:
        """#223: one step of the wait (a reading at most every 30 s of the injected clock); the re-run starts on the
        step that reads a quiet machine, or at 10 min with the note ``re-check under load``."""
        prep, wait = self._prep, self._quiet_wait
        assert prep is not None and prep.output_path is not None and wait is not None
        answer = wait.step(self._machine)
        if answer is None:
            return None
        self._recheck_reading, self.note = answer
        self._phase = "recheck"
        self._recheck_offset = prep.output_path.stat().st_size
        self._start_child(m._recheck_cmd(self.suite_cmd, self.ids))
        return None

    def _judge(self, still: list[str], inconclusive: bool) -> None:
        """#223: the verdict word of each failed test, kept in ``verdicts`` and written to the check's output."""
        prep, failure, again = self._prep, self._failure, self._recheck_reading
        assert prep is not None and prep.output_path is not None and failure is not None and again is not None
        lines = []
        for test_id in self.ids:
            if inconclusive:
                rec = merge_load.unjudged(test_id, "inconclusive", failure)
            else:
                rec = merge_load.record(test_id, failure, again,
                                        merge_load.verdict(failure, test_id not in still, again))
            self.verdicts[test_id] = rec
            lines.append(merge_load.verdict_line(rec))
        if self.note:
            lines.append(self.note)
        try:
            with open(prep.output_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write("".join(line + "\n" for line in lines))
        except OSError as exc:
            self.errors.append(f"verdict lines not written: {type(exc).__name__}: {exc}")

    # -- end states ----------------------------------------------------------------------------

    def _end(self, state: str, message: str) -> None:
        if self.state in FINAL_STATES:
            return
        self._kill_child()
        self._stop_cpu()
        self.state = state
        self.message = message
        self.child_pid = None
        self._write_status()
        self.release_berth()
        self._free_seat()

    def _kill_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            from .merge import _kill_tree
            try:
                _kill_tree(proc.pid)
            except Exception:  # noqa: BLE001 -- taskkill's own failure: fall back to the direct kill
                pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        self.child_pid = None

    def _write_status(self) -> None:
        shown = "running" if self.state in ("preparing", "running") else self.state
        if shown not in ("running", *FINAL_STATES):
            return
        body = {"state": shown, "pid": self.child_pid, "branch": self.branch, "since": now(), "ids": self.ids,
                "reason": self.message}
        if self.verdicts:  # #223
            body["verdicts"] = {i: merge_load.verdict_summary(rec) for i, rec in self.verdicts.items()}
        if self.notes():
            body["note"] = self.notes()
        try:
            _write_atomic(status_path(self.lock_path, self.ticket), json.dumps(body))
        except OSError:
            pass
