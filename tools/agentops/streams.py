"""Stream keeper (#319): a Stream is a native sub-issue of an open `wayfinder:map` issue whose title
starts with `Stream: `. It closes itself when all its tickets are closed and reopens when one is open
again. `StreamKeeper.tick()` is called by the dispatcher loop; it reads at most once per `READ_INTERVAL`."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from .gh_call import run_gh

ROOT = Path(__file__).resolve().parents[2]
STREAM_PREFIX = "Stream: "
#: #1113: seconds between two reads of the stream search (one GraphQL call of 10 points). What it feeds
#: is only the close and reopen of a stream parent, and the dispatcher shows no stream data: a stream
#: may be closed or reopened up to this long after its last ticket changes, which is allowed.
#: #18: 3 dispatchers x 12 reads an hour x 10 points = ~360 points/h of the shared 5,000/h GitHub budget;
#: at 15 min (900 s) it is ~120 points/h. A stream parent may now close or reopen up to 15 min after
#: its last ticket changes, which is allowed.
READ_INTERVAL = 900.0

QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 20) {
    nodes { ... on Issue { number
      subIssues(first: 50) { nodes { number title state
        subIssues(first: 100) { nodes { number state closedAt } } } } } }
  }
}
"""


class StreamKeeper:
    def __init__(self, repo: str, runner: Callable[[list[str], str | None], subprocess.CompletedProcess] | None = None,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print) -> None:
        self.repo = repo
        self._runner = runner
        self._clock = clock
        self._log = log
        self._last_read: float | None = None

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(args, stdin)
        return run_gh(args, cwd=ROOT, input=stdin, timeout=60)

    def _write(self, verb: str, number: int, comment: str) -> None:
        try:
            r = self._run(["issue", verb, str(number), "-R", self.repo, "--comment", comment])
        except Exception as exc:
            self._log(f"streams: gh issue {verb} #{number} failed: {exc}")
            return
        if r.returncode != 0:
            self._log(f"streams: gh issue {verb} #{number} failed: {(r.stderr or '').strip()}")
            return
        self._log(f"streams: {verb} #{number}: {comment}")

    def tick(self) -> None:
        now = self._clock()
        if self._last_read is not None and now - self._last_read < READ_INTERVAL:
            return
        self._last_read = now
        try:
            r = self._run(["api", "graphql", "-f", f"query={QUERY}", "-f",
                           f"q=repo:{self.repo} is:issue is:open label:wayfinder:map"])
            if r.returncode != 0:
                self._log(f"streams: gh read failed: {(r.stderr or '').strip()}")
                return
            maps = json.loads(r.stdout)["data"]["search"]["nodes"]
            for m in maps:
                for s in ((m.get("subIssues") or {}).get("nodes") or []):
                    self._one(s)
        except Exception as exc:
            self._log(f"streams: tick failed: {exc}")

    def _one(self, s: dict) -> None:
        if not str(s.get("title", "")).startswith(STREAM_PREFIX):
            return
        tickets = (s.get("subIssues") or {}).get("nodes") or []
        if not tickets:
            return
        open_ = sorted(t["number"] for t in tickets if t["state"] == "OPEN")
        if s["state"] == "OPEN" and not open_:
            last = max(tickets, key=lambda t: t.get("closedAt") or "")
            self._write("close", s["number"], f"All tickets of this stream are closed; the last one is #{last['number']}.")
        elif s["state"] == "CLOSED" and open_:
            self._write("reopen", s["number"], f"Ticket #{open_[0]} of this stream is open.")
