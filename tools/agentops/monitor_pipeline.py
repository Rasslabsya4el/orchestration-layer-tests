"""The monitor's pipeline: one row per ticket that is ready for an agent, held by ORCH, or in the merge queue (#6).

Two pure functions over facts the collector already reads (no new query for the agents or the merge line):

* ``pipeline_facts(issues, repo=, gh=, cache=)``: the facts of the open tickets (title, labels, open blockers) and
  the time each pipeline-eligible ticket got its ``ready-for-agent`` label. That time is one ``gh api`` timeline
  call per ticket, cached in ``cache`` until the ticket's label set changes and kept after the label is removed
  (it leaves the cache when the ticket closes); a failed call is not cached, so the
  next pull asks again. ``gh`` is injected: the collector passes ``gh_call.run_gh``; a test passes a fake.
  #151: ``fill_row_times(facts, row_numbers(facts, sessions, merge), ...)`` gives the same time to a Pipeline row's
  ticket that was never seen eligible (one timeline call, cached); a ticket with no such event keeps no time.
* ``claim_times(events, session_ids)``: from the dispatcher's events file (``artifacts/runs/events.jsonl``, read by
  the caller), the time of each issue's ``claimed`` event (``issue_taken``, how ``claimed``) in the session's own
  window (at or after its ``orch_start``). Pure: the caller reads the file.
* ``pipeline_rows(facts, sessions, merge)``: the rows, from those facts, the ``agents`` payload's sessions (their
  ``held`` issues, each with its ``claimed_at`` when known, and flat ``subagents``) and the ``merge`` payload
  (``running`` with its ``queued`` time, ``waiting``).

A row is ``{number, title, stage, failed, workers, strong, researchers, merge_position, merge_percent,
started_at, stage_since, units, wait, returned}`` (``units`` is ``{done, total}`` or None, #151; #171: ``wait`` is
``{reason, until}`` for a parked ticket that landed, ``until`` an ISO local time or None, else None; ``returned`` is
True when a send-back is newer than the ticket's newest landed merge row (rc 0)); the stage is ``merge`` > ``accept`` >
``work`` > ``orch`` > ``ready`` (furthest first), then the oldest ``started_at`` first. ``stage_since`` is the time the
row's stage began: the claim (``orch``), the earliest live subagent (``work``), the merge enqueue (``merge``), the
landing (``accept``: an open ticket whose newest merge-line row landed, #154), the ``ready-for-agent`` label
(``ready``). Pure: no file, no clock.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, Iterable

READY_LABEL = "ready-for-agent"
PARKED_LABEL = "parked"
WORKER_TYPES = ("worker", "worker-strong")
RESEARCHER_TYPES = ("researcher-find", "researcher-how")
STAGE_RANK = {"merge": 0, "accept": 1, "work": 2, "orch": 3, "ready": 4}
TIMELINE_PATH = "repos/{repo}/issues/{number}/timeline"
#: #171: a body's wait line, the parked-on-run marker, the send-back marker (each read on a comment's first line)
PARKED_PREFIX = "Parked:"
PARKED_RUN_MARK = "<!-- parked-on-run:"
SEND_BACK_MARK = "<!-- send-back -->"
WAIT_REASON_CHARS = 60
UNTIL_DATE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _events(stdout: str) -> list[dict]:
    """The timeline events of a `gh api --paginate` answer: its pages are arrays printed one after the other."""
    decoder = json.JSONDecoder()
    events: list[dict] = []
    i = 0
    while i < len(stdout):
        while i < len(stdout) and stdout[i].isspace():
            i += 1
        if i >= len(stdout):
            break
        page, i = decoder.raw_decode(stdout, i)
        if isinstance(page, list):
            events.extend(e for e in page if isinstance(e, dict))
    return events


def _ready_since(stdout: str) -> str | None:
    """The time of the first `ready-for-agent` label event in a timeline answer, or None (#151: the first, not the
    newest, so a label ORCH removes on a take does not move the time the ticket started)."""
    for event in _events(stdout):
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == READY_LABEL:
            return event.get("created_at") or None
    return None


def label_times(labelled: dict[int, list[str]], *, repo: str, gh: Callable[[list[str]], Any],
                cache: dict[str, dict]) -> dict[int, str | None]:
    """The ``ready-for-agent`` time of each ticket in ``labelled`` (number -> its labels). ``cache`` maps a number
    (as str) to ``{"labels": [...], "at": ...}``: a ticket whose label set is unchanged is not asked again. A ticket
    no longer in ``labelled`` keeps its entry (the caller drops it only when the ticket closes). A failed call gives
    None and is not cached."""
    times: dict[int, str | None] = {}
    for number, labels in labelled.items():
        key = str(number)
        names = sorted(labels)
        entry = cache.get(key)
        if isinstance(entry, dict) and entry.get("labels") == names:
            times[number] = entry.get("at")
            continue
        try:
            res = gh(["api", TIMELINE_PATH.format(repo=repo, number=number), "--paginate"])
            at = _ready_since(res.stdout) if res.returncode == 0 else None
        except Exception:  # noqa: BLE001 - a timeout or a bad answer is a missing time for this pull, asked again next pull
            times[number] = None
            continue
        if res.returncode == 0:
            cache[key] = {"labels": names, "at": at}
        times[number] = at
    return times


def pipeline_facts(issues: dict[int, dict], *, repo: str, gh: Callable[[list[str]], Any],
                   cache: dict[str, dict], cut_totals: dict[int, int] | None = None) -> dict[str, dict]:
    """The facts of the open tickets (the collector's `issues` map), JSON-ready, keyed by number as str. Each
    pipeline-eligible ticket (open, ready-for-agent, not parked, no open blocker) gets ``label_at`` when known.
    #151: an open ticket with a known cut-table total (``cut_totals``: number -> n) gets ``cut_total``."""
    facts: dict[str, dict] = {}
    eligible: dict[int, list[str]] = {}
    for number, issue in issues.items():
        if not issue.get("open"):
            continue
        labels = list(issue.get("labels") or [])
        blockers = list(issue.get("blockers") or [])
        facts[str(number)] = {"title": str(issue.get("title") or ""), "open": True, "labels": labels,
                              "blockers": blockers}
        for key in ("parked_line", "parked_run", "send_back_at"):  # #171: the wait and return facts, when present
            if issue.get(key):
                facts[str(number)][key] = str(issue[key])
        if cut_totals and cut_totals.get(number) is not None:
            facts[str(number)]["cut_total"] = cut_totals[number]
        if READY_LABEL in labels and PARKED_LABEL not in labels and not blockers:
            eligible[int(number)] = labels
    for number, at in label_times(eligible, repo=repo, gh=gh, cache=cache).items():
        if at:
            facts[str(number)]["label_at"] = at
    # #151: an open ticket that was eligible and no longer is (ORCH took it: its label is gone) keeps the time it was
    # cached with, without a new gh call; a ticket that closed or left the map leaves the cache
    eligible_keys = {str(n) for n in eligible}
    for key, fact in facts.items():
        entry = cache.get(key)
        if "label_at" not in fact and key not in eligible_keys and isinstance(entry, dict) and entry.get("at"):
            fact["label_at"] = entry["at"]
    open_keys = {str(n) for n, issue in issues.items() if issue.get("open")}
    for key in list(cache):
        if key not in open_keys:
            del cache[key]
    return facts


def cut_totals_of(facts: dict) -> dict[int, int]:
    """#151: the ``cut_total`` of each fact that has one (number as int -> n >= 2); the collector reads it back
    from the facts file for the ORCH rows. A fact with no int ``cut_total`` is skipped."""
    totals: dict[int, int] = {}
    for key, fact in (facts or {}).items():
        total = fact.get("cut_total") if isinstance(fact, dict) else None
        if isinstance(total, int) and total >= 2 and str(key).isdigit():
            totals[int(key)] = total
    return totals


CLAIMED_KIND = "issue_taken"
CLAIMED_HOW = "claimed"


def session_start(events: list[dict], session_ids: Any) -> str:
    """#6: the time of the session's newest ``orch_start`` event (its ids: its id and its transcript uuid), or ""."""
    ids = {str(s) for s in session_ids or [] if s}
    start = ""
    for event in events:
        if event.get("kind") == "orch_start" and str(event.get("session") or "") in ids:
            start = str(event.get("ts") or "")
    return start


def claim_times(events: list[dict], session_ids: Any) -> dict[int, str]:
    """#6: issue number -> the time of its ``claimed`` event (``issue_taken``, how ``claimed``) for the session whose
    ids are ``session_ids``. The window is the session's own: a claim at or after its ``orch_start`` (see
    ``session_start``); an older session's claim is none of this session's, and a session with no ``orch_start``
    in the file has no claims at all. An event whose ``issue`` is not a number is skipped. ``events`` are the parsed
    lines of the events file, in file order."""
    start = session_start(events, session_ids)
    if not start:
        return {}
    times: dict[int, str] = {}
    for event in events:
        if event.get("kind") != CLAIMED_KIND or event.get("how") != CLAIMED_HOW or event.get("issue") is None:
            continue
        try:
            number = int(event["issue"])
        except (TypeError, ValueError):
            continue  # a garbled issue is no claim
        stamp = str(event.get("ts") or "")
        if stamp < start:
            continue
        times[number] = stamp  # file order: the newest claim of the issue wins
    return times


def _ready(fact: dict) -> bool:
    labels = fact.get("labels") or []
    return bool(fact.get("open")) and READY_LABEL in labels and PARKED_LABEL not in labels \
        and not fact.get("blockers")


def _merge_marks(merge: Any) -> dict[int, dict]:
    """number -> its place in the merge line: 1 = the running suite, then the waiters in order (the running suite
    takes place 1, so the first waiter is 2). A missing or unreadable merge line has no marks."""
    if not isinstance(merge, dict) or merge.get("missing"):
        return {}
    marks: dict[int, dict] = {}
    run = merge.get("running") or None
    offset = 0
    if run:
        offset = 1
        percent = run.get("percent")
        for n in run.get("issues") or []:
            marks[int(n)] = {"position": 1, "percent": int(percent) if percent is not None else None,
                             "failed": bool(run.get("failed")),
                             "since": run.get("queued") or run.get("started")}  # no claimed ticket: the suite start
    for waiter in merge.get("waiting") or []:
        for n in waiter.get("issues") or []:
            marks.setdefault(int(n), {"position": offset + int(waiter.get("position") or 1), "percent": None,
                                      "failed": False, "since": waiter.get("queued")})
    return marks


def _newest_landed(merge: Any, *, rc_zero_only: bool = False) -> dict[int, dict]:
    """#171: number -> its newest merge-line row that has a ``landed`` time (with ``rc_zero_only``: only the rows that
    landed, rc 0). Newest is by that time, not the list order."""
    if not isinstance(merge, dict) or merge.get("missing"):
        return {}
    newest: dict[int, dict] = {}
    for row in merge.get("landed") or []:
        if not isinstance(row, dict) or not row.get("landed"):
            continue
        if rc_zero_only and row.get("rc") != 0:
            continue  # a did-not-land row is no landing
        for raw in row.get("issues") or []:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue  # a garbled issue is no landing
            prev = newest.get(number)
            if prev is None or str(row["landed"]) >= str(prev["landed"]):
                newest[number] = row
    return newest


def _accepted(merge: Any) -> dict[int, str]:
    """#154: number -> the landed time of its newest merge-line row, for each ticket whose newest row landed (rc 0).
    Newest is by the row's ``landed`` time, not the list order. A ticket whose newest row did not land is not here."""
    return {n: str(row["landed"]) for n, row in _newest_landed(merge).items() if row.get("rc") == 0}


def parked_line(body: str | None) -> str | None:
    """#171: the body's first line that opens with ``Parked:`` (stripped), or None."""
    for line in (body or "").splitlines():
        if line.strip().startswith(PARKED_PREFIX):
            return line.strip()
    return None


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def parked_run_line(comments: Any) -> str | None:
    """#171: the first line of the newest ``<!-- parked-on-run: ... -->`` comment (a dict per comment with ``body``
    and ``createdAt``), or None."""
    newest_at, newest_line = "", None
    for c in comments or []:
        first = _first_line(c.get("body"))
        stamp = str(c.get("createdAt") or "")
        if first.startswith(PARKED_RUN_MARK) and (newest_line is None or stamp >= newest_at):
            newest_at, newest_line = stamp, first
    return newest_line


def send_back_at(comments: Any) -> str | None:
    """#171: the ``createdAt`` of the newest ``<!-- send-back -->`` comment (its first line holds the marker), or None."""
    newest = None
    for c in comments or []:
        stamp = str(c.get("createdAt") or "")
        if SEND_BACK_MARK in _first_line(c.get("body")) and stamp and (newest is None or stamp > newest):
            newest = stamp
    return newest


def _cut(text: str, limit: int = WAIT_REASON_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _wait_text(source: str) -> str:
    """The text of a wait line: a ``Parked:`` line without its word, or a parked-on-run line without its marks."""
    text = source.strip()
    if text.startswith(PARKED_PREFIX):
        return text[len(PARKED_PREFIX):].strip()
    if text.startswith(PARKED_RUN_MARK):
        text = text[len(PARKED_RUN_MARK):].strip()
        if text.endswith("-->"):
            text = text[:-3].strip()
    return text


def _wait_of(fact: dict) -> dict:
    """#171 rule 1: a parked ticket's wait. reason = its ``Parked:`` line's text (else the newest parked-on-run line's),
    cut to about 60 characters; until = the first ``YYYY-MM-DD HH:MM`` in that text as a local ISO time, or None."""
    text = _wait_text(str(fact.get("parked_line") or fact.get("parked_run") or ""))
    until = None
    found = UNTIL_DATE.search(text)
    if found:
        try:
            until = datetime.strptime(found.group(0), "%Y-%m-%d %H:%M").isoformat(timespec="minutes")
        except ValueError:
            until = None
    return {"reason": _cut(text) or PARKED_LABEL, "until": until}


def _instant(stamp: Any) -> datetime | None:
    """#171: a time as an aware datetime: a GitHub ``Z`` time is UTC, a naive one (a merge row's ``landed``, written in
    local time) is local time."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone() if moment.tzinfo is None else moment


def _returned(fact: dict, landed_at: str | None) -> bool:
    """#171 rule 2: the ticket's newest send-back comment is newer than its newest landed merge row (rc 0; ``landed_at``
    is None when the ticket has no such row)."""
    sent, landed = _instant(fact.get("send_back_at")), _instant(landed_at)
    return sent is not None and landed is not None and sent > landed


def row_numbers(facts: dict, sessions: list[dict], merge: Any) -> set[int]:
    """#151: the numbers that get a Pipeline row: the held tickets, the merge line's and the accepted (#154), when open in the facts, and
    every ready ticket (open, ready-for-agent, not parked, no open blocker)."""
    tickets = {int(k): v for k, v in (facts or {}).items()}
    held = {int(h["number"]) for session in sessions or [] for h in session.get("held") or []}
    numbers = {n for n in set(_merge_marks(merge)) | held | set(_accepted(merge)) if tickets.get(n, {}).get("open")}
    numbers |= {n for n, fact in tickets.items() if _ready(fact)}
    return numbers


def fill_row_times(facts: dict[str, dict], numbers: Iterable[int], *, repo: str, gh: Callable[[list[str]], Any],
                   cache: dict[str, dict]) -> bool:
    """#151 "с начала": the ``label_at`` of each Pipeline row's ticket in ``numbers`` (see ``row_numbers``) that the
    label cache has no entry for: one timeline call per such ticket, its first ``ready-for-agent`` time (None when
    the timeline has no such event) cached with the ticket's labels. A ticket already carrying a ``label_at``, or not
    open in ``facts``, is not asked; a failed call is not cached, so the next pull asks again. Mutates ``facts``;
    returns True when the cache changed (the caller writes it)."""
    changed = False
    for number in numbers:
        key = str(number)
        fact = facts.get(key)
        if not isinstance(fact, dict) or not fact.get("open") or fact.get("label_at"):
            continue
        entry = cache.get(key)
        if isinstance(entry, dict) and "at" in entry:
            at = entry.get("at")
        else:
            try:
                res = gh(["api", TIMELINE_PATH.format(repo=repo, number=number), "--paginate"])
            except Exception:  # noqa: BLE001 - a timeout or a bad answer is a missing time for this pull, asked again next pull
                continue
            if res.returncode != 0:
                continue
            at = _ready_since(res.stdout)
            cache[key] = {"labels": sorted(fact.get("labels") or []), "at": at}
            changed = True
        if at:
            fact["label_at"] = at
    return changed


def _row_units(number: int, held: bool, fact: dict, units: dict | None) -> dict[str, int] | None:
    """#151: a row's `units`: the outcome log's progress of the ticket, else (held only) its cut-table total as
    {0, n}, else None."""
    progress = (units or {}).get(str(number))
    if isinstance(progress, dict):
        return {"done": int(progress["done"]), "total": int(progress["total"])}
    total = fact.get("cut_total")
    if held and isinstance(total, int) and total >= 2:
        return {"done": 0, "total": total}
    return None


def pipeline_rows(facts: dict, sessions: list[dict], merge: Any, units: dict | None = None) -> list[dict]:
    """One row per pipeline ticket, furthest stage first, then the oldest ``started_at`` first.
    #151: ``units`` (issue number as str -> ``{done, total}``) is the outcome log's ``k/n`` progress; a held ticket
    with no such entry but a ``cut_total`` fact gets ``{done: 0, total: n}``; every other row without one is None."""
    tickets = {int(k): v for k, v in (facts or {}).items()}
    held: set[int] = set()
    claimed: dict[int, str] = {}
    live: dict[int, list[dict]] = {}
    for session in sessions or []:
        for h in session.get("held") or []:
            held.add(int(h["number"]))
            if h.get("claimed_at"):
                claimed[int(h["number"])] = h["claimed_at"]
        for sub in session.get("subagents") or []:
            if sub.get("issue") is not None and sub.get("busy"):
                live.setdefault(int(sub["issue"]), []).append(sub)
    marks = _merge_marks(merge)
    landed = _accepted(merge)
    # #171: the newest landed row (rc 0) per ticket, for the send-back check: a did-not-land row is no landing
    landed_ok = {n: str(row["landed"]) for n, row in _newest_landed(merge, rc_zero_only=True).items()}
    # a ticket leaves the pipeline when it closes, and one the facts do not know (absent) has no row either
    numbers = row_numbers(facts, sessions, merge)

    rows: list[dict] = []
    for n in numbers:
        fact = tickets.get(n, {})
        subs = live.get(n, [])
        mark = marks.get(n)
        label_at = fact.get("label_at") or None
        # #151: a ticket with no label time (its label removed on a take) starts at its claim time, then None
        started = label_at or claimed.get(n)
        wait, returned = None, False
        if mark:
            stage, failed = "merge", mark["failed"]
            since = mark["since"]
        else:
            failed = False
            # #171: a parked ticket that landed carries its wait (never accept); a send-back after the landing is returned
            labels = fact.get("labels") or []
            if n in landed and PARKED_LABEL in labels:
                wait = _wait_of(fact)
            returned = _returned(fact, landed_ok.get(n))
            if n in landed and wait is None and not returned:  # #154: landed and still open: accepted, held or not
                stage, since = "accept", landed[n]
            elif n in held:
                stage = "work" if subs else "orch"
                since = (min((s["started"] for s in subs if s.get("started")), default=None) if subs
                         else claimed.get(n))
            else:
                stage, since = "ready", label_at
        rows.append({
            "number": n,
            "title": str(fact.get("title") or ""),
            "stage": stage,
            "failed": failed,
            "workers": sum(1 for s in subs if s.get("type") in WORKER_TYPES),
            "strong": sum(1 for s in subs if s.get("type") == "worker-strong"),
            "researchers": sum(1 for s in subs if s.get("type") in RESEARCHER_TYPES),
            "merge_position": mark["position"] if mark else None,
            "merge_percent": mark["percent"] if mark else None,
            "started_at": started,
            "stage_since": since,
            "units": _row_units(n, n in held, fact, units),
            "wait": wait,
            "returned": returned,
        })
    rows.sort(key=lambda r: (STAGE_RANK[r["stage"]], r["started_at"] is None, r["started_at"] or "", r["number"]))
    return rows
