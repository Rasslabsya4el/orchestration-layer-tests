"""#209: the record of the unblocker sessions the dispatcher started, so one trigger starts one session, restart included.

A trigger is a ticket and the `createdAt` of its newest failed attempt (`LoopStatus.last_at`): the same ticket with a
newer failed attempt is a new trigger, the same key never starts a second session. The store is one small JSON file
(`{"<issue>": {"key": ..., "session": ..., "at": ...}}`), written atomically like the brain notice's; with no path
the record lives in memory only (every test's default).

The same record carries what came after the start (#209 unit 6): `told` (the key of the brain-answer the area's brain
chat was told of) and `limbo` (the "ended without a brain-answer" line was logged). `way_line` writes the one line
the chat gets for a brain-answer.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .brain_notice import first_line
from .loop_stop import BRAIN_ANSWER

_HEADING = re.compile(r"^\s*##\s")
_ASKS_OWNER = re.compile(r"for the owner\s*:", re.IGNORECASE)
NL = "\n"
_BULLET =re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


class UnblockerStarts:
    """`started(issue, key)` tells whether that trigger already has its session; `record(...)` writes it down."""

    def __init__(self, path: Path | None = None, log: Callable[[str], Any] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._log = log or (lambda _text: None)
        self._records: dict[int, dict[str, str]] = self._load()

    def started(self, issue: int, key: str) -> bool:
        return self._records.get(issue, {}).get("key") == key

    def record(self, issue: int, key: str, session: str, at: str) -> None:
        self._records[issue] = {"key": key, "session": session, "at": at}
        self._save()

    def get(self, issue: int) -> dict[str, str] | None:
        record = self._records.get(issue)
        return dict(record) if record is not None else None

    def untold(self) -> list[tuple[int, dict[str, str]]]:
        """The records whose brain-answer is not told yet, by issue number."""
        return [(n, dict(r)) for n, r in sorted(self._records.items()) if not r.get("told")]

    def mark(self, issue: int, **fields: str) -> None:
        """Add `told` / `limbo` to the issue's record (nothing for an issue with no record)."""
        if issue in self._records:
            self._records[issue].update({k: str(v) for k, v in fields.items()})
            self._save()

    def _load(self) -> dict[int, dict[str, str]]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log(f"unblocker starts: state file unreadable ({type(exc).__name__}), starting empty")
            return {}
        records: dict[int, dict[str, str]] = {}
        for number, value in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(value, dict) and str(number).isdigit():
                records[int(number)] = {str(k): str(v) for k, v in value.items()}
        return records

    def _save(self) -> None:
        if self._path is None:
            return
        data = {str(number): record for number, record in sorted(self._records.items())}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:  # a store that cannot be written never breaks the tick; the in-memory record still holds
            self._log(f"unblocker starts: could not write {self._path.name} ({exc!r})")


def _parse(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def answer_after(comments: list[dict], since: str) -> dict | None:
    """The newest comment whose first line is `<!-- brain-answer -->` and that was created at or after `since`
    (the record's start time); `None` with none. An unreadable time on either side counts as not after."""
    start = _parse(since)
    found = None
    for comment in comments:
        if not str(comment.get("body") or "").lstrip().startswith(BRAIN_ANSWER):
            continue
        at = _parse(str(comment.get("createdAt") or ""))
        if start is not None and at is not None and at >= start:
            found = comment
    return found


def answer_key(comment: dict) -> str:
    """The id of the brain-answer comment, else its `createdAt`: the key its line is told once for."""
    return str(comment.get("id") or comment.get("createdAt") or "")


def way_line(number: int, body: str) -> str:
    """`#N: <first non-empty line of the comment's second section, at most 160 chars>`, or `#N: asks the owner` when
    the comment holds `for the owner:`. The second section is what follows the second `## ` heading; a comment with
    fewer headings gives its last paragraph."""
    if _ASKS_OWNER.search(body):
        return f"#{number}: asks the owner"
    lines = [ln for ln in str(body).splitlines() if not ln.strip().startswith(BRAIN_ANSWER)]
    heads = [i for i, ln in enumerate(lines) if _HEADING.match(ln)]
    if len(heads) >= 2:
        section = lines[heads[1] + 1:]
    else:
        paragraphs = [p for p in NL.join(lines).split(NL * 2) if p.strip()]
        section = paragraphs[-1].splitlines() if paragraphs else []
    text = first_line(NL.join(_BULLET.sub("", ln) for ln in section))
    return f"#{number}: {text}" if text else f"#{number}: way written"
