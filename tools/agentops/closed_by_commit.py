"""#422: the dispatcher's net for a ticket closed by a commit or a pull request.

A ticket closes only by its own closing step (`python -m tools.agentops close`). A closing word in a commit, a pull
request title or body closes it too, on GitHub's side, and no check on the way sees every such door. So each dispatcher
cycle reads the home issues closed since the last cycle; one whose last close came from a commit (a `closed` event with
a `commit_id`, which a merged pull request's close has too) is reopened with a comment that names the commit and, when
the commit came in by a pull request, that pull request; the notice goes to ORCH and to the brain. The commit link is
the event's own `commit_url` (its repo need not be the home repo). A close with no commit (the close step, a close in
the web UI) is left alone, and so is an issue a `reopened` event followed.

The REST event carries no `commit_id` when the commit is in a private repository, so a last close with none is read
once more through GraphQL: the `closer` field of `Issue.timelineItems(CLOSED_EVENT)` is a Commit or a PullRequest
of any repo for a close by one, and null for a close by hand. A Commit or PullRequest there is a close by commit too;
a null one stays closed. The comment is public in the home repo, so a close by a private repo other than the home is named
there only as `a private repository` (no sha, no url, no repo name); the notice to ORCH and the brain, and the
log, carry the full name.

`is_closed_by_commit` is the pure rule over an issue's events; `sweep` is the one cycle, with every `gh` call going
through an injected runner (`(args, stdin) -> CompletedProcess`, the shape of `GhCli._run`), so a test never writes to
a real ticket. Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: the first line of the comment the net writes on a reopened ticket.
NOTE_MARK = "<!-- reopened-by-net -->"
#: the key of the last-read time in the dispatcher's state file (`orch_session.json`).
SINCE_KEY = "closed_by_commit_since"
#: the `--limit` of the closed-issue list. A list that returns exactly this many is cut short: `sweep` logs it and keeps
#: the excess for the next cycle.
LIST_LIMIT = 200
#: `/repos/<owner>/<repo>/commits/<sha>` inside a `closed` event's `commit_url` (the API URL of its commit).
_COMMIT_URL = re.compile(r"/repos/([^/]+/[^/]+)/commits/")

#: the last close's source, by GraphQL (the `closer` field): a Commit or a PullRequest, null for a hand close.
_SOURCE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){"
    "timelineItems(itemTypes:[CLOSED_EVENT],last:1){nodes{... on ClosedEvent{closer{__typename "
    "... on Commit{oid url repository{nameWithOwner isPrivate}} "
    "... on PullRequest{number url repository{nameWithOwner isPrivate}}}}}}}}}")
#: how the public comment names a commit or pull request in a private repository other than the home one.
PRIVATE_REPO = "a private repository"

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


@dataclass(frozen=True)
class _Closed:
    """How a close is named: `key` is what `Sweep.reopened` holds (the commit sha, or the pull request's url), `text` is
    the full name the log and the notice to ORCH and the brain carry, `public` is what the comment on the home ticket
    says (the same as `text`, or `a commit / a pull request in a private repository` with nothing else of it)."""

    key: str
    text: str
    public: str


@dataclass(frozen=True)
class Sweep:
    """One cycle's result: `since` is the time the next cycle reads from; `reopened` maps each reopened issue to the
    commit sha that closed it, or the url of the pull request that did (in full, also for a private repository)."""

    since: datetime
    reopened: dict[int, str] = field(default_factory=dict)


def closing_event(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The `closed` event that closed the issue, when its last close (GitHub lists events oldest first) has a
    `commit_id` and no `reopened` event follows it; `None` otherwise (a close step, a web-UI close, a reopened issue)."""

    last = last_close(events)
    if last is None:
        return None
    sha = last.get("commit_id")
    return last if isinstance(sha, str) and sha else None


def last_close(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The newest `closed` event when no `reopened` event follows it; `None` otherwise (open again, or never closed)."""

    last: Mapping[str, Any] | None = None
    for event in events:
        if isinstance(event, Mapping) and event.get("event") in ("closed", "reopened"):
            last = event
    return last if last is not None and last.get("event") == "closed" else None


def is_closed_by_commit(events: Iterable[Mapping[str, Any]]) -> str | None:
    """The commit sha that closed the issue (see `closing_event`); `None` otherwise."""

    event = closing_event(events)
    return event["commit_id"] if event is not None else None


def load_since(state_file: Path | None) -> datetime | None:
    """The last-read time the state file holds, or `None` (no file, no key, or an unreadable value)."""

    if state_file is None or not Path(state_file).is_file():
        return None
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
        return _parse_time(data.get(SINCE_KEY)) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def sweep(*, repo: str, since: datetime | None, now: datetime, runner: Runner,
          notify: Callable[[int, str], Any] | None = None,
          log: Callable[[str], Any] | None = None) -> Sweep:
    """One cycle. `now` is taken by the caller before this call, so an issue closed while the cycle runs is read next
    time. With no `since` (the first cycle) nothing is read: the clock starts. A failed list keeps `since`; a failed
    read or a failed reopen moves it back to that issue's close, so the next cycle tries again. A list that comes back
    full (`LIST_LIMIT` issues) is logged, and `since` stops at the oldest close it read: the excess is read next cycle."""

    if since is None:
        return Sweep(since=now)
    say = log or (lambda text: None)

    listed = runner(["issue", "list", "-R", repo, "--state", "closed",
                     "--search", f"closed:>={since.isoformat(timespec='seconds')}",
                     "--json", "number,closedAt", "--limit", str(LIST_LIMIT)], None)
    if listed.returncode != 0:
        say(f"closed-by-commit net: gh issue list failed: {(listed.stderr or '').strip()[:200]}")
        return Sweep(since=since)
    try:
        items = json.loads(listed.stdout)
    except ValueError as exc:
        say(f"closed-by-commit net: gh issue list: invalid JSON ({exc})")
        return Sweep(since=since)
    full = isinstance(items, list) and len(items) >= LIST_LIMIT

    hold: datetime | None = None  # the earliest close that must be tried again
    kept: list[datetime] = []  # the closes this cycle reads: the cap's oldest is the last one it can move past
    reopened: dict[int, str] = {}
    for item in sorted((i for i in items if isinstance(i, dict)), key=lambda i: int(i.get("number") or 0)):
        number = int(item.get("number") or 0)
        closed_at = _parse_time(item.get("closedAt"))
        if closed_at is not None and closed_at < since:
            continue  # the search answers at date precision; the close itself decides
        if closed_at is not None:
            kept.append(closed_at)
        retry_at = closed_at or since

        events = _read_events(runner, repo, number)
        if events is None:
            say(f"closed-by-commit net: #{number} events not read")
            hold = _earlier(hold, retry_at)
            continue
        event = closing_event(events)
        if event is not None:
            sha = event["commit_id"]
            repo_of_commit = _commit_repo(event)
            link = f"https://github.com/{repo_of_commit}/commit/{sha}" if repo_of_commit else None
            pull = _pull_of(runner, repo_of_commit, sha) if repo_of_commit else None
            named = _closed_by(sha, link, pull)
            closed = _Closed(sha, named, named)
        elif last_close(events) is None:
            continue  # open again after its close, or never closed
        else:
            source = _read_closing_source(runner, repo, number)
            if source is _FAILED:
                say(f"closed-by-commit net: #{number} closing source not read")
                hold = _earlier(hold, retry_at)
                continue
            closed = _name_closing_source(source, repo)
            if closed is None:
                continue  # no closing source: a close by hand, or the close step
        key, closed_by = closed.key, closed.text

        reopen = runner(["issue", "reopen", str(number), "-R", repo], None)
        if reopen.returncode != 0:
            say(f"closed-by-commit net: #{number} reopen failed: {(reopen.stderr or '').strip()[:200]}")
            hold = _earlier(hold, retry_at)
            continue
        reopened[number] = key
        note = runner(["issue", "comment", str(number), "-R", repo, "--body-file", "-"], _note(closed.public))
        if note.returncode != 0:
            say(f"closed-by-commit net: #{number} reopened, its comment failed: {(note.stderr or '').strip()[:200]}")
        say(f"closed-by-commit net: #{number} closed by {closed_by}, reopened")
        if notify is not None:
            try:
                notify(number, _line(number, closed_by))
            except Exception as exc:  # noqa: BLE001 -- a notice never stops the cycle
                say(f"closed-by-commit net: #{number} notice failed ({type(exc).__name__}: {exc})")

    cap_at: datetime | None = None
    if full:
        cap_at = min(kept, default=since)  # every kept close is at or after `since`, so `since` never moves back
        say(f"closed-by-commit net: the closed-issue list is full ({LIST_LIMIT}, the limit); "
            f"the rest is read next cycle, from {cap_at.isoformat(timespec='seconds')}")
    floor = min((t for t in (hold, cap_at) if t is not None), default=None)
    return Sweep(since=floor if floor is not None else now, reopened=reopened)


def _read_events(runner: Runner, repo: str, number: int) -> list[dict[str, Any]] | None:
    """The issue's events over every page (`gh api --paginate` prints one JSON array per page); `None` when the read
    fails or the text is not JSON arrays."""

    result = runner(["api", "--paginate", f"repos/{repo}/issues/{number}/events"], None)
    if result.returncode != 0:
        return None
    try:
        return _parse_pages(result.stdout or "")
    except ValueError:
        return None


_FAILED: Any = object()


def _read_closing_source(runner: Runner, repo: str, number: int) -> Mapping[str, Any] | None:
    """The object GraphQL `ClosedEvent.closer` holds for the last close (a Commit or a PullRequest), `None` for a close
    with none, `_FAILED` when the read fails or the answer has no timeline."""

    owner, _, name = repo.partition("/")
    result = runner(["api", "graphql", "-f", f"query={_SOURCE_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                     "-F", f"number={number}"], None)
    if result.returncode != 0:
        return _FAILED
    try:
        nodes = json.loads(result.stdout or "")["data"]["repository"]["issue"]["timelineItems"]["nodes"]
    except (ValueError, KeyError, TypeError):
        return _FAILED
    if not isinstance(nodes, list):
        return _FAILED
    source = nodes[-1].get("closer") if nodes and isinstance(nodes[-1], Mapping) else None
    return source if isinstance(source, Mapping) else None


def _name_closing_source(source: Mapping[str, Any] | None, home: str) -> _Closed | None:
    """The close by a Commit or PullRequest named for the notice (in full) and for the public comment (a repo other than
    `home` that is private, or not known to be public, is `a private repository`, nothing else of it); `None` for no
    source or any other kind."""

    if source is None:
        return None
    kind = source.get("__typename")
    where = source.get("repository")
    where = where if isinstance(where, Mapping) else {}
    repo_of = where.get("nameWithOwner") if isinstance(where.get("nameWithOwner"), str) else None
    private = (where.get("isPrivate") is not False
               and not (repo_of is not None and repo_of.lower() == home.lower()))
    url = source.get("url") if isinstance(source.get("url"), str) else None
    in_repo = f" in {repo_of}" if private and repo_of else ""
    if kind == "Commit" and isinstance(source.get("oid"), str) and source["oid"]:
        sha = source["oid"]
        full = _closed_by(sha, url, None).replace(f"commit {sha}", f"commit {sha}{in_repo}", 1)
        return _Closed(sha, full, f"a commit in {PRIVATE_REPO}" if private else full)
    if kind == "PullRequest" and isinstance(source.get("number"), int):
        number = source["number"]
        full = f"pull request #{number}{in_repo}" + (f" ({url})" if url else "")
        return _Closed(url or f"#{number}", full, f"a pull request in {PRIVATE_REPO}" if private else full)
    return None


def _commit_repo(event: Mapping[str, Any]) -> str | None:
    """`owner/repo` of the event's own `commit_url`, the repo the commit is in; `None` when the event has none."""

    url = event.get("commit_url")
    match = _COMMIT_URL.search(url) if isinstance(url, str) else None
    return match.group(1) if match else None


def _pull_of(runner: Runner, repo: str, sha: str) -> tuple[int, str] | None:
    """The pull request the commit came in by, as (number, url): the merged one when several list it, else the first.
    `None` when none is listed or the read fails; the note then names the commit only."""

    result = runner(["api", f"repos/{repo}/commits/{sha}/pulls"], None)
    if result.returncode != 0:
        return None
    try:
        pulls = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(pulls, list):
        return None
    found = [p for p in pulls if isinstance(p, dict) and isinstance(p.get("number"), int)]
    if not found:
        return None
    pick = next((p for p in found if p.get("merged_at")), found[0])
    number = pick["number"]
    return number, f"https://github.com/{repo}/pull/{number}"


def _closed_by(sha: str, link: str | None, pull: tuple[int, str] | None) -> str:
    """How the note and the notice name the close: the commit, with its link when the event gives one, and in front of
    it the pull request that brought the commit in, when one is found."""

    commit = f"commit {sha}" + (f" ({link})" if link else "")
    if pull is None:
        return commit
    number, url = pull
    return f"pull request #{number} ({url}), through {commit}"


def _parse_pages(text: str) -> list[dict[str, Any]]:
    """The events of one or more JSON arrays printed back to back; raises ValueError on anything else."""

    decoder = json.JSONDecoder()
    events: list[dict[str, Any]] = []
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return events
        value, pos = decoder.raw_decode(text, pos)
        if not isinstance(value, list):
            raise ValueError("not a JSON array of events")
        events.extend(e for e in value if isinstance(e, dict))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _earlier(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b < a else a


def _note(closed_by: str) -> str:
    return (f"{NOTE_MARK}\n"
            f"Reopened by the dispatcher's net: this ticket was closed by {closed_by}, not by its closing step. "
            "A ticket closes only by its closing step (`python -m tools.agentops close`), after its rows are judged; "
            "a commit or a pull request does not close it.\n")


def _line(number: int, closed_by: str) -> str:
    return f"#{number} was closed by {closed_by} and is reopened: it closes only by its closing step"
