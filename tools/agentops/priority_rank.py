"""#122: the rank of a Priority ticket. A Priority ticket is an open ticket labelled `priority` (the owner's
push) or `flow-fix` (the brain's mark on a fix of the flow). `priority` ranks before `flow-fix`; inside a
class the older mark ranks first; a ticket with both labels ranks as `priority`, with that label's mark.

Pure: the timeline reader is injected (`core_table.GhIssueReader` in production, a stub in tests), so no
test needs `gh`. Stdlib only."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol, Sequence

PRIORITY_LABEL = "priority"
FLOW_FIX_LABEL = "flow-fix"
#: the classes in rank order: the first one heads the queue
CLASSES = (PRIORITY_LABEL, FLOW_FIX_LABEL)


class TimelineReader(Protocol):
    """What `mark_time` needs of a reader: an issue's timeline events, as the GitHub API returns them."""

    def timeline(self, issue: int) -> list[dict]: ...


def priority_class(labels: Sequence[str]) -> str | None:
    """The class a ticket ranks in: `priority` when it carries that label (even with `flow-fix`), else
    `flow-fix`, else None (an ordinary ticket)."""

    for label in CLASSES:
        if label in labels:
            return label
    return None


def rank_key(cls: str | None, marked: datetime | None) -> tuple[int, float]:
    """The sort key of a Priority ticket (ascending = first in line). `cls` is the ticket's class from
    `priority_class` (derived once by the caller) and `marked` the mark time of that class label; None (no mark
    found) sorts last inside the class. A ticket with no class (`cls` None) gets a key after every Priority one."""

    if cls is None:
        return (len(CLASSES), math.inf)
    when = marked.timestamp() if marked is not None else math.inf
    return (CLASSES.index(cls), when)


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def mark_time(issue_number: int, label: str, reader: TimelineReader) -> datetime | None:
    """When `label` was last put on the issue: the newest `labeled` timeline event of that label, or None
    when the timeline has none. One `reader.timeline` call per ticket."""

    times: list[datetime] = []
    for event in reader.timeline(issue_number):
        if event.get("event") != "labeled":
            continue
        if (event.get("label") or {}).get("name") != label:
            continue
        when = _parse(event.get("created_at"))
        if when is not None:
            times.append(when)
    return max(times) if times else None
