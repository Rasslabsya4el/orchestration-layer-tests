"""The daily losses note: one line per cost-like bucket that doubled, sent once a day to the brain chat.

``LossesDaily.check()`` is called by the dispatcher (throttled there to once an hour). On the first check of a day it
starts the computation in a background thread (``runner``); on the first check after that thread finished it sends
ONE message to the brain chat, or nothing when no bucket doubled. The computation reads session transcripts and takes
about a minute per repo, so it never runs inside the tick:

    Losses (<day>): <n> bucket(s) doubled in 7 days against the 7 before:
    <repo>: <bucket> doubled in 7 days: <before> -> <this>

A money bucket that is a window total (`$ spent`, each `$ no ticket owns` row) is compared per closed ticket of its
own window: ``<repo>: <bucket> per closed ticket doubled in 7 days: <before> -> <this>``.

A day is recorded in the state file as ``"checked_on"`` once its check is done (a message sent, or no bucket doubled),
so a restart the same day repeats nothing and a day is never computed twice. A failed computation is one log line and
the next day retries. A failed send keeps the computed lines in memory and the next check retries the send alone.
``check()`` never raises.

Standard library plus the core's ``core_format``, ``losses`` and ``losses_calc`` modules. Every outside seam is injected.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core_format import human_duration
from .losses import ROWS, Window, _money, bucket_values, configured_repos, default_closed_lister
from .losses_calc import doubled

#: the length of each of the two windows compared.
WINDOW_DAYS = 7
#: the table's buckets that are not a cost: a fall in `closed` is not a loss, so `closed` is never doubled.
NOT_A_COST = frozenset({"closed"})


#: the money buckets that are already per closed ticket (the table's mean, median and p90): compared as they are.
#: Every other money bucket is a window total and is compared per closed ticket of its own window.
PER_CLOSED_ALREADY = frozenset({"mean_usd", "median_usd", "p90_usd"})


def _cost_rows() -> list:
    """The table's rows whose number is a cost (every bucket of the table except `closed`), in table order."""
    return [row for row in ROWS if row.key not in NOT_A_COST]


def _is_window_total(row) -> bool:
    """A money bucket that is a window total (`$ spent`, each `$ no ticket owns` row): compared per closed ticket."""
    return row.kind in ("money", "unowned") and row.key not in PER_CLOSED_ALREADY


def _text(row, value: float) -> str:
    """One bucket's value as the table writes it: money as ``$x``, durations through the core's human format,
    counts as whole numbers."""
    if row.kind in ("money", "unowned"):
        return _money(value)
    if row.kind == "duration":
        return human_duration(value)
    return f"{round(value):,}" if math.isfinite(value) else "-"


def _per_closed_line(repo: str, row, this_values: Mapping[str, float],
                     last_values: Mapping[str, float]) -> str | None:
    """The note's line for a window-total money bucket, or None.

    The value compared is the window total divided by the tickets closed in the same window. No base (the window
    before has no closed ticket, or no money) gives no line. A window with no closed ticket and money spent gives the
    `with 0 closed tickets` line against the per-closed base; otherwise the doubling rule applies to the two ratios.
    """
    this_total = float(this_values[row.key])
    last_total = float(last_values[row.key])
    this_closed = float(this_values["closed"])
    last_closed = float(last_values["closed"])
    if last_closed <= 0 or last_total <= 0:
        return None
    before = last_total / last_closed
    if this_closed <= 0:
        if this_total > 0:
            return f"{repo}: {row.label} with 0 closed tickets: {_money(before)} -> {_money(this_total)}"
        return None
    this = this_total / this_closed
    if this >= 2 * before:
        return (f"{repo}: {row.label} per closed ticket doubled in {WINDOW_DAYS} days: "
                f"{_money(before)} -> {_money(this)}")
    return None


def doubling_lines(per_repo: Mapping[str, tuple[Mapping[str, float], Mapping[str, float]]]) -> list[str]:
    """One line per cost-like bucket that doubled, in table order.

    A window-total money bucket (`$ spent`, `$ no ticket owns` rows) is compared per closed ticket of its window:
    ``<repo>: <bucket> per closed ticket doubled in 7 days: <before> -> <this>``, or, when this window closed no ticket
    but spent money against a base, ``<repo>: <bucket> with 0 closed tickets: <before> -> <this total>``.
    Every other bucket: ``<repo>: <bucket> doubled in 7 days: <before> -> <this>``.

    ``per_repo`` maps a repo name to ``(this window's bucket values, the window before's)``, the shape of
    ``losses.bucket_values``. The doubling rule is ``losses_calc.doubled``: a bucket with nothing before never doubles.
    """
    rows = _cost_rows()
    lines: list[str] = []
    for repo, (this_values, last_values) in per_repo.items():
        this_cost = {row.key: float(this_values[row.key]) for row in rows
                     if row.key in this_values and not _is_window_total(row)}
        last_cost = {row.key: float(last_values[row.key]) for row in rows
                     if row.key in last_values and not _is_window_total(row)}
        names = set(doubled(this_cost, last_cost))
        for row in rows:
            if _is_window_total(row):
                if row.key in this_values and row.key in last_values:
                    line = _per_closed_line(repo, row, this_values, last_values)
                    if line is not None:
                        lines.append(line)
            elif row.key in names:
                lines.append(f"{repo}: {row.label} doubled in {WINDOW_DAYS} days: "
                             f"{_text(row, last_cost[row.key])} -> {_text(row, this_cost[row.key])}")
    return lines


def default_compute(settings: Mapping[str, Any], now: datetime) -> dict[str, tuple[dict[str, float], dict[str, float]]]:
    """The real computation: for each configured repo the bucket values of the last 7 days and of the 7 days before."""
    week = timedelta(days=WINDOW_DAYS)
    this = Window(now - week, now, "the last 7 days")
    before = Window(now - 2 * week, now - week, "the 7 days before")
    per_repo = {}
    for repo in configured_repos(dict(settings)):
        per_repo[repo.name] = (bucket_values(repo, this, now, default_closed_lister),
                               bucket_values(repo, before, now, default_closed_lister))
    return per_repo


def _start_thread(job: Callable[[], None]) -> None:
    """The default runner: the job in a daemon thread, so the tick never waits for it."""
    threading.Thread(target=job, name="losses-daily", daemon=True).start()


class LossesDaily:
    """Seams (all injected):
      compute() -> dict[str, tuple[dict, dict]] -- repo -> (last 7 days values, the 7 days before); runs in the
          background job; may raise (one log line, nothing sent, the next day retries).
      runner(job) -- runs the no-argument `job` in the background; the default starts a daemon thread, a test passes
          a synchronous one.
      send(brain_title, message) -- may raise; nothing is recorded then, the next check retries the send.
      brain_live() -> bool -- False sends nothing this call and the next check retries.
      log(text) -- one call per log line.
      now() -> datetime -- the calendar day is the local day of this instant.
    """

    def __init__(
        self,
        *,
        state_path: str | Path,
        brain_title: str,
        send: Callable[[str, str], Any],
        compute: Callable[[], Mapping[str, tuple[Mapping[str, float], Mapping[str, float]]]],
        brain_live: Callable[[], bool],
        log: Callable[[str], Any],
        now: Callable[[], datetime] | None = None,
        runner: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._brain_title = brain_title
        self._send = send
        self._compute = compute
        self._brain_live = brain_live
        self._log = log
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._runner = runner or _start_thread
        #: the day the computation was started for; `None` before the first check of this object.
        self._job_day: str | None = None
        #: set by the job when it ends: ``(day it was computed for, True, lines)`` or ``(day, False, [])``;
        #: `None` while it runs. A result whose day is not today is dropped (a late job of an earlier day).
        self._result: tuple[str, bool, list[str]] | None = None

    # ----- state file -------------------------------------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        """The state file as a dict; `{}` when absent, unreadable (one log line) or not a dict."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log(f"losses daily: state file unreadable ({type(exc).__name__}), starting empty")
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, day: str) -> None:
        state = self._read_state()
        state["checked_on"] = day
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            self._log(f"losses daily: state file not written ({type(exc).__name__}: {exc})")

    # ----- the computation --------------------------------------------------------------------------------------

    def _start(self, day: str) -> None:
        """Starts the computation for `day` through the runner; the job records its own outcome."""
        self._job_day = day
        self._result = None

        def job() -> None:
            try:
                self._result = (day, True, doubling_lines(self._compute()))
            except Exception as exc:  # the job never raises into the runner
                self._log(f"losses daily: compute failed ({type(exc).__name__}: {exc}); the next day retries")
                self._result = (day, False, [])

        self._runner(job)

    # ----- the message ------------------------------------------------------------------------------------------

    @staticmethod
    def message(lines: list[str], day: str) -> list[str]:
        """The message lines: one header line, then the doubled lines."""
        head = (f"Losses ({day}): {len(lines)} bucket(s) doubled in {WINDOW_DAYS} days against the {WINDOW_DAYS} before:")
        return [head, *lines]

    # ----- the check --------------------------------------------------------------------------------------------

    def check(self) -> list[str]:
        """Start the day's computation, or send its lines once it finished; returns the message lines sent, or ``[]``.
        Never raises."""
        try:
            return self._check()
        except Exception as exc:  # never raises into the tick
            self._log(f"losses daily failed: {type(exc).__name__}: {exc}")
            return []

    def _check(self) -> list[str]:
        today = self._now().astimezone().date().isoformat()
        if self._read_state().get("checked_on") == today:
            return []
        if self._job_day != today:
            self._start(today)
            return []
        if self._result is None:
            return []  # still computing
        result_day, ok, lines = self._result
        if result_day != today:
            self._log("losses daily: a result of an earlier day is dropped; a new computation starts")
            self._start(today)
            return []
        if not ok:
            return []  # the computation failed: the next day retries
        if not lines:
            self._save(today)  # no bucket doubled: the day is checked, nothing is sent
            return []
        if not self._brain_live():
            self._log("losses daily: brain chat not live, nothing sent; the next check retries")
            return []
        message_lines = self.message(lines, today)
        try:
            self._send(self._brain_title, "\n".join(message_lines))
        except Exception as exc:
            self._log(f"losses daily: send to {self._brain_title} failed ({type(exc).__name__}: {exc}); "
                      f"the next check retries")
            return []
        self._save(today)
        self._log(f"losses daily: told {self._brain_title}: {len(lines)} doubled bucket(s)")
        return message_lines
