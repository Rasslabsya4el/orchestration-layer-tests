"""The autopilot beside ORCH (#116, D108) -- core since #314 (D109 amd 1), still loaded as a module
the settings name (ADR 0109, extension point 4), joined to the core at the points `extensions.py` offers and nowhere
else. The core's dispatcher knows nothing about any of it.

A goal is an open issue labelled `autopilot`, worked by a background brain session (`claude --bg
--agent brain --name "autopilot #N" --settings tools/agentops/autopilot_settings.json`) restarted
from its own checkpoint comment. Each tick (registered `after` the ORCH step, so it never affects
ORCH and ORCH never affects it, D104 unchanged) the dispatcher adopts any live `autopilot #N`
session it did not start, polls every tracked one, ends and (next tick) restarts one that finished
its turn or is `blocked`, and starts one for every eligible goal with no live session yet.

An `idle` end (it finished its turn, most often waiting on ORCH) does not restart on the next tick:
the goal waits (`_AutopilotWaiting`) until a "Part of #N" ticket closes, reopens or has its labels
change, the goal itself gets a new non-checkpoint comment, or `AUTOPILOT_IDLE_BACKSTOP_MINUTES` pass
-- an assignee claim and a plain comment on a child (ORCH's progress, a claim's first checkpoint)
never count (#146, #270). A `blocked` end (a permission prompt, or Claude Code's
autocompact-thrashing guard) still restarts next tick; a `blocked` row naming nothing it waits for
whose goal checkpoint changed since the start is a finished turn and ends as `idle` (#935); three in a row with no new
checkpoint comment label the goal `autopilot-stuck` and stop retrying it. The owner's `Autopilot
terms` comment (#127, GLOSSARY.md) on the goal -- a `deadline`, `quiet` -- is read every tick
(`_read_terms`): no start at or past the deadline, a live session 15 min past it is stopped, and a
terms block the dispatcher cannot parse labels the goal stuck. The dispatcher never wakes the chat
brain for an autopilot (#1105, D108 amd 1): a `quiet` deadline and an `autopilot-stuck` labelling
only log and label; a `chat:` line, if present, is kept and ignored.

The session's own SessionStart hook (`tools/hooks/autopilot_checkpoint.py`) injects the latest
checkpoint into the next session on the same goal, so the prompt never repeats it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import claude_bg
from .dispatch import (
    ClaudeCli,
    area_labels,
    area_problem,
    Dispatcher,
    GhCli,
    LIVE_STATES,
    READ_MEMO_SECONDS,
    _log,
    end_session,
    running_subagent_ids,
)
from .extensions import Registry
from .util import pid_alive
from .worktree import ROOT

#: a goal is an open issue labelled `autopilot`, and never one already `autopilot-stuck`.
AUTOPILOT_LABEL = "autopilot"
AUTOPILOT_STUCK_LABEL = "autopilot-stuck"
AUTOPILOT_DOC = "docs/agents/autopilot.md"
AUTOPILOT_SETTINGS_PATH = ROOT / "tools" / "agentops" / "autopilot_settings.json"
#: the checkpoint comment prefix the hook and the dispatcher both look for (`docs/agents/autopilot.md`).
AUTOPILOT_CHECKPOINT_PREFIX = "Autopilot checkpoint"
#: 3 `blocked` restarts in a row with no new checkpoint comment -> `autopilot-stuck`, no further start.
MAX_AUTOPILOT_RESTARTS = 3
#: #146: after an `idle` end (the session finished its turn while waiting on ORCH), the next session
#: on the same goal starts only when a ticket it waits on changes, or after this many minutes have
#: passed since the checkpoint (the backstop). `done` and `blocked` endings are unaffected.
AUTOPILOT_IDLE_BACKSTOP_MINUTES = 15
#: #318: the native-sub-issue walk costs k+1 REST calls, so a waiting session reads its children at
#: most this often (the 30 s tick would exceed GitHub's shared 5000/h limit on a ~50-ticket tree).
AUTOPILOT_CHILD_READ_MINUTES = 5
#: `build_autopilot_command`'s `--name` ("autopilot #116"): adoption, same idea as `_ORCH_NAME_RE`.
_AUTOPILOT_NAME_RE = re.compile(r"^autopilot\s+#(\d+)$")
#: #1130: a restart adopts a live `autopilot #N` session in these states: `blocked` (a question or a
#: permission prompt) and `done` (its turn ended), beside LIVE_STATES; the next poll ends or restarts it by
#: the existing rules. `stopped` (a `claude stop`, no live process) and `failed` are never adopted.
#: A `blocked` or `done` session is adopted only while its process is alive (`_has_live_process`):
#: #1130 send-back, 2026-10-16, 11 long-dead `done` sessions were adopted on main 4ed5deef.
AUTOPILOT_ADOPT_STATES = LIVE_STATES | frozenset({"blocked", "done"})
#: Claude Code >= 2.1.281: `claude --bg` in an untrusted directory exits with this verbatim message
#: (#109) -- a start failure containing it is stuck immediately, never retried.
WORKSPACE_NOT_TRUSTED = "Workspace not trusted"

#: Autopilot terms (#127, GLOSSARY.md "Autopilot terms", `docs/research/2026-09-27-autopilot-grill.md`
#: round 4): the latest comment on a goal `#N` starting with this exact prefix carries a fenced
#: `key: value` block the dispatcher reads. Every key is optional (#1105: `chat` is no longer one,
#: and a `chat:` line is ignored); custom terms live in the prose around the block, never parsed here.
AUTOPILOT_TERMS_PREFIX = "Autopilot terms"
#: a live session past its deadline by at least this long is stopped (#127 numbers item 3).
AUTOPILOT_DEADLINE_GRACE_MINUTES = 15


@dataclass(slots=True)
class AutopilotTerms:
    """One `Autopilot terms` comment's fenced block, parsed (#127). `parse_error` names the raw
    `key: value` line the dispatcher could not use (a bad `deadline`) -- the
    caller marks the goal `autopilot-stuck` on that, never guesses."""

    scope: str | None = None
    deadline: datetime | None = None
    quiet: bool = False
    usage: str | None = None
    lifted: str | None = None
    chat: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    parse_error: str | None = None


#: the fenced block inside an `Autopilot terms` comment (the first one found; there is one per comment).
_TERMS_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def parse_autopilot_terms(body: str) -> AutopilotTerms:
    """`Autopilot terms` comment body -> `AutopilotTerms` (GLOSSARY.md "Autopilot terms"). Every key
    is optional; a `deadline` that does not parse as ISO 8601 sets `parse_error` to the offending
    line, quoted, for the goal's stuck comment (#127 numbers item 6). A `chat:` line is kept in
    `chat`/`raw` and never read by the dispatcher (#1105)."""

    raw: dict[str, str] = {}
    match = _TERMS_FENCE_RE.search(body)
    if match:
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            raw[key.strip().lower()] = value.strip()

    deadline: datetime | None = None
    parse_error: str | None = None
    deadline_raw = raw.get("deadline")
    if deadline_raw:
        try:
            parsed = datetime.fromisoformat(deadline_raw)
        except ValueError:
            parse_error = f"deadline: {deadline_raw}"
        else:
            deadline = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    quiet = raw.get("quiet", "").strip().lower() in {"yes", "true", "1"}
    return AutopilotTerms(
        scope=raw.get("scope"), deadline=deadline, quiet=quiet, usage=raw.get("usage"),
        lifted=raw.get("lifted"), chat=raw.get("chat"), raw=raw, parse_error=parse_error,
    )


class GhReadFailed(Exception):
    """#922: a `gh` read the start rule depends on failed (HTTP 504, a rate limit, garbage). The answer
    is unknown: never "no such comment" and never "something changed" -- the pass skips the goal."""


def _comment_id(comment: dict[str, Any]) -> str:
    """A stable key for one comment (#146's "new comment" check): `id` when `gh` gives one, else
    `createdAt` -- either is unique enough to tell "already seen" from "new" across ticks."""

    return str(comment.get("id") or comment.get("createdAt") or comment.get("url") or "")


def _comment_ids(comments: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(_comment_id(c) for c in comments)


def _child_change(old: dict[int, tuple[bool, frozenset[str]]],
                  new: dict[int, tuple[bool, frozenset[str]]]) -> str:
    """#544: the first child (lowest number) whose `(open, labels)` differs, in words for the
    `started` log line: `#N closed`, `#N reopened`, `#N labels`, `#N added`, `#N removed`."""

    for number in sorted(set(old) | set(new)):
        if old.get(number) == new.get(number):
            continue
        if number not in old:
            return f"#{number} added"
        if number not in new:
            return f"#{number} removed"
        was_open, now_open = old[number][0], new[number][0]
        if was_open and not now_open:
            return f"#{number} closed"
        if now_open and not was_open:
            return f"#{number} reopened"
        return f"#{number} labels"
    return "children"


def _child_signature(items: list[dict[str, Any]]) -> dict[int, tuple[bool, frozenset[str]]]:
    """One `(open, labels)` tuple per child issue (#270): what the rule reacts to -- closed/reopened,
    a label added or removed -- and nothing else. Neither an assignee (a claim alone never changes
    the signature) nor a comment count is in these fields: a new comment on a child (ORCH's progress,
    a checkpoint) never wakes a waiting autopilot on its own (#270; the old `comment_count` field
    used to make every claim's first checkpoint restart the session for no actionable reason)."""

    out: dict[int, tuple[bool, frozenset[str]]] = {}
    for item in items:
        number = int(item["number"])
        is_open = str(item.get("state") or "").upper() != "CLOSED"
        labels = frozenset(str(lbl.get("name", "")) for lbl in item.get("labels") or [])
        out[number] = (is_open, labels)
    return out


@dataclass(slots=True)
class _AutopilotCurrent:
    """One live autopilot session for one goal issue (#116). No held-issue bookkeeping (unlike
    ORCH's `_Current`): a goal is one issue, resolved by its own checkpoint comments, not by
    `ready-for-agent` claims."""

    goal: int
    session: str
    started_at: float
    session_uuid: str | None = None
    #: the latest `AUTOPILOT_CHECKPOINT_PREFIX` comment that existed when this session started (or
    #: was adopted); compared at end time to tell progress from a `blocked` restart that made none.
    start_checkpoint: str | None = None
    #: #934: set at the `blocked` verdict from the listing row's `waitingFor`; named in the reset line.
    blocked_waiting_for: str | None = None
    #: #935: the whole listing row at the `blocked` verdict, logged as one JSON line after the reset line.
    blocked_row: dict | None = None


@dataclass(slots=True)
class _AutopilotWaiting:
    """A goal whose session just ended `idle` or `done` (#146, #544): held back from an immediate
    restart until a ticket it waits on changes, or the backstop passes. Snapshotted once, right at end
    time -- never updated while waiting, so every later tick compares against the same moment, not a
    moving one. Never created for a `blocked` ending (that restarts, or stops, as before)."""

    goal: int
    #: `Dispatcher.clock()` (monotonic) at the moment the session ended -- the backstop's zero point.
    since: float
    #: comment ids on the goal issue itself, present at end time (`_comment_ids`). #922: `None` when
    #: that read failed -- the first good read in `_waiting_is_over` becomes the baseline.
    comments_seen: frozenset[str] | None = frozenset()
    #: `_child_signature` of every "Part of #{goal}" issue, at end time (`None` as above, #922).
    children: dict[int, tuple[bool, frozenset[str]]] | None = field(default_factory=dict)
    #: #280: the wall-clock time of the idle end (`Dispatcher.timestamp()`), kept in the waits file so
    #: a restarted dispatcher can re-base `since` (the monotonic clock does not survive a restart).
    since_wall: datetime | None = None
    #: #318: `Dispatcher.clock()` of the last children walk (`None` = none yet); not persisted.
    last_child_read: float | None = None
    #: #544: why `_waiting_is_over` last said yes (`backstop 15 min`, `comment on #N`, `#N closed`...),
    #: for the `started` log line; not persisted.
    wake_reason: str | None = None


# -- the prompt and the command line ------------------------------------------------------------

def build_autopilot_prompt(goal: int) -> str:
    """The first turn of every autopilot session (#116): the checkpoint (or "first session") is
    injected separately by the SessionStart hook, not repeated in the prompt."""

    return f"Autopilot on #{goal}. Follow {AUTOPILOT_DOC}."


def build_autopilot_command(goal: int, exe: Path | str = "claude.exe",
                            settings: Path | str = AUTOPILOT_SETTINGS_PATH) -> list[str]:
    """The exact command line for one autopilot goal, as an argv list (#116). `--agent brain`: the
    autopilot IS the brain, working unattended; no worktree isolation (it never edits code) and the
    SessionStart checkpoint hook come from `settings`, not from flags here."""

    return claude_bg.build_argv(exe, name=f"autopilot #{goal}", agent="brain", settings=settings,
                                prompt=build_autopilot_prompt(goal))


# -- what this module gives the core's classes ----------------------------------------------------
#
# Every method below moved here from the dispatcher unchanged; `register` hands each one to the core
# class it belongs to (`extensions.py`, the `method` point). The classes are namespaces only, never
# instantiated: `self` in them is the `Dispatcher`, the `GhCli` or the `ClaudeCli` the core built.


class _GhAutopilot:
    """`GhCli`'s reads for this module: every one goes through the core's own `_run` seam, so the
    tests drive them with the same fake `gh` the core uses."""

    def autopilot_goals(self) -> list[int] | None:
        """Open issues labelled `autopilot` and not already `autopilot-stuck` (#116) -- a `wayfinder:
        map` or a task, either is a goal. No ordering guarantee needed: every eligible goal not
        already tracked gets its own session the same tick. `None` when `gh` fails (#922): an empty
        list means "no goal", a failed read means "unknown"."""

        result = self._read([
            "issue", "list", "-R", self.repo, "--state", "open", "--label", AUTOPILOT_LABEL,
            "--json", "number,labels", "--limit", "200",
        ], READ_MEMO_SECONDS)  # #1113: one read per 90 s, dropped by a write of an issue
        if result.returncode != 0:
            _log(f"gh issue list --label {AUTOPILOT_LABEL} failed: {(result.stderr or '').strip()[:200]}")
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            _log(f"gh issue list --label {AUTOPILOT_LABEL}: invalid JSON ({exc})")
            return None
        out: list[int] = []
        areas = area_labels()
        logged = self.__dict__.setdefault("_logged_area", set())
        for item in data if isinstance(data, list) else []:
            labels = {lbl.get("name") for lbl in item.get("labels") or []}
            if AUTOPILOT_STUCK_LABEL in labels:
                continue
            why = area_problem(labels, areas)  # #319: a goal holds exactly one area label
            if why is not None:
                key = (int(item["number"]), why)
                if key not in logged:
                    logged.add(key)
                    _log(f"autopilot #{item['number']} not started: {why}")
                continue
            out.append(int(item["number"]))
        return out

    def latest_checkpoint_comment(self, number: int) -> str | None:
        """The latest comment on `number` that starts with `AUTOPILOT_CHECKPOINT_PREFIX`, or `None`
        when there is none yet or `gh` fails -- the same rule `tools/hooks/autopilot_checkpoint.py`
        applies from inside the session. #1113: never memoized -- its four callers (adoption, start,
        the end snapshot, the blocked check) compare against a checkpoint the session has just written."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} --json comments failed: {(result.stderr or '').strip()[:200]}")
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            _log(f"gh issue view {number} --json comments: invalid JSON ({exc})")
            return None
        comments = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            return None
        for comment in reversed(comments):
            body = (comment or {}).get("body") or ""
            if body.startswith(AUTOPILOT_CHECKPOINT_PREFIX):
                return body
        return None

    def latest_terms_comment(self, number: int) -> str | None:
        """The latest comment on `number` starting with `AUTOPILOT_TERMS_PREFIX` (#127); `None`
        when there is none yet. The same rule as `latest_checkpoint_comment`, a separate call: a
        change during the stretch is a new comment, the latest one wins. #922: raises `GhReadFailed`
        when `gh` fails -- "no terms" and "could not read" are different answers. #1113: read through
        the memo (one read per `READ_MEMO_SECONDS` per goal, shared with the waiting check); a terms
        change is seen within that window, and a failed read is never kept."""

        result = self._read(["issue", "view", str(number), "-R", self.repo, "--json", "comments"],
                            READ_MEMO_SECONDS)
        if result.returncode != 0:
            message = f"gh issue view {number} --json comments failed: {(result.stderr or '').strip()[:200]}"
            _log(message)
            raise GhReadFailed(message)
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            message = f"gh issue view {number} --json comments: invalid JSON ({exc})"
            _log(message)
            raise GhReadFailed(message) from exc
        comments = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            raise GhReadFailed(f"gh issue view {number} --json comments: no comments list")
        for comment in reversed(comments):
            body = (comment or {}).get("body") or ""
            if body.startswith(AUTOPILOT_TERMS_PREFIX):
                return body
        return None

    def comments(self, number: int, *, fresh: bool = False) -> list[dict[str, Any]] | None:
        """Every comment on `number`, oldest first, or `None` when `gh` fails (#146: the snapshot at
        the moment an `idle` autopilot session ends, and the check for a new one against it).
        #1113: the check reads through the memo (`READ_MEMO_SECONDS`); `fresh=True` is a read that
        must see the comments as they are now -- the end-of-session snapshot and a baseline taken
        without one -- and it always calls `gh`. A failed read is never kept."""

        args = ["issue", "view", str(number), "-R", self.repo, "--json", "comments"]
        result = self._run(args) if fresh else self._read(args, READ_MEMO_SECONDS)
        if result.returncode != 0:
            _log(f"gh issue view {number} --json comments failed: {(result.stderr or '').strip()[:200]}")
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            _log(f"gh issue view {number} --json comments: invalid JSON ({exc})")
            return None
        comments = data.get("comments") if isinstance(data, dict) else None
        return comments if isinstance(comments, list) else None

    def child_issues(self, goal: int) -> list[dict[str, Any]] | None:
        """Every descendant of `goal` by GitHub native sub-issues, at any depth (sub-issues of
        sub-issues ...), open or closed (#318; it replaced the `Part of #N` body search): a
        breadth-first walk, one paginated `gh api repos/<repo>/issues/<n>/sub_issues` call per
        ticket in the tree (the goal and every descendant: k descendants = k+1 calls), a seen set
        guards against cycles. Each item: `number`, `state` (`OPEN`/`CLOSED`), `labels`
        (`[{name}]`), `updatedAt`, `comments` (the API's count). `None` when any `gh` call fails or
        answers garbage -- never a false empty, so a failed read never looks like "nothing changed"."""

        seen: set[int] = {goal}
        queue: list[int] = [goal]
        out: list[dict[str, Any]] = []
        while queue:
            parent = queue.pop(0)
            result = self._run(["api", f"repos/{self.repo}/issues/{parent}/sub_issues?per_page=100",
                                "--paginate", "--slurp"])
            if result.returncode != 0:
                _log(f"gh api sub_issues of #{parent} failed: {(result.stderr or '').strip()[:200]}")
                return None
            try:
                pages = json.loads(result.stdout or "[]")
            except ValueError as exc:
                _log(f"gh api sub_issues of #{parent}: invalid JSON ({exc})")
                return None
            if not isinstance(pages, list):
                return None
            # `--slurp` wraps every page in one outer array; a flat list is accepted too.
            subs = [s for page in pages for s in page] if pages and isinstance(pages[0], list) else pages
            for sub in subs:
                if not isinstance(sub, dict) or "number" not in sub:
                    return None
                number = int(sub["number"])
                if number in seen:
                    continue
                seen.add(number)
                queue.append(number)
                out.append({
                    "number": number,
                    "state": str(sub.get("state") or "").upper(),
                    "labels": sub.get("labels") or [],
                    "updatedAt": sub.get("updated_at"),
                    "comments": sub.get("comments"),
                })
        return out


class _ClaudeAutopilot:
    """`ClaudeCli`'s one extra launch. The wake of a chat (#123) is the core's own `wake`."""

    def start_autopilot(self, goal: int) -> tuple[str, list[str]]:
        """Launch one autopilot session for `goal`; returns `(session_id, command)`. Raises
        `RuntimeError` on any `claude --bg` failure -- including, on Claude Code >= 2.1.281, the
        verbatim `WORKSPACE_NOT_TRUSTED` message the caller checks for (#109)."""

        cmd = build_autopilot_command(goal, self.exe)
        return claude_bg.start_session(cmd, cwd=self.cwd, timeout=self.timeout, stderr_chars=300), cmd


def _has_live_process(session: dict[str, Any]) -> bool:
    """A `working` (or state-less) listing row is live by its own state. A `blocked` or `done` row
    is live only when its listed pid is a live process (`pid_alive`): a row with no pid, or a pid that
    has exited, is a session that ended and must not be taken over (#1130)."""
    if session.get("state") in LIVE_STATES:
        return True
    pid = session.get("pid")
    return bool(pid) and pid_alive(int(pid))


class _DispatcherAutopilot:
    """The autopilot itself, on the `Dispatcher`: its tick, its terms, its adoption, its endings."""

    # -- the autopilot (#116): one live session per goal, beside the one ORCH ------------------
    #
    # A goal is an open issue labelled `autopilot`. Each tick: adopt any live `autopilot #N` session
    # this dispatcher did not start (a restart, same idea as `_adopt_live`); poll every tracked
    # session with the same idle/subagent test `_poll_current` uses; end and (next tick) restart one
    # that finished its turn or is `blocked`; a `blocked` end with no new checkpoint comment three
    # times in a row labels the goal `autopilot-stuck` and stops retrying it; start one for every
    # eligible goal (open, `autopilot`, not `autopilot-stuck`) with no live session yet.

    def _tick_autopilot(self) -> None:
        listed = self.gh.autopilot_goals()  # one gh call per tick, before adopting (#337)
        goals_known = listed is not None  # #922: a failed list is "unknown", not "no goals"
        pending = list(listed or [])
        if goals_known:
            self._adopt_live_autopilots(set(pending))
        ended_this_tick: set[int] = set()
        goal_numbers = set(self.autopilots.keys()) | set(pending)
        terms_by_goal: dict[int, "AutopilotTerms | None"] = {}
        terms_unknown: set[int] = set()  # #922: terms read failed: no start for that goal this tick
        for goal in goal_numbers:
            try:
                terms_by_goal[goal] = self._read_terms(goal)
            except GhReadFailed:
                terms_unknown.add(goal)
        self._load_waits()  # #280: once, at the first tick of this dispatcher
        dropped = False
        for goal in list(self.autopilot_waiting):  # #146: a closed/relabelled goal stops waiting too
            if goals_known and goal not in goal_numbers:
                del self.autopilot_waiting[goal]
                dropped = True
        if dropped:
            self._save_waits()

        for goal, cur in list(self.autopilots.items()):
            terms = terms_by_goal.get(goal)
            if terms is not None and terms.deadline is not None:
                past_deadline = self._deadline_reached(goal, terms)
                if past_deadline and self._past_deadline_grace(terms.deadline):
                    self._end_autopilot(goal, cur, "deadline")
                    ended_this_tick.add(goal)
                    continue
            action = self._poll_autopilot(cur)
            if action is not None:
                self._end_autopilot(goal, cur, action)
                ended_this_tick.add(goal)
        for goal in pending:
            if goal in self.autopilots or goal in ended_this_tick or goal in terms_unknown:
                continue
            terms = terms_by_goal.get(goal)
            if terms is not None and terms.parse_error is not None:
                self._mark_stuck(
                    goal, f"autopilot #{goal}: could not parse `{AUTOPILOT_TERMS_PREFIX}` "
                          f"({terms.parse_error}); labelled `{AUTOPILOT_STUCK_LABEL}`, no further start.",
                    "bad_terms")
                continue
            if terms is not None and terms.deadline is not None and self._deadline_reached(goal, terms):
                continue
            waiting = self.autopilot_waiting.get(goal)
            # #544: a restart with no wait (after a `blocked` or other non-waiting end) says so.
            ended = self.autopilot_ended_why.pop(goal, None)
            reason = f"restart after {ended}" if ended is not None else "first session"
            if waiting is not None:
                if not self._waiting_is_over(waiting):
                    continue
                reason = waiting.wake_reason or "change"
                del self.autopilot_waiting[goal]
                self._save_waits()
            self._start_autopilot(goal, reason)

    # -- autopilot terms (#127): the deadline and quiet mode; no wake to the chat brain (#1105) ------

    def _read_terms(self, goal: int) -> "AutopilotTerms | None":
        """The latest `Autopilot terms` comment on `goal`, parsed; `None` when there is none --
        every check below is then skipped, exactly today's behaviour (#127 numbers item 7). Raises
        `GhReadFailed` when the read failed (#922); the tick skips that goal."""

        body = self.gh.latest_terms_comment(goal)
        if body is None:
            return None
        return parse_autopilot_terms(body)

    def _past_deadline_grace(self, deadline: datetime) -> bool:
        return self.timestamp() >= deadline + timedelta(minutes=AUTOPILOT_DEADLINE_GRACE_MINUTES)

    def _deadline_reached(self, goal: int, terms: "AutopilotTerms") -> bool:
        """`True` once `terms.deadline` has passed; logs "past deadline" once per (goal, deadline).
        No wake to the chat brain (#1105): a `quiet` deadline only logs and stops the start."""

        assert terms.deadline is not None
        if self.timestamp() < terms.deadline:
            return False
        key = (goal, terms.deadline.isoformat())
        if key not in self._logged_past_deadline:
            self._logged_past_deadline.add(key)
            self.log(f"autopilot #{goal} past deadline")
        return True

    def _mark_stuck(self, goal: int, message: str, why: str) -> None:
        """Every place the dispatcher labels a goal `autopilot-stuck` on its own (a bad terms
        block, #116's blocked-restart streak, #109's workspace-not-trusted): the label, the
        comment, the feed event and the log line. No wake to the chat brain (#1105, D108 amd 1)."""

        self.gh.add_label(goal, AUTOPILOT_STUCK_LABEL)
        self.gh.comment(goal, message)
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "autopilot_stuck",
            "goal": goal, "why": why,
        })
        self.log(message)

    def _adopt_live_autopilots(self, eligible_goals: set[int]) -> None:
        # #337: only a session whose goal is open and labelled `autopilot` is adopted; a closed or
        # unlabelled goal's session is none of this dispatcher's business (it was once reset as idle).
        for session in self.claude.list_sessions():
            session_id = session.get("id")
            if not session_id or str(session_id) in self.ended_sessions:
                continue
            if (session.get("kind", "background") != "background"
                    or session.get("state") not in AUTOPILOT_ADOPT_STATES
                    or not _has_live_process(session)):
                continue
            match = _AUTOPILOT_NAME_RE.match(str(session.get("name") or ""))
            if not match:
                continue
            goal = int(match.group(1))
            if goal in self.autopilots or goal not in eligible_goals:
                continue
            if self.autopilot_waiting.pop(goal, None) is not None:  # a live session again (#146)
                self._save_waits()
            started_ms = session.get("startedAt") or 0
            age = max(0.0, self.timestamp().timestamp() - float(started_ms) / 1000.0) if started_ms else 0.0
            self.autopilots[goal] = _AutopilotCurrent(
                goal=goal, session=str(session_id), started_at=self.clock() - age,
                session_uuid=str(session["sessionId"]) if session.get("sessionId") else None,
                start_checkpoint=self.gh.latest_checkpoint_comment(goal),
            )
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "autopilot_adopt",
                "goal": goal, "session": str(session_id),
            })
            self.log(f"adopted live autopilot #{goal} ({session_id})")

    def _poll_autopilot(self, cur: _AutopilotCurrent) -> str | None:
        """`None` while still live; else the reason it ended: `blocked` (a permission prompt, or
        Claude Code's autocompact-thrashing guard, #109), `idle` (finished its turn), `stopped` or
        `gone`. Subagents keep a `done`/idle session alive, exactly as `_poll_current` for ORCH."""

        sessions = self.claude.list_sessions()
        session = next((s for s in sessions if s.get("id") == cur.session), None)
        if session is None:
            return "gone"
        if session.get("sessionId"):
            cur.session_uuid = str(session["sessionId"])
        state = session.get("state")
        status = session.get("status")
        if state == "stopped":
            return "stopped"
        if state == "failed":
            # #1021: a failed row's helpers never keep it live: it ends like `stopped` and restarts next
            # tick (14.10: autopilot #982 sat `failed` 09:38-10:05 behind a helper that never reported).
            return "failed"
        if state == "blocked":
            cur.blocked_waiting_for = str(session.get("waitingFor") or "").strip() or "no reason given"
            cur.blocked_row = dict(session)
            if (not str(session.get("waitingFor") or "").strip()
                    and self.gh.latest_checkpoint_comment(cur.goal) != cur.start_checkpoint):
                # #935: a `blocked` row that names nothing it waits for, whose goal checkpoint changed
                # since the session started, is a finished turn (seen 3 times on 12-13.10): handled
                # as `idle`, so the next session waits for a change or the backstop; the row is logged.
                self.log(f"autopilot #{cur.goal} blocked row with no waitingFor and a new checkpoint "
                         f"(session {cur.session}) handled as idle: "
                         f"{json.dumps(cur.blocked_row, ensure_ascii=False, default=str, sort_keys=True)}")
                return "idle"
            return "blocked"
        if state not in LIVE_STATES or status == "idle":
            if running_subagent_ids(self.projects_dir, cur.session_uuid, now=self.timestamp()):
                return None
            if state not in LIVE_STATES:
                return str(state)
            return "idle"
        return None

    # -- the waits file (#280): a wait outlives a dispatcher restart --------------------------------

    def _load_waits(self) -> None:
        """Once per dispatcher: rebuild the waits from the file, re-basing each monotonic `since`
        from its wall-clock idle end. Missing or unreadable: no waits, one log line."""

        if getattr(self, "_waits_loaded", False):
            return
        self._waits_loaded = True
        path = self.autopilot_waits_file
        if path is None:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            now_wall = self.timestamp()
            now = self.clock()
            loaded: dict[int, _AutopilotWaiting] = {}
            for entry in raw["waits"]:
                idle_end = datetime.fromisoformat(entry["idle_end"])
                if idle_end.tzinfo is None:
                    idle_end = idle_end.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (now_wall - idle_end).total_seconds())
                goal = int(entry["goal"])
                loaded[goal] = _AutopilotWaiting(
                    goal=goal, since=now - elapsed, since_wall=idle_end,
                    comments_seen=(None if entry["comments_seen"] is None
                                   else frozenset(str(c) for c in entry["comments_seen"])),
                    children=(None if entry["children"] is None
                              else {int(n): (bool(v[0]), frozenset(str(x) for x in v[1]))
                                    for n, v in entry["children"].items()}),
                )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.log(f"autopilot waits: {path} missing/unreadable ({exc!r}); starting with none")
            return
        self.autopilot_waiting.update(loaded)
        if loaded:
            self.log(f"autopilot waits: {len(loaded)} restored from {path.name}")

    def _save_waits(self) -> None:
        path = self.autopilot_waits_file
        if path is None:
            return
        waits = []
        for w in self.autopilot_waiting.values():
            idle_end = w.since_wall or self.timestamp()
            waits.append({
                "goal": w.goal, "idle_end": idle_end.isoformat(timespec="seconds"),
                "comments_seen": None if w.comments_seen is None else sorted(w.comments_seen),
                "children": (None if w.children is None else
                             {str(n): [is_open, sorted(labels)] for n, (is_open, labels) in w.children.items()}),
            })
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"waits": waits}, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.log(f"could not write {path.name} ({exc!r})")

    def _waiting_is_over(self, waiting: "_AutopilotWaiting") -> bool:
        """#270: `True` once one of these holds -- the 15 min backstop; a new comment on the goal
        that is not an `Autopilot checkpoint`; or a child issue's `(open, labels)` (closed, reopened
        or relabelled) changed since the snapshot taken when the session ended. A new comment on a
        child alone (ORCH's progress, a claim's first checkpoint) is not in the signature and never
        wakes it. A `gh` failure on either read is silent (already logged by `GhCli`) and never
        itself counts as a change; a snapshot that failed at end time is taken at the first good read (#922). The reason it is over lands in `waiting.wake_reason` (#544)."""

        if self.clock() - waiting.since >= AUTOPILOT_IDLE_BACKSTOP_MINUTES * 60.0:
            waiting.wake_reason = f"backstop {AUTOPILOT_IDLE_BACKSTOP_MINUTES:g} min"
            return True
        # #1113: the check is memoized (one read per window); a missing baseline (#922) is read fresh,
        # so a memoized list from before the session ended can never become the snapshot.
        if waiting.comments_seen is None:
            comments = self.gh.comments(waiting.goal, fresh=True)
        else:
            comments = self.gh.comments(waiting.goal)
        if comments is not None and waiting.comments_seen is None:
            waiting.comments_seen = _comment_ids(comments)  # #922: the snapshot read failed; this is the baseline
        elif comments is not None:
            for comment in comments:
                if _comment_id(comment) in waiting.comments_seen:
                    continue
                if not str(comment.get("body") or "").startswith(AUTOPILOT_CHECKPOINT_PREFIX):
                    waiting.wake_reason = f"change: comment on #{waiting.goal}"
                    return True
        now = self.clock()
        if (waiting.last_child_read is not None
                and now - waiting.last_child_read < AUTOPILOT_CHILD_READ_MINUTES * 60.0):
            return False
        waiting.last_child_read = now
        children = self.gh.child_issues(waiting.goal)
        if children is not None:
            signature = _child_signature(children)
            if waiting.children is None:
                waiting.children = signature  # #922: the snapshot read failed; this is the baseline
            elif signature != waiting.children:
                waiting.wake_reason = f"change: {_child_change(waiting.children, signature)}"
                return True
        return False

    def _consecutive_blocked_no_checkpoint(self, goal: int) -> int:
        count = 0
        for event in reversed(self._read_events()):
            if event.get("kind") != "autopilot_end" or event.get("goal") != goal:
                continue
            if event.get("why") == "blocked" and not event.get("checkpoint_new", True):
                count += 1
            else:
                break
        return count

    def _end_autopilot(self, goal: int, cur: _AutopilotCurrent, why: str) -> None:
        self.ended_sessions.add(cur.session)
        end_session(self.claude, cur.session, log=self.log)
        del self.autopilots[goal]
        checkpoint = self.gh.latest_checkpoint_comment(goal)
        checkpoint_new = checkpoint != cur.start_checkpoint
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "autopilot_end",
            "goal": goal, "session": cur.session, "why": why,
            "checkpoint": checkpoint, "checkpoint_new": checkpoint_new,
        })
        if why == "deadline":
            self.log(f"autopilot #{goal} stopped (session {cur.session}, "
                     f"{AUTOPILOT_DEADLINE_GRACE_MINUTES} min past its deadline); not restarted")
            return
        if why in ("idle", "done"):
            # #146, #544: an idle or done end (it finished its turn, most often waiting on ORCH) does
            # not restart on its own -- only a change on what it waits on, or the backstop, does.
            # #922: a failed read is `None` (unknown), never an empty snapshot that turns every
            # existing comment or child into "a change" at the next good read.
            seen = self.gh.comments(goal, fresh=True)  # #1113: the snapshot is read fresh, never memoized
            kids = self.gh.child_issues(goal)
            self.autopilot_waiting[goal] = _AutopilotWaiting(
                goal=goal, since=self.clock(), since_wall=self.timestamp(),
                comments_seen=None if seen is None else _comment_ids(seen),
                children=None if kids is None else _child_signature(kids),
            )
            self._save_waits()
            self.log(f"autopilot #{goal} reset ({why}, session {cur.session}); waiting for a change "
                     f"(backstop {AUTOPILOT_IDLE_BACKSTOP_MINUTES:g} min)")
            return
        self.autopilot_ended_why[goal] = why
        if why != "blocked":
            self.log(f"autopilot #{goal} reset ({why}, session {cur.session}); restarting next tick")
            return
        streak = self._consecutive_blocked_no_checkpoint(goal)
        if streak >= MAX_AUTOPILOT_RESTARTS:
            self._mark_stuck(
                goal, f"autopilot #{goal}: blocked {streak} times in a row with no new "
                      f"`{AUTOPILOT_CHECKPOINT_PREFIX}` comment; labelled "
                      f"`{AUTOPILOT_STUCK_LABEL}`, no further start.",
                "blocked_streak")
            return
        waiting_for = cur.blocked_waiting_for or "no reason given"
        self.log(f"autopilot #{goal} reset (blocked, session {cur.session}, "
                 f"{streak}/{MAX_AUTOPILOT_RESTARTS} with no new checkpoint, "
                 f"waiting for: {waiting_for}); restarting next tick")
        if cur.blocked_row is not None:  # #935: keep the whole row, so the next case shows what it was
            self.log(f"autopilot #{goal} blocked row: "
                     f"{json.dumps(cur.blocked_row, ensure_ascii=False, default=str, sort_keys=True)}")

    def _start_autopilot(self, goal: int, reason: str = "first session") -> None:
        if self.dry_run:
            self.log(f"dry-run: would start autopilot #{goal}")
            return
        try:
            session, cmd = self.claude.start_autopilot(goal)
        except RuntimeError as exc:
            message = str(exc)
            if WORKSPACE_NOT_TRUSTED not in message:
                self._note_refused_start(f"autopilot #{goal}", exc, (goal,))  # #298, #1109: the goal chat
                return
            self.log(f"autopilot #{goal}: claude --bg failed ({message})")
            if WORKSPACE_NOT_TRUSTED in message:
                self._mark_stuck(
                    goal, f"autopilot #{goal}: `claude --bg` failed ({message}); "
                          f"labelled `{AUTOPILOT_STUCK_LABEL}`, no further start.",
                    "workspace_not_trusted")
            return
        self.autopilots[goal] = _AutopilotCurrent(
            goal=goal, session=session, started_at=self.clock(),
            start_checkpoint=self.gh.latest_checkpoint_comment(goal),
        )
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "autopilot_start",
            "goal": goal, "session": session,
        })
        self.log(f"autopilot #{goal} started ({session}): {' '.join(cmd)} ({reason})")
        self._note_start_went_through(f"autopilot #{goal}")


# -- what the core loads this module for ------------------------------------------------------------

#: every method `register` gives `Dispatcher`, in the order they read best (the tick first).
DISPATCHER_METHODS = (
    "_tick_autopilot",
    "_read_terms",
    "_past_deadline_grace",
    "_deadline_reached",
    "_mark_stuck",
    "_adopt_live_autopilots",
    "_poll_autopilot",
    "_load_waits",
    "_save_waits",
    "_waiting_is_over",
    "_consecutive_blocked_no_checkpoint",
    "_end_autopilot",
    "_start_autopilot",
)

GH_METHODS = (
    "autopilot_goals",
    "latest_checkpoint_comment",
    "latest_terms_comment",
    "comments",
    "child_issues",
)

CLAUDE_METHODS = ("start_autopilot",)


def _setup(dispatcher: Dispatcher) -> None:
    """The state one dispatcher keeps for this module (`extensions.py`, the `setup` point)."""

    #: the autopilot beside the one ORCH (#116): one live session per goal, no cap on goals.
    dispatcher.autopilots = {}
    #: #146: goals whose session ended `idle` and are held back from an immediate restart.
    dispatcher.autopilot_waiting = {}
    #: #544: why a goal's last session ended when it restarts with no wait (`blocked`...), for the
    #: `started` line's `(restart after <why>)`; popped at the next start.
    dispatcher.autopilot_ended_why = {}
    #: (goal, deadline.isoformat()) already logged "past deadline" (#127) -- keyed on the deadline
    #: itself so a changed `Autopilot terms` comment (a new deadline) logs again.
    dispatcher._logged_past_deadline = set()


def _tick(dispatcher: Dispatcher, report: Any) -> None:
    """One tick of the autopilot. Registered without a guard: a failure here propagates exactly as a
    core step's would, to the dispatcher's own per-tick `except` (nothing is swallowed)."""

    dispatcher._tick_autopilot()


def register(core: Registry) -> None:
    for name in DISPATCHER_METHODS:
        core.method(Dispatcher, name, getattr(_DispatcherAutopilot, name))
    for name in GH_METHODS:
        core.method(GhCli, name, getattr(_GhAutopilot, name))
    for name in CLAUDE_METHODS:
        core.method(ClaudeCli, name, getattr(_ClaudeAutopilot, name))
    core.setup(_setup)
    core.tick("autopilot", _tick, phase="after")


__all__ = [
    "AUTOPILOT_CHECKPOINT_PREFIX",
    "AUTOPILOT_DEADLINE_GRACE_MINUTES",
    "AUTOPILOT_DOC",
    "AUTOPILOT_CHILD_READ_MINUTES",
    "AUTOPILOT_IDLE_BACKSTOP_MINUTES",
    "AUTOPILOT_LABEL",
    "AUTOPILOT_SETTINGS_PATH",
    "AUTOPILOT_STUCK_LABEL",
    "AUTOPILOT_TERMS_PREFIX",
    "AutopilotTerms",
    "MAX_AUTOPILOT_RESTARTS",
    "WORKSPACE_NOT_TRUSTED",
    "build_autopilot_command",
    "build_autopilot_prompt",
    "parse_autopilot_terms",
    "register",
]
