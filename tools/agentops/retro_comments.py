"""Issue 109 (unit 2): ticket-comment link failures for the weekly retro, kinds d, e, f, g.

``comment_failures(repo, since, until, run_gh) -> list[dict]`` returns one row per kind d, e, f, g, each
``{"kind", "count", "lost_s", "pointers", "note"}``: ``lost_s`` raw seconds or None when the record gives no time,
``pointers`` at most three URLs. ``run_gh(args) -> stdout text`` is the injected gh seam (args are the gh arguments;
it raises or returns None or non-JSON text when the read fails, and that kind is then `no source`).

Cost: one GraphQL search per kind (four calls), plus one batched read of the states of the issues that a parked
ticket's `until #N closes` form names, only when such a form exists. Nothing is written.

Kinds a, b, c and h belong to another unit and are not returned here. The parked evaluator is
``parked_return.ParkedReturn``'s own parser and its ``_reached`` check (read-only, that module is not edited); its
issue-state read is answered from the batched read, its target core from ``default_target_core`` of the repo's settings.

Standard library plus ``.comment_markers`` and ``.parked_return`` only."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import core_settings
from .comment_markers import first_line, is_machine_comment
from .parked_return import CONFIG_REL, HUMAN_WAITS, REPO_ROOT, ParkedReturn, default_target_core, parse_parked_line

#: the dispatcher's alarm comment (`dispatch.py` ALARM_COMMENT_TEXT, posted by `_resolve_outcome` at :4039 as
#: "ORCH session <id> ended without a closing or parking checkpoint"): kind d
ALARM_TEXT = "ended without a closing or parking checkpoint"
#: kind e: the takeover texts counted: "taking over #N from <id>" and "taking over from <id>" (the issue's wording and
#: the real ORCH checkpoints, docs/research/2026-10-16-comment-tasks-rows.jsonl), and "takes #N over" (the form the
#: dispatcher reads a takeover note by, dispatch.py:2510; no code posts it). "continuing #N" has no source and is not counted.
TAKEOVER_RE = re.compile(r"taking over (?:#\d+ )?from\b|\btakes #\d+ over\b", re.IGNORECASE)
#: the search text for kind e: both wordings in one search
TAKEOVER_SEARCH = '("taking over" OR "takes")'
#: kind f: the merge result comment is `merge_handback.MARKER` on its first line, then the visible text of `_body`
#: ("Merge of <branch> landed on main ..." or "Merge of <branch> did not land ..."). GitHub search does not index the
#: HTML marker, so the search reads the visible text and the marker is checked on the fetched body.
MERGE_MARKER = "<!-- merge result -->"
MERGE_SEARCH_TEXT = "Merge of"
#: kind f: a merge result with no closer, and more than this long between it and the window end, is a failure
STALE_AFTER = timedelta(days=1)
#: an ORCH closing comment's first line, "Closing N (ORCH <id>) ..." (the same form `comment_markers.ORCH_LINE` reads)
CLOSING_RE = re.compile(r"^[#*_\s]*Closing\b.*\(ORCH\b")

NO_SOURCE = "no source"
POINTER_CAP = 3

COMMENTS_QUERY = (
    "query($q: String!) { search(query: $q, type: ISSUE, first: 100) { issueCount nodes { ... on Issue {"
    " number url state closedAt comments(last: 100) { nodes { body createdAt url author { login } } } } } } }"
)
PARKED_QUERY = (
    "query($q: String!) { search(query: $q, type: ISSUE, first: 100) { issueCount nodes { ... on Issue {"
    " number url body labels(first: 20) { nodes { name } } } } } }"
)


def _now() -> datetime:
    """The clock a closer is judged against: a closer counts up to now, not only up to the window end."""
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """A naive datetime is local time (the parked form's rule); an aware one is kept."""
    return moment if moment.tzinfo is not None else moment.astimezone()


def _parse(stamp) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _graphql(run_gh: Callable, query: str, variables: dict) -> dict | None:
    """The `data` object of one `gh api graphql` read; None when the call fails or answers no usable JSON."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f", f"{key}={value}"]
    try:
        text = run_gh(args)
    except Exception:  # noqa: BLE001 -- a failed read is no source for the kind it serves
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    body = data.get("data") if isinstance(data, dict) else None
    return body if isinstance(body, dict) else None


def _search(run_gh: Callable, query: str, q: str) -> tuple[list[dict], int] | None:
    """The nodes of one search and the search's issue count; None when the read gave no source."""
    data = _graphql(run_gh, query, {"q": q})
    search = data.get("search") if data is not None else None
    if not isinstance(search, dict):
        return None
    nodes = [node for node in (search.get("nodes") or []) if isinstance(node, dict)]
    return nodes, int(search.get("issueCount") or len(nodes))


def _window_comments(nodes: list[dict], since: datetime, until: datetime):
    """(issue node, comment, createdAt) of every comment of the nodes that was written in the window."""
    for node in nodes:
        for comment in (node.get("comments") or {}).get("nodes") or []:
            if not isinstance(comment, dict):
                continue
            created = _parse(comment.get("createdAt"))
            if created is not None and since <= created <= until:
                yield node, comment, created


def _is_closer(body: str) -> bool:
    """A person's comment, or ORCH's own closing line: either closes a merge result's loop. Other machine lines do not."""
    return not is_machine_comment(body) or CLOSING_RE.match(first_line(body) or "") is not None


def _closed_later(node: dict, created: datetime, now: datetime) -> bool:
    """A closer after a merge result: the issue closed at any time from the merge result up to now, or a later
    closer comment (`_is_closer`) written up to now."""
    closed = _parse(node.get("closedAt"))
    if closed is not None and created <= closed <= now:
        return True
    for comment in (node.get("comments") or {}).get("nodes") or []:
        if not isinstance(comment, dict):
            continue
        at = _parse(comment.get("createdAt"))
        if at is not None and created < at <= now and _is_closer(str(comment.get("body") or "")):
            return True
    return False


def _urls(items: list[str]) -> list[str]:
    return [url for url in items if url][:POINTER_CAP]


def _row(kind: str, count: int, lost_s: int | None, pointers: list[str], note: str) -> dict:
    return {"kind": kind, "count": count, "lost_s": lost_s, "pointers": pointers, "note": note}


def _note(text: str, found: tuple[list[dict], int]) -> str:
    nodes, total = found
    if total > len(nodes):
        return f"{text}; the search read {len(nodes)} of {total} issues"
    return text


def _candidates(nodes: list[dict]) -> list[tuple[dict, object]]:
    """(issue node, parsed Form) of each parked ticket whose `Parked:` line is a machine form and that a human wait does not hold."""
    out = []
    for node in nodes:
        labels = {label.get("name") for label in (node.get("labels") or {}).get("nodes") or [] if isinstance(label, dict)}
        if labels & HUMAN_WAITS:
            continue
        form = parse_parked_line(str(node.get("body") or ""))
        if form is not None:
            out.append((node, form))
    return out


def _issue_states(run_gh: Callable, refs: list[tuple[str, int]]) -> dict | None:
    """The states of the referenced issues in ONE batched read; None when that read gives no source."""
    if not refs:
        return {}
    parts = []
    for i, (owner_repo, number) in enumerate(refs):
        owner, _, name = owner_repo.partition("/")
        parts.append(f'r{i}: repository(owner: "{owner}", name: "{name}") {{ issue(number: {number}) {{ state }} }}')
    data = _graphql(run_gh, "query { " + " ".join(parts) + " }", {})
    if data is None:
        return None
    states = {}
    for i, ref in enumerate(refs):
        issue = (data.get(f"r{i}") or {}).get("issue") or {}
        if issue.get("state") in ("OPEN", "CLOSED"):
            states[ref] = issue["state"]
    return states


class _StateSeam:
    """The `gh` seam `ParkedReturn._reached` reads: the issue states of the one batched read."""

    def __init__(self, states: dict):
        self._states = states

    def issue_state(self, owner_repo: str, number: int) -> str | None:
        return self._states.get((owner_repo, number))


def _settings() -> dict:
    try:
        return json.loads((REPO_ROOT / CONFIG_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stale_parked(run_gh: Callable, repo: str, nodes: list[dict], until: datetime) -> tuple[list[tuple[dict, object, int | None]], int]:
    """The parked tickets whose event has passed, as (issue node, form, seconds from the event to `until` or None when
    the event is not a date), and the count of issue-form tickets whose state read failed."""
    candidates = _candidates(nodes)
    refs = sorted({(form.owner_repo or repo, form.number) for _, form in candidates if form.kind == "issue"})
    states = _issue_states(run_gh, refs)
    unread = 0
    if states is None:
        states, unread = {}, sum(1 for _, form in candidates if form.kind == "issue")
    has_core = any(form.kind == "core" for _, form in candidates)
    target_core = default_target_core(_settings(), REPO_ROOT) if has_core else (lambda owner_repo: None)
    now_local = until.astimezone().replace(tzinfo=None)
    checker = ParkedReturn(repo, _StateSeam(states), now=lambda: now_local, target_core=target_core,
                           home=str(core_settings.load()["repo"]))
    stale = []
    for node, form in candidates:
        if form.kind == "issue" and (form.owner_repo or repo, form.number) not in states:
            continue
        if checker._reached(form, now_local, node["number"]) is None:
            continue
        lost = int((now_local - form.at).total_seconds()) if form.kind == "date" else None
        stale.append((node, form, lost))
    return stale, unread


def comment_failures(repo: str | None, since: datetime, until: datetime,
                     run_gh: Callable[[list[str]], str]) -> list[dict]:
    """The link failures of kinds d, e, f, g in the window `[since, until]` of `repo`'s tickets, one row each.

    ``repo``: the repo whose tickets are read; None = the installation's tickets repo (#323). A repo named in a
    parked form (`owner_repo`) is read as it is named.
    ``since``/``until``: datetimes (naive = local time). ``run_gh(args)`` returns the stdout text of ``gh <args>``.
    """
    repo = repo or core_settings.tickets_repo()
    start, end, now = _aware(since), _aware(until), _now()
    since_day = start.astimezone(timezone.utc).strftime("%Y-%m-%d")
    scope = f"repo:{repo} is:issue"
    rows: list[dict] = []

    d = _search(run_gh, COMMENTS_QUERY, f'{scope} in:comments "{ALARM_TEXT}" updated:>={since_day}')
    if d is None:
        rows.append(_row("d", 0, None, [], NO_SOURCE))
    else:
        hits = [c for _, c, _ in _window_comments(d[0], start, end) if ALARM_TEXT in str(c.get("body") or "")]
        rows.append(_row("d", len(hits), None, _urls([c.get("url") for c in hits]),
                         _note("the dispatcher's alarm: the session ended holding the ticket (dispatch.py _resolve_outcome); "
                               "the record gives no time lost", d)))

    e = _search(run_gh, COMMENTS_QUERY, f"{scope} in:comments {TAKEOVER_SEARCH} updated:>={since_day}")
    if e is None:
        rows.append(_row("e", 0, None, [], NO_SOURCE))
    else:
        hits = [c for _, c, _ in _window_comments(e[0], start, end) if TAKEOVER_RE.search(str(c.get("body") or ""))]
        rows.append(_row("e", len(hits), None, _urls([c.get("url") for c in hits]),
                         _note("a second ORCH took the ticket over (texts counted: `taking over [#N] from`, `takes #N over`); "
                               "the record gives no time lost", e)))

    f = _search(run_gh, COMMENTS_QUERY, f'{scope} in:comments "{MERGE_SEARCH_TEXT}" updated:>={since_day}')
    if f is None:
        rows.append(_row("f", 0, None, [], NO_SOURCE))
    else:
        failures = []
        for node, comment, created in _window_comments(f[0], start, end):
            if not str(comment.get("body") or "").lstrip().startswith(MERGE_MARKER):
                continue
            if _closed_later(node, created, now) or end - created <= STALE_AFTER:
                continue
            failures.append((comment, int((end - created).total_seconds())))
        rows.append(_row("f", len(failures), sum(lost for _, lost in failures),
                         _urls([c.get("url") for c, _ in failures]),
                         _note("a merge result with no closer (issue closed, or a person's or ORCH's Closing comment, up to "
                               "now) for over a day; lost_s is from the merge result to the window end", f)))

    g = _search(run_gh, PARKED_QUERY, f"{scope} is:open label:parked")
    if g is None:
        rows.append(_row("g", 0, None, [], NO_SOURCE))
    else:
        stale, unread = _stale_parked(run_gh, repo, g[0], end)
        dated = [lost for _, _, lost in stale if lost is not None]
        note = _note("a parked ticket whose Parked: event passed and that still carries parked; lost_s sums the stale "
                     "tickets with a known event time (a date form), from the event to the window end", g)
        undated = len(stale) - len(dated)
        if undated:
            note += f"; {undated} of {len(stale)} stale ticket(s) had no event time (core or issue form)"
        if unread:
            note += f"; {unread} issue-form ticket(s) unread (the state read failed)"
        rows.append(_row("g", len(stale), sum(dated) if dated else None,
                         _urls([node.get("url") for node, _, _ in stale]), note))
    return rows
