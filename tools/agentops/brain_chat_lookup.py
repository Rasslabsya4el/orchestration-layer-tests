"""Which chat is the brain chat of a repo, and when its news is held too long (#69).

``resolve_brain_chat`` picks the chat to wake from the rows of ``claude agents --json --all``, in this order:
exact (a live row named like the configured title), alias (a live row named like one of the aliases, in listed
order), folder (the one live interactive row whose cwd is the repo folder; two or more is ambiguous, none is none).

``BrainChatAway`` holds, per title, the time it was first not resolved, and says once per held batch that the
home's brain chat should be woken. ``FallbackLogger`` gives the one log line per changed folder fallback.
``home_alert_text`` is the message for that wake. ``read_home_brain_title`` reads a home's configured title.

Standard library only. The clock and the ``gh`` runner are injected, so tests use fakes and nothing is sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: news held this long without its brain chat is told to the home's brain chat once per batch (#69)
HOME_ALERT_HOLD_SECONDS = 30 * 60


@dataclass(frozen=True)
class Resolved:
    """``name``: the chat to wake, or None; ``how``: exact, alias, folder, ambiguous or none;
    ``count``: live interactive rows in the repo folder."""

    name: str | None
    how: str
    count: int


def _folder_key(path: str | None) -> str:
    """Backslashes as slashes, no trailing slash, case folded: the same folder spelled two ways is one key."""
    if not path:
        return ""
    return re.sub(r"/+$", "", path.replace("\\", "/")).lower()


def _is_live(row: Mapping[str, Any], dead_states: frozenset[str]) -> bool:
    return row.get("state") not in dead_states


def resolve_brain_chat(rows: Iterable[Mapping[str, Any]], title: str, aliases: Iterable[str],
                       folder: str | None, dead_states: Iterable[str]) -> Resolved:
    dead = frozenset(dead_states)  # read once: an iterator would be spent after the first row
    live = [row for row in rows if _is_live(row, dead)]
    names = {row.get("name") for row in live}
    key = _folder_key(folder)
    in_folder = [row for row in live
                 if row.get("kind") == "interactive" and key and _folder_key(row.get("cwd")) == key]
    count = len(in_folder)
    if title in names:
        return Resolved(title, "exact", count)
    for alias in aliases:
        if alias in names:
            return Resolved(alias, "alias", count)
    if count == 1:
        return Resolved(in_folder[0].get("name"), "folder", 1)
    if count >= 2:
        return Resolved(None, "ambiguous", count)
    return Resolved(None, "none", 0)


class BrainChatAway:
    """Per title: when it was first not resolved, and whether this batch was already alerted."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._since: dict[str, float] = {}
        self._alerted: set[str] = set()

    def note(self, title: str, resolved: bool) -> None:
        if resolved:
            self._since.pop(title, None)
            self._alerted.discard(title)
        else:
            self._since.setdefault(title, self._now())

    def held_since(self, title: str) -> float | None:
        return self._since.get(title)

    def home_alert_due(self, title: str, hold_seconds: float = HOME_ALERT_HOLD_SECONDS) -> bool:
        since = self._since.get(title)
        if since is None or title in self._alerted:
            return False
        if self._now() - since >= hold_seconds:
            self._alerted.add(title)
            return True
        return False


class FallbackLogger:
    """The folder fallback's log line, once per changed (title -> name) pair; never for exact or alias."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def line(self, title: str, resolved: Resolved) -> str | None:
        if resolved.how != "folder" or resolved.name is None:
            self._last.pop(title, None)  # the fallback ended: its next start is a new one and logs again
            return None
        if self._last.get(title) == resolved.name:
            return None
        self._last[title] = resolved.name
        return (f"brain chat: no chat named {title}; using {resolved.name} "
                f"(the one chat in the repo folder)")


def home_alert_text(repo: str, title: str, count: int, held: int, since_text: str) -> str:
    """``held``: how many news items wait; ``since_text`` is a human clock (18:07), computed by the caller,
    never from seconds here."""
    return (f"{repo}: brain chat {title} not found ({count} chats in the folder); "
            f"{held} news held since {since_text}")


def read_home_brain_title(home_repo: str, runner: Callable[[list[str]], Any]) -> str | None:
    """The home's ``brain_chat_title`` from its raw ``config/orchestration.json``; None on any failure."""
    try:
        proc = runner(["api", f"repos/{home_repo}/contents/config/orchestration.json",
                       "-H", "Accept: application/vnd.github.raw"])
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except Exception:
        return None
    title = data.get("brain_chat_title") if isinstance(data, dict) else None
    return title if isinstance(title, str) and title else None
