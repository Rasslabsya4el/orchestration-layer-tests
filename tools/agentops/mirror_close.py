"""#425 unit 3: the dispatcher's close sync between a mirror and its source (`sync_closes`).

Each cycle, after the mirror pass, every mirror (a `mirror`-labelled issue of the tickets repo whose first line names a
source URL) is read against its source. Both open or both closed: nothing. The source closed and the mirror open: the
mirror closes with one comment naming who closed the source and when; while a live session holds the mirror it gets
one marker comment and stays open until the hold ends. The mirror closed and the source open: a `reopened` event of the
source later than the mirror's close reopens the mirror; otherwise the mirror was closed first and the source closes,
with a comment linking the mirror's last comment that is not a sync marker. A reopened mirror never closes its source
again, so the sync is loop-free.

Every `gh` call goes through the injected runner (`(args, stdin) -> CompletedProcess`, the shape of `GhCli._run`); the
pair's parsing comes from `mirror_pass`. Standard library only.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tools.agentops.mirror_pass import MIRROR_LABEL, parse_source_url, source_url_of

#: the first line of each comment this pass writes on a mirror or a source.
HELD_MARK = "<!-- mirror-sync: source-closed-while-held -->"
CLOSED_MARK = "<!-- mirror-sync: closed-by-source -->"
REOPENED_MARK = "<!-- mirror-sync: reopened-by-source -->"
_SYNC_PREFIX = "<!-- mirror-sync:"
_LIMIT = "1000"
_JSON_MIRROR = "number,state,closedAt,body,url"

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


@dataclass(frozen=True)
class _Pair:
    """One mirror and the source its first line names, with what the writes need."""

    tickets_repo: str
    mirror: dict[str, Any]
    src: str
    repo: str
    source_number: int
    runner: Runner
    log: Callable[[str], Any]

    @property
    def mirror_number(self) -> int:
        return int(self.mirror["number"])

    @property
    def mirror_url(self) -> str:
        return str(self.mirror.get("url") or f"https://github.com/{self.tickets_repo}/issues/{self.mirror_number}")


def sync_closes(*, tickets_repo: str, runner: Runner, log: Callable[[str], Any], mirror_repos: list[str],
                held: Callable[[int], bool] = lambda number: False) -> list[str]:
    """One cycle. Only a mirror whose source repo is in `mirror_repos` is read or written; with none listed the cycle
    makes no runner call at all. Returns one action string per change made (`closed mirror #n`, `closed source
    owner/name#n`, `reopened mirror #n`, `waiting on hold #n`). A source repo whose read fails is logged once in the
    cycle; a failed read skips only its own pair, and the repo's other pairs are still read; a failed write is logged
    and the loop goes on."""

    if not mirror_repos:
        return []
    mirrors = _list_mirrors(tickets_repo, runner, log)
    if mirrors is None:
        return []
    actions: list[str] = []
    logged_repos: set[str] = set()
    for mirror in mirrors:
        pair = _pair_of(tickets_repo, mirror, runner, log)
        if pair is None or pair.repo not in mirror_repos:
            continue
        source, error = _parse(runner(["api", f"repos/{pair.repo}/issues/{pair.source_number}"], None), dict)
        if source is None:
            if pair.repo not in logged_repos:
                logged_repos.add(pair.repo)
                log(f"mirror: cannot read {pair.repo}: {error}")
            continue
        action = _sync_pair(pair, source, held)
        if action is not None:
            actions.append(action)
    return actions


def _list_mirrors(tickets_repo: str, runner: Runner, log: Callable[[str], Any]) -> list[dict[str, Any]] | None:
    result = runner(["issue", "list", "--repo", tickets_repo, "--label", MIRROR_LABEL, "--state", "all",
                     "--limit", _LIMIT, "--json", _JSON_MIRROR], None)
    items, error = _parse(result, list, default="[]")
    if items is None:
        log(f"mirror: cannot read {tickets_repo}: {error}")
        return None
    return [item for item in items if isinstance(item, dict)]


def _pair_of(tickets_repo: str, mirror: dict[str, Any], runner: Runner, log: Callable[[str], Any]) -> _Pair | None:
    """The pair of a mirror whose first line names a source issue URL; `None` for any other mirror."""

    src = source_url_of(mirror.get("body") or "")
    if src is None:
        return None
    try:
        repo, number = parse_source_url(src)
    except ValueError:
        return None
    return _Pair(tickets_repo=tickets_repo, mirror=mirror, src=src, repo=repo, source_number=number,
                 runner=runner, log=log)


def _sync_pair(pair: _Pair, source: dict[str, Any], held: Callable[[int], bool]) -> str | None:
    mirror_open = str(pair.mirror.get("state") or "").upper() == "OPEN"
    source_open = str(source.get("state") or "").lower() == "open"
    if mirror_open == source_open:
        return None
    if source_open:
        return _reopen_or_close_source(pair)
    return _close_mirror(pair, source, held)


def _close_mirror(pair: _Pair, source: dict[str, Any], held: Callable[[int], bool]) -> str | None:
    number = pair.mirror_number
    closed_by = source.get("closed_by")
    login = str(closed_by.get("login")) if isinstance(closed_by, dict) and closed_by.get("login") else "unknown"
    when = source.get("closed_at") or "an unknown time"
    if held(number):
        return _hold_comment(pair, login, when)
    text = f"{CLOSED_MARK}\nSource {pair.src} was closed by {login} at {when}."
    if not _write(pair, ["issue", "close", str(number), "--repo", pair.tickets_repo, "--comment", text],
                  "close", f"mirror #{number}"):
        return None
    return f"closed mirror #{number}"


def _hold_comment(pair: _Pair, login: str, when: str) -> str | None:
    """One marker comment on a held mirror: the marker already there means the hold was announced before."""

    number = pair.mirror_number
    viewed, error = _parse(pair.runner(["issue", "view", str(number), "--repo", pair.tickets_repo, "--json",
                                        "comments"], None), dict)
    if viewed is None:
        pair.log(f"mirror: cannot read comments of mirror #{number}: {error}")
        return None
    if any(_first_line(comment.get("body")) == HELD_MARK for comment in _comments(viewed)):
        return None
    text = (f"{HELD_MARK}\nSource {pair.src} was closed by {login} at {when}. The mirror stays open while a session "
            "holds it; it closes after the hold ends.")
    if not _write(pair, ["issue", "comment", str(number), "--repo", pair.tickets_repo, "--body", text],
                  "comment on", f"mirror #{number}"):
        return None
    return f"waiting on hold #{number}"


def _reopen_or_close_source(pair: _Pair) -> str | None:
    """The mirror is closed and the source open: a reopen of the source after the mirror's close reopens the mirror;
    otherwise the mirror was closed first and the source closes."""

    events, error = _events(pair)
    if events is None:
        pair.log(f"mirror: cannot read the events of {pair.src}: {error}")
        return None
    closed_at = _when(pair.mirror.get("closedAt"))
    if any(event.get("event") == "reopened" and _after(event.get("created_at"), closed_at) for event in events):
        return _reopen_mirror(pair)
    return _close_source(pair)


def _reopen_mirror(pair: _Pair) -> str | None:
    number = pair.mirror_number
    text = f"{REOPENED_MARK}\nSource {pair.src} was reopened."
    if not _write(pair, ["issue", "reopen", str(number), "--repo", pair.tickets_repo, "--comment", text],
                  "reopen", f"mirror #{number}"):
        return None
    return f"reopened mirror #{number}"


def _close_source(pair: _Pair) -> str | None:
    number = pair.mirror_number
    viewed, error = _parse(pair.runner(["issue", "view", str(number), "--repo", pair.tickets_repo, "--json",
                                        "comments"], None), dict)
    if viewed is None:
        pair.log(f"mirror: cannot read comments of mirror #{number}: {error}")
        return None
    closing_url = _closing_url(pair, _comments(viewed))
    text = f"Closed from the mirror {pair.mirror_url}. Its closing comment: {closing_url}."
    if not _write(pair, ["issue", "close", str(pair.source_number), "--repo", pair.repo, "--comment", text],
                  "close", f"source {pair.src}"):
        return None
    return f"closed source {pair.repo}#{pair.source_number}"


def _events(pair: _Pair) -> tuple[list[dict[str, Any]] | None, str]:
    """The source's events over every page. `gh api --paginate` prints one JSON array per page, back to back."""

    result = pair.runner(["api", f"repos/{pair.repo}/issues/{pair.source_number}/events", "--paginate"], None)
    if result.returncode != 0:
        return None, _first_line(result.stderr)
    try:
        pages = _json_values(result.stdout or "")
    except ValueError as exc:
        return None, f"invalid JSON ({exc})"
    events = [event for page in pages if isinstance(page, list) for event in page if isinstance(event, dict)]
    return events, ""


def _json_values(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    text = text.strip()
    values: list[Any] = []
    index = 0
    while index < len(text):
        value, index = decoder.raw_decode(text, index)
        values.append(value)
        while index < len(text) and text[index].isspace():
            index += 1
    return values


def _write(pair: _Pair, args: list[str], action: str, ref: str) -> bool:
    """One write; a failure is logged as `mirror: cannot <action> <ref>: <err>`; the pair waits for the next cycle."""

    result = pair.runner(args, None)
    if result.returncode == 0:
        return True
    pair.log(f"mirror: cannot {action} {ref}: {_first_line(result.stderr)}")
    return False


def _parse(result: subprocess.CompletedProcess, want: type, default: str | None = None) -> tuple[Any, str]:
    """The JSON value of a call that must be a `want` (dict or list); `(None, reason)` for a failed call or a wrong
    shape. An empty answer reads as `default` when one is given."""

    if result.returncode != 0:
        return None, _first_line(result.stderr)
    text = result.stdout or ""
    if not text.strip() and default is not None:
        text = default
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"invalid JSON ({exc})"
    if not isinstance(data, want):
        return None, f"not a JSON {'object' if want is dict else 'list'}"
    return data, ""


def _comments(viewed: dict[str, Any]) -> list[dict[str, Any]]:
    return [comment for comment in (viewed.get("comments") or []) if isinstance(comment, dict)]


def _closing_url(pair: _Pair, comments: list[dict[str, Any]]) -> str:
    """The URL of the mirror's last comment that is not one of this pass's own markers; the mirror's URL with none."""

    for comment in reversed(comments):
        if _first_line(comment.get("body")).startswith(_SYNC_PREFIX):
            continue
        return str(comment.get("url") or pair.mirror_url)
    return pair.mirror_url


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _after(text: Any, moment: datetime | None) -> bool:
    when = _when(text)
    return when is not None and moment is not None and when > moment


def _first_line(text: Any) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0] if lines else ""
