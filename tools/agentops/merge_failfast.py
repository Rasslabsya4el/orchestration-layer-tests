"""#219 rule 1: the merge suite stops at a confirmed failure.

A test that fails while the suite runs is re-run alone at once. If it fails alone too, the failure is confirmed: the
suite's process tree is stopped and the run ends red with a result that names the test and both failures. If it
passes alone, the suite goes on (as the end-of-suite re-run would have let it). The module knows nothing of pytest or
of a process: the caller hands it three readings (``failed_now``, ``reasons``, ``rerun``) and the way to stop the
tree (``kill``); ``watch`` is a thread body shaped like ``StallWatch.watch``."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

FAILFAST_POLL_SECONDS = 2.0  # how often the live failures are read


class NoVerdict(Exception):
    """The re-run could not tell (timed out, ended red without naming the test, was stopped): the suite goes on,
    and the reason is kept as a note."""


@dataclass
class Confirmed:
    test: str    # the node id
    first: str   # how it failed in the suite ("" when the run did not report it)
    again: str   # how it failed alone

    def message(self) -> str:
        first = self.first or "failure not reported by the suite run"
        return (f"fail-fast: {self.test} failed in the suite ({first}) and failed again alone ({self.again or 'no reason given'}); "
                f"the suite was stopped, the remaining tests were not run")


class FailFast:
    """``failed_now() -> node ids failed so far, in order``; ``reasons() -> {id: one-line failure}``;
    ``rerun(id, stop) -> (passed_alone, one-line failure)``, which raises ``NoVerdict`` when it could not tell and
    must end its own process tree when ``stop`` is set; ``kill(pid)`` stops the suite's process tree.
    A reading or a re-run that could not tell never stops the suite; it leaves one line in ``notes``."""

    def __init__(self, failed_now: Callable[[], list[str]], reasons: Callable[[], dict[str, str]],
                 rerun: Callable[[str, Any], tuple[bool, str]], kill: Callable[[int], object],
                 interval: float | None = None):
        self.failed_now, self.reasons, self.rerun, self.kill = failed_now, reasons, rerun, kill
        self.interval = FAILFAST_POLL_SECONDS if interval is None else interval
        self.confirmed: Confirmed | None = None
        self.notes: list[str] = []  # what could not be read or judged, once each, for the result text
        self._checked: set[str] = set()  # a test is re-run alone once, across restarts of the suite

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def lines(self) -> list[str]:
        """The lines the run's output ends with: the notes, then the confirmed failure."""
        return [*self.notes, *([self.confirmed.message()] if self.confirmed else [])]

    def watch(self, proc, stop) -> None:
        """Thread body: re-runs each newly failed test alone; stops ``proc``'s tree at the first one that fails
        again, then returns. A reading or a re-run that could not tell is no verdict: the suite goes on, the
        reason is noted, and the end-of-suite triage judges it."""
        while not stop.wait(self.interval):
            if proc.poll() is not None:
                return
            try:
                failed = [t for t in self.failed_now() if t not in self._checked]
            except Exception as exc:  # noqa: BLE001 -- a failed reading never hurts the suite
                self._note(f"fail-fast: reading the failed tests raised {type(exc).__name__}: {exc}")
                continue
            for test in failed:
                self._checked.add(test)
                try:
                    passed, again = self.rerun(test, stop)
                except NoVerdict as exc:
                    if stop.is_set():  # stopped because the suite ended: nothing to tell
                        return
                    self._note(f"fail-fast: no verdict for {test}: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._note(f"fail-fast: the re-run of {test} raised {type(exc).__name__}: {exc}")
                    continue
                if stop.is_set() or proc.poll() is not None:  # the suite ended meanwhile: its own end judges
                    return
                if passed:
                    continue
                try:
                    first = self.reasons().get(test, "")
                except Exception as exc:  # noqa: BLE001
                    first = ""
                    self._note(f"fail-fast: reading the reason of {test} raised {type(exc).__name__}: {exc}")
                self.confirmed = Confirmed(test, first, again)
                self.kill(proc.pid)
                return
