"""#924: a ticket whose result failed three times in a row is not offered a fourth attempt.

Pure counter over a ticket's comments. Machine markers, each the FIRST line of a comment body (leading whitespace
stripped): `<!-- send-back -->` (an attempt not accepted), `<!-- owner-answer -->` and `<!-- brain-answer -->` (an
answer to the stop, #209), `<!-- accepted -->` (something of the ticket accepted), `<!-- merge result -->` (a merge's
result, #209: failed when its line reads "did not land", reset when it reads "landed on main").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

SEND_BACK = "<!-- send-back -->"
OWNER_ANSWER = "<!-- owner-answer -->"
#: the brain's answer to a loop stop (#209): the same reset as the owner's, for the brain's own decision
BRAIN_ANSWER = "<!-- brain-answer -->"
#: the markers that restart the count and the stop reading (the newest of them wins)
ANSWER_MARKERS = (OWNER_ANSWER, BRAIN_ANSWER)
ACCEPTED = "<!-- accepted -->"
#: a merge's result comment (#209), written by merge_handback.py: "Merge of <branch> landed on main ..." or
#: "Merge of <branch> did not land (<reason>) ..." on the line after the marker
MERGE_RESULT = "<!-- merge result -->"
#: the heading of the comment whoever holds the ticket writes when the stop fires.
STOP_HEADING = "Loop stop"
#: #209: failed attempts (send-backs, merges that did not land) in a row that stop a ticket
MAX_FAILED_ATTEMPTS = 3
_STOP_HEAD = re.compile(r"^\W*" + re.escape(STOP_HEADING) + r"\b", re.IGNORECASE)
_MERGE_OUTCOME = re.compile(r"Merge of \S+ (landed on main|did not land(?: \(([^)]*)\))?)")
#: not-landed reasons that are an outside event, not a fault of the ticket's work: they count for nothing
_OUTSIDE_REASONS = ("lock-timeout", "setup-error", "silent-holder", "withdrawn", "superseded")  # #219: withdrawn/superseded

# what a comment does to the streak
_FAILED, _RESET, _ANSWER = "failed", "reset", "answer"


@dataclass(frozen=True, slots=True)
class LoopStatus:
    count: int  # failed attempts in a row since the newest answer, landed merge or accepted
    first_at: str  # createdAt of the first of them, "" when count == 0
    by_count: bool  # count >= 3
    last_at: str = ""  # createdAt of the newest of them, "" when count == 0 (#209: the key of one unblocker start)

    @property
    def stopped(self) -> bool:
        return self.by_count

    def reason(self) -> str:
        """`3 failed attempts`, `1 failed attempt`."""
        return f"{self.count} failed attempt{'' if self.count == 1 else 's'}"


def _event(body: object) -> str | None:
    """What the comment does to the streak: `_FAILED`, `_RESET`, `_ANSWER`, or None when it counts for nothing."""
    first, _, rest = str(body or "").lstrip().partition("\n")
    first = first.strip()
    if first.startswith(SEND_BACK):
        return _FAILED
    if first.startswith(ACCEPTED):
        return _RESET
    if first.startswith(ANSWER_MARKERS):
        return _ANSWER
    if first.startswith(MERGE_RESULT):
        found = _MERGE_OUTCOME.match(rest.lstrip().split("\n", 1)[0].strip())
        if found is None:
            return None
        if found.group(1) == "landed on main":
            return _RESET
        return None if found.group(2) in _OUTSIDE_REASONS else _FAILED
    return None


def loop_status(comments: list[dict], now: datetime | None = None) -> LoopStatus:
    """Count the failed attempts in a row since the newest answer (owner or brain); a landed merge result or an
    accepted comment restarts the streak at 0. `comments` are dicts with `body` and `createdAt`, in the order GitHub
    lists them. `now` is unused since #209 removed the 4 h clause; it stays so the callers keep their call."""

    failed: list[dict] = []
    for c in comments or []:
        kind = _event(c.get("body"))
        if kind in (_ANSWER, _RESET):
            failed = []
        elif kind == _FAILED:
            failed.append(c)
    first_at = str(failed[0].get("createdAt") or "") if failed else ""
    last_at = str(failed[-1].get("createdAt") or "") if failed else ""
    return LoopStatus(len(failed), first_at, len(failed) >= MAX_FAILED_ATTEMPTS, last_at)


def has_stop_comment(comments: list[dict]) -> bool:
    """#118: a comment headed `Loop stop` (its first non-empty line) after the newest `<!-- owner-answer -->` or
    `<!-- brain-answer -->` (or anywhere, with none), in the order GitHub lists them."""

    since = 0
    for i, c in enumerate(comments or []):
        if _event(c.get("body")) == _ANSWER:
            since = i + 1
    return any(_STOP_HEAD.match(str(c.get("body") or "").lstrip()) for c in (comments or [])[since:])
