"""#122: Priority tickets in the merge line. Pure functions (no clock, no gh, no files) plus one small reader.

A merge's queue entry carries ``priority``: ``None`` (ordinary) or
``{"class": "priority"|"flow-fix", "marked": <iso>, "hard": bool}``.

* ``priority_of(issue_marks)``: from the labels of the branch's issues; ``issue_marks`` is one mapping per issue,
  label name -> iso time of that label's newest ``labeled`` event, holding the labels ``priority``, ``flow-fix`` and
  ``hard-push`` (others are ignored). The highest-ranked issue counts: ``priority`` before ``flow-fix``, inside a
  class the older mark first; ``hard`` = that issue carries ``hard-push``.
* ``order_waiters(waiters)``: Priority waiters first by rank, then the rest in the order given (arrival).
* ``batch_for(holder_priority, ordered)``: the waiters of ``ordered`` the holder may claim: a Priority holder
  takes only Priority waiters, an ordinary holder only ordinary ones. ``holder_priority`` is the holder's
  ``priority`` value (``None`` = ordinary).

A waiter is any object with an ``info`` dict (``merge.Waiter``). The reader (``marks_from_timeline``,
``gh_marks``, ``priority_for_issues``) takes an injectable per-issue reader; a failed read is ordinary and logged.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

PRIORITY = "priority"
FLOW_FIX = "flow-fix"
HARD_PUSH = "hard-push"
_CLASS_ORDER = {PRIORITY: 0, FLOW_FIX: 1}
_WATCHED = (PRIORITY, FLOW_FIX, HARD_PUSH)


def _epoch(marked: object) -> float:
    """An iso time as epoch seconds; a missing or unparsable one ranks last inside its class."""
    try:
        return datetime.fromisoformat(str(marked).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def rank_key(priority: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """Sort key of a ``priority`` value (smaller heads the line); ``None`` for an ordinary ticket."""
    if not isinstance(priority, Mapping) or priority.get("class") not in _CLASS_ORDER:
        return None
    return (_CLASS_ORDER[priority["class"]], _epoch(priority.get("marked")))


def priority_of(issue_marks: Iterable[Mapping[str, str]]) -> dict | None:
    best: tuple[tuple[float, float], dict] | None = None
    for labels in issue_marks:
        for cls in (PRIORITY, FLOW_FIX):  # both labels: ranks as priority with that label's time
            if cls in labels:
                value = {"class": cls, "marked": labels[cls], "hard": HARD_PUSH in labels}
                key = rank_key(value)
                if best is None or key < best[0]:
                    best = (key, value)
                break
    return best[1] if best else None


def waiter_rank(waiter: Any) -> tuple[float, float] | None:
    """Rank of a waiter (any object with an ``info`` dict) from its entry's ``priority`` field."""
    return rank_key(waiter.info.get("priority"))


def _blocker_deps(waiters: Sequence[Any]) -> list[set[int]]:
    """For each waiter (by index) the indexes of the waiters in the same sequence that hold an open blocker of one
    of its issues: the entry's ``blocked_by`` (#140) against the other entries' ``issues``."""
    deps: list[set[int]] = [set() for _ in waiters]
    for i, w in enumerate(waiters):
        blockers = {int(b) for b in (w.info.get("blocked_by") or ()) if isinstance(b, int)}
        if not blockers:
            continue
        for j, x in enumerate(waiters):
            if j != i and blockers & {int(n) for n in (x.info.get("issues") or ()) if isinstance(n, int)}:
                deps[i].add(j)
    return deps


def _stuck(deps: list[set[int]]) -> set[int]:
    """Indexes that never get free when blockers go first: a blocker cycle and whatever waits on one."""
    done: set[int] = set()
    while True:
        free = {i for i in range(len(deps)) if i not in done and deps[i] <= done}
        if not free:
            return set(range(len(deps))) - done
        done |= free


def blocker_cycle(waiters: Sequence[Any]) -> list:
    """The waiters of a blocker cycle among ``waiters`` (plus those waiting on one), in the order given; empty when
    the blockers inside the line can all go first (#140)."""
    stuck = _stuck(_blocker_deps(waiters))
    return [w for i, w in enumerate(waiters) if i in stuck]


def order_waiters(waiters: Sequence[Any]) -> list:
    """Priority waiters first by rank (equal ranks keep arrival order), then ordinary ones in arrival order. Then
    (#140) a waiter whose open blocker waits in the same line goes right after that blocker (after its last one);
    a blocker not in the line changes nothing, and the waiters of a blocker cycle keep their place."""
    ranked = [(waiter_rank(w), i, w) for i, w in enumerate(waiters)]
    head = sorted((r for r in ranked if r[0] is not None), key=lambda r: (r[0], r[1]))
    base = [r[2] for r in head] + [r[2] for r in ranked if r[0] is None]
    deps = _blocker_deps(base)
    stuck = _stuck(deps)
    deps = [set() if i in stuck else d for i, d in enumerate(deps)]
    if not any(deps):
        return base
    placed: list[int] = []
    seen: set[int] = set()

    def emit(i: int) -> None:
        placed.append(i)
        seen.add(i)
        for j in range(len(base)):
            if j not in seen and deps[j] and deps[j] <= seen:
                emit(j)

    for i in range(len(base)):
        if i not in seen and deps[i] <= seen:
            emit(i)
    return [base[i] for i in placed] + [base[i] for i in range(len(base)) if i not in seen]


def batch_for(holder_priority: Mapping[str, Any] | None, ordered: Sequence[Any]) -> list:
    """The members of ``ordered`` (already in ``order_waiters`` order, the holder's own entry not in it) that a
    holder with this ``priority`` value claims into its batch: Priority for a Priority holder, ordinary for an
    ordinary one."""
    holder_is_priority = rank_key(holder_priority) is not None
    return [w for w in ordered if (waiter_rank(w) is not None) == holder_is_priority]


# -- the reader ------------------------------------------------------------------------------------

def marks_from_timeline(timeline: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """label -> iso time of its newest ``labeled`` event, for the watched labels the issue still carries."""
    marks: dict[str, str] = {}
    for event in timeline:
        name = (event.get("label") or {}).get("name")
        if name not in _WATCHED:
            continue
        if event.get("event") == "labeled":
            marks[name] = str(event.get("created_at") or "")
        elif event.get("event") == "unlabeled":
            marks.pop(name, None)
    return marks


def join_pages(text: str) -> str:
    """``gh api --paginate`` prints one JSON array per page, concatenated: one array holding every item."""
    decoder = json.JSONDecoder()
    items: list = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        value, i = decoder.raw_decode(text, i)
        items.extend(value if isinstance(value, list) else [value])
    return json.dumps(items)


def _gh_raw(args: list[str]) -> str:
    from .gh_call import run_gh

    result = run_gh(args)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {(result.stderr or '').strip()[:200]}")
    return result.stdout or ""


def gh_marks(issue: int, runner: Callable[[list[str]], str] | None = None, repo: str | None = None) -> dict[str, str]:
    """The watched labels of one issue with their mark times: ``core_table.GhIssueReader.timeline`` (the same
    read as the cycle-time table), fed through ``join_pages``. Raises on any failure. Under pytest the real
    ``gh`` is not run (the guard ``merge.read_issue_state`` has), unless a ``runner`` is given."""
    if runner is None and os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the real gh is not read under pytest")
    from .core_table import GhIssueReader

    if repo is None:
        from .core_settings import tickets_repo  # #323: the issue's repo is the tickets repo
        repo = tickets_repo()
    run = runner or _gh_raw
    reader = GhIssueReader(repo, gh_runner=lambda args: join_pages(run(args)))
    return marks_from_timeline(reader.timeline(int(issue)))


def priority_for_issues(issues: Sequence[int],
                        reader: Callable[[int], Mapping[str, str]] | None = None,
                        log: Callable[[int, Exception], None] | None = None) -> dict | None:
    """``priority_of`` the marks ``reader`` gives for each issue (default ``gh_marks``). An issue whose read
    fails counts as unmarked and is reported once to ``log(issue, exc)``; all reads failing is ``None`` =
    ordinary."""
    read = reader if reader is not None else gh_marks
    marks: list[Mapping[str, str]] = []
    for issue in issues:
        try:
            marks.append(read(int(issue)))
        except Exception as exc:  # noqa: BLE001 - a failed read is an ordinary ticket, never a failed merge
            if log is not None:
                log(int(issue), exc)
    return priority_of(marks)


def reread_priority(previous: Mapping[str, Any] | None, issues: Sequence[int],
                    reader: Callable[[int], Mapping[str, str]] | None = None,
                    log: Callable[[int, Exception], None] | None = None) -> Mapping[str, Any] | None:
    """#140: the ``priority`` a waiting merge's entry holds after its issues' labels are read again. A failed read
    never lowers a mark: when a read failed, the entry keeps the better of ``previous`` and the labels read (by
    ``rank_key``). An entry with no issues keeps ``previous``."""
    if not issues:
        return previous
    failed: list[int] = []

    def note(issue: int, exc: Exception) -> None:
        failed.append(issue)
        if log is not None:
            log(issue, exc)

    new = priority_for_issues(issues, reader, note)
    if not failed:
        return new
    prev_key, new_key = rank_key(previous), rank_key(new)  # a failed read: never below the previous mark
    if prev_key is None:
        return new
    return previous if new_key is None or prev_key <= new_key else new


def gh_blockers(issue: int, runner: Callable[[list[str]], str] | None = None, repo: str | None = None) -> list[int]:
    """#140: the open native blockers of one issue (``gh issue view N --json blockedBy``; the nodes are read as
    ``dispatch.parse_candidates`` reads them: a CLOSED blocker does not count). Raises on any failure. Under
    pytest the real ``gh`` is not run, unless a ``runner`` is given."""
    if runner is None and os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the real gh is not read under pytest")
    from .dispatch import parse_candidates

    if repo is None:
        from .core_settings import tickets_repo  # #323: the issue's repo is the tickets repo
        repo = tickets_repo()
    run = runner or _gh_raw
    data = json.loads(run(["issue", "view", str(int(issue)), "--repo", repo, "--json", "number,blockedBy"]) or "{}")
    return list(parse_candidates([{"number": int(issue), **data}])[0].open_blockers)


# -- the hard cut (rule 5) -------------------------------------------------------------------------------------
# A waiting Priority merge whose issues carry ``hard-push`` writes one request file beside the lock,
# ``<lock>.cut`` = {"ticket", "pid", "issue", "at"}. The holder of an ordinary batch polls it through its suite
# watcher, kills the suite and goes back in line; a Priority batch never looks at it.

HARD_POLL_S = 120  # a waiting Priority merge re-reads its labels this often


def cut_path(lock: Any) -> Path:
    lock = Path(lock)
    return lock.with_name(lock.name + ".cut")


def _queue_dir(lock: Path) -> Path:
    return lock.with_name(lock.name + ".queue")


def request_cut(lock: Any, ticket: str, pid: int, issue: int, *, wall: Callable[[], float] = time.time) -> None:
    """Write the cut request (atomic: a reader sees all of it or none of it)."""
    path = cut_path(lock)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"ticket": ticket, "pid": int(pid), "issue": issue, "at": wall()}), encoding="utf-8")
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def clear_cut(lock: Any, ticket: str | None = None) -> None:
    """Remove the request; with ``ticket`` only a request that names it (a requester never removes another's)."""
    path = cut_path(lock)
    if ticket is not None:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("ticket") != ticket:
                return
        except (OSError, ValueError, AttributeError):
            return
    try:
        path.unlink()
    except OSError:
        pass


def pending_cut(lock: Any, *, wall: Callable[[], float], max_age_s: float,
                pid_alive: Callable[[int], bool]) -> dict | None:
    """The standing request, or ``None``. A request whose requester is dead, whose ticket left the queue, that is
    older than ``max_age_s`` or unreadable is removed and counts as none."""
    path = cut_path(lock)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    req: dict | None = None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("pid"), int) and isinstance(data.get("ticket"), str):
            req = data
    except ValueError:
        pass
    live = (req is not None
            and pid_alive(req["pid"])
            and (_queue_dir(Path(lock)) / req["ticket"]).exists()
            and wall() - float(req.get("at") or 0) <= max_age_s)
    if not live:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return req


class HardWatch:
    """The waiter's side: ``poll(ticket)`` is called from the line-wait loop and answers True once this merge has a
    cut request standing. Nothing for an ordinary merge. A mark already ``hard`` asks at once; otherwise the issues'
    labels are re-read every ``every`` seconds counted from the first poll, and the request is written once.
    A failed read never asks."""

    def __init__(self, lock: Any, priority: Mapping[str, Any] | None, issues: Sequence[int], *,
                 reader: Callable[[int], Mapping[str, str]] | None = None,
                 clock: Callable[[], float] = time.monotonic, every: float = float(HARD_POLL_S),
                 wall: Callable[[], float] = time.time, pid: int | None = None,
                 log: Callable[[int, Exception], None] | None = None) -> None:
        self.lock = lock
        self.priority = priority
        self.issues = list(issues)
        self.reader = reader
        self.clock = clock
        self.every = every
        self.wall = wall
        self.pid = os.getpid() if pid is None else pid
        self.log = log
        self.asked = False
        self._logged = False
        self._ticket: str | None = None
        self._next: float | None = None

    def _ask(self, ticket: str) -> None:
        issue = self.issues[0] if self.issues else 0
        request_cut(self.lock, ticket, self.pid, issue, wall=self.wall)
        self._ticket = ticket
        self.asked = True

    def poll(self, ticket: str) -> bool:
        if self.asked:
            return True
        if rank_key(self.priority) is None:
            return False
        if self.priority.get("hard"):
            self._ask(ticket)
            return True
        now = self.clock()
        if self._next is None:
            self._next = now + self.every
            return False
        if now < self._next:
            return False
        self._next = now + self.every
        hard = False
        for issue in self.issues:
            try:
                hard = hard or HARD_PUSH in (self.reader or gh_marks)(int(issue))
            except Exception as exc:  # noqa: BLE001 - a failed read never asks for a cut
                if self.log is not None and not self._logged:
                    self.log(int(issue), exc)  # once: a gh outage does not fill the log every two minutes
                self._logged = True
        if hard:
            self._ask(ticket)
        return self.asked

    def withdraw(self) -> None:
        """Remove this watch's own request (the merge got the lock, or ended)."""
        if self.asked and self._ticket is not None:
            clear_cut(self.lock, self._ticket)
        self.asked = False
