"""#219 rule 3: `merge withdraw <issue|branch> --why "..."` (unit 2 of 4). Stdlib only.

One live merge entry of a branch (or of an issue) leaves the merge line: a queued one leaves the queue, a running
one has its own process tree ended (`merge._kill_tree`, the holder pid of the lock file) and the lock freed so the
next entry starts. Its ticket gets one comment whose first line is `<!-- merge withdrawn -->`, which re-adds
`ready-for-agent` (`merge_handback._post_one`); the brain chat gets one line. A withdrawn run's result says
`did not land (withdrawn)`: a marker next to its result file, which `detach` reads.

#219 rule 2 (unit 3) reuses the same machinery with another reason: `supersede` ends the older entry of a branch when
a merge is queued for the same branch at a new head; its marker reads `superseded: replaced by <new head>`, no comment
is posted and no brain line is sent (the new head's own run reports). `marker_reason(result_file)` gives `detach` the word.

Interface: `withdraw(target, ...) -> (rc, message)` for callers and tests, `run_command(args)` for the CLI, and
`withdrawn_note(result_file)` for `detach`. A failed comment or send is logged on stderr, never raised.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.agentops import detach
from tools.agentops.core_format import human_duration
from tools.agentops.merge import DEFAULT_LOCK_PATH, MergeLock, _kill_tree, _unlink_retrying
from tools.agentops.merge_handback import _post_one
from tools.agentops.util import holder_is_live, parse_started, pid_alive

MARKER = "<!-- merge withdrawn -->"
MARKER_SUFFIX = ".withdrawn"  # next to an ended run's result file (withdrawn or superseded); `detach` reads it
WITHDRAWN, SUPERSEDED = "withdrawn", "superseded"  # the reason words of the marker and of the result record
READY_LABEL = "ready-for-agent"


@dataclass
class _Entry:
    state: str  # "queued" (a ticket in the queue dir) or "running" (the holder of the lock file)
    branch: str
    issues: list[int]
    result: str  # the result file the run writes ("" when it is not detached)
    pid: int
    token: str = ""  # running: the holder's token, re-checked under the guard before anything is ended
    started: str = ""  # running: the holder's start stamp
    arrived_ns: int = 0  # queued: the arrival time in the ticket's name
    ticket: Path | None = None  # queued: the ticket file
    head: str = ""  # the branch head sha recorded at queue time ("" for a record made before #219 rule 2)


def withdrawn_note(result_file: Path) -> str:
    """The one line a withdraw left next to this result file; "" when there is none (`detach` reads it)."""
    try:
        return Path(str(result_file) + MARKER_SUFFIX).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def marker_reason(result_file: Path) -> str:
    """The reason word the marker next to this result file carries (`withdrawn`, `superseded`); "" when there is none."""
    note = withdrawn_note(result_file)
    if not note:
        return ""
    word = note.split(":", 1)[0].strip()
    return word if word in (WITHDRAWN, SUPERSEDED) else WITHDRAWN


def _ints(values: Any) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _queued_entries(lock: Path) -> list[_Entry]:
    queue = lock.with_name(lock.name + ".queue")
    try:
        names = sorted(queue.iterdir(), key=lambda t: t.name)
    except OSError:
        return []
    out: list[_Entry] = []
    for t in names:
        try:
            ns, pid = int(t.name.split("-")[0]), int(t.name.split("-")[1])
        except (IndexError, ValueError):
            continue
        if not pid_alive(pid):
            continue  # a dead waiter's ticket is nobody's entry
        info = _read_json(t)
        if info is None or info.get("kind") != "merge":
            continue
        out.append(_Entry("queued", str(info.get("branch") or ""), _ints(info.get("issues")),
                          str(info.get("result") or ""), pid, arrived_ns=ns, ticket=t,
                          head=str(info.get("head") or "")))
    return out


def _holder(lock: Path) -> _Entry | None:
    """The lock file's merge holder, live or stale (a stale one names a pid that no longer is the holder)."""
    holder = _read_json(lock)
    if holder is None or holder.get("kind") != "merge":
        return None
    try:
        pid = int(holder["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    sidecar = _read_json(lock.with_name(lock.name + ".holding")) or {}
    info = sidecar.get("info") if isinstance(sidecar.get("info"), dict) else {}
    return _Entry("running", str(holder.get("branch") or info.get("branch") or ""), _ints(info.get("issues")),
                  str(info.get("result") or ""), pid, token=str(holder.get("token") or ""),
                  started=str(holder.get("started") or ""), head=str(info.get("head") or ""))


def _running_entry(lock: Path) -> _Entry | None:
    """The holder when it is genuinely live; a pid reused by an unrelated process is not a holder (`holder_is_live`)."""
    entry = _holder(lock)
    if entry is None or not holder_is_live(entry.pid, entry.started or None):
        return None
    return entry


def _is_batch_member(lock: Path, entry: _Entry, leader: _Entry) -> bool:
    """A queued ticket the running leader claimed for its batch: its `<lock>.batch/<ticket>.claim` names the leader's token."""
    if entry.ticket is None or not leader.token:
        return False
    claim = _read_json(lock.with_name(lock.name + ".batch") / f"{entry.ticket.name}.claim")
    return claim is not None and claim.get("token") == leader.token


def _free_stale_lock(lock: Path, stale: _Entry) -> None:
    """Frees the lock of a holder that is gone, only while the file still names that holder. Nothing is killed."""
    with MergeLock(lock)._guard(time.time() + 30.0):
        holder = _read_json(lock)
        if holder is None or holder.get("pid") != stale.pid or holder.get("token") != stale.token:
            return
        for f in (lock, lock.with_name(lock.name + ".holding")):
            _unlink_retrying(f)


def _default_results_dir() -> Path:
    return Path(os.environ.get(detach.ENV_RESULTS_DIR) or detach.RESULTS_DIR)


def _ended(results_dir: Path, issue: int | None, branch: str | None) -> bool:
    """A merge record of this issue or branch is in the results dir: the merge ended (it is not in line)."""
    try:
        files = list(results_dir.glob("*.json"))
    except OSError:
        return False
    for f in files:
        rec = _read_json(f)
        if rec is None or rec.get("kind") != "merge":
            continue
        if issue is not None and issue in _ints(rec.get("issues")):
            return True
        if branch is not None and branch in [str(a) for a in rec.get("argv") or []]:
            return True
    return False


def _write_marker(entry: _Entry, why: str, reason: str = WITHDRAWN) -> None:
    """Writes the `.withdrawn` marker of this entry's result (`<reason>: <why>`); an OSError is raised to the caller
    (nothing is ended then)."""
    if entry.result:
        marker = Path(entry.result + MARKER_SUFFIX)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{reason}: {why}", encoding="utf-8")


KILL_CONFIRM_SECONDS = 10.0  # how long the holder's pid is polled after its tree was killed (bounded)


class _KillNotConfirmed(RuntimeError):
    """The holder's pid is still alive after its tree was killed: the lock stays, nothing is posted."""


def _pid_gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def _take_out(lock: MergeLock, entry: _Entry) -> str:
    """Ends one entry under the guard: a queued ticket is removed; a running holder's own tree is ended and the lock
    and its sidecar removed, only once the holder's pid is confirmed gone and while the lock file still names that
    holder. Raises `_KillNotConfirmed` when the pid is still alive. Returns what was done."""
    with lock._guard(time.time() + 30.0):
        if entry.state == "queued":
            if entry.ticket is not None:
                _unlink_retrying(entry.ticket)
            return "left the queue"
        holder = _read_json(lock.path)
        if holder is None or holder.get("pid") != entry.pid or holder.get("token") != entry.token:
            return "already gone"
        if entry.pid != os.getpid():  # never this process's own tree
            try:
                _kill_tree(entry.pid)
            except (subprocess.TimeoutExpired, OSError):
                pass  # the pid is polled below: a failed kill that still ended the holder is fine
            if not _pid_gone_within(entry.pid, KILL_CONFIRM_SECONDS):
                raise _KillNotConfirmed(f"the holder pid {entry.pid} of {entry.branch} is still alive after its "
                                        f"tree was killed")
        for f in (lock.path, lock.path.with_name(lock.path.name + ".holding")):
            _unlink_retrying(f)
        return "tree ended, line freed"


def _held_text(entry: _Entry, now: float) -> str:
    """The held-minutes phrase; "" when a running holder's start stamp does not parse (never a made-up "0 s")."""
    if entry.state == "running":
        started = parse_started(entry.started or None)
        if started is None:
            return ""
        return f"It held the merge line for {human_duration(max(0.0, now - started))}."
    waited = max(0.0, now - entry.arrived_ns / 1e9)
    return f"It was in line for {human_duration(waited)} and did not run."


def _comment_body(entry: _Entry, by: str, why: str, now: float) -> str:
    held = _held_text(entry, now)
    tail = f" {held}" if held else ""
    return f"{MARKER}\nMerge of {entry.branch} withdrawn by {by}: {why}.{tail}\n"


def _notify_brain(issue: int, message: str, brain_chat: Callable[[int], tuple[int, str]],
                  send: Callable[[str, str], tuple[bool, str]]) -> None:
    try:
        rc, chat = brain_chat(issue)
        if rc != 0:
            print(f"merge withdraw: no brain chat for #{issue}: {chat}", file=sys.stderr)
            return
        ok, detail = send(chat, message)
        if not ok:
            print(f"merge withdraw: the brain line for #{issue} was not sent: {detail}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- a failed send is logged, never raised
        print(f"merge withdraw: the brain line for #{issue} was not sent: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def _default_brain_chat(issue: int) -> tuple[int, str]:
    from tools.agentops.brain_notice import run_brain_chat_command
    return run_brain_chat_command(issue)


def _default_send(chat: str, message: str) -> tuple[bool, str]:
    from tools.agentops.wake import wake
    return wake(chat, message)


def _default_by() -> str:
    try:
        return getpass.getuser() or "user"
    except Exception:  # noqa: BLE001 -- no user name is no reason to refuse the withdraw
        return "user"


def withdraw(target: str, *, why: str, by: str = "", lock_path: Path = DEFAULT_LOCK_PATH,
             results_dir: Path | None = None, gh: Any = None,
             brain_chat: Callable[[int], tuple[int, str]] | None = None,
             send: Callable[[str, str], tuple[bool, str]] | None = None) -> tuple[int, str]:
    """Withdraws every live merge entry of ``target`` (an issue number, `#N` too, or a branch name). Returns
    ``(rc, message)``: 0 when withdrawn or when the merge already ended or its holder is gone ("nothing to withdraw",
    nothing posted); 1 when a marker cannot be written or the holder's pid is still alive after the kill (lock kept,
    nothing posted); 2 when no entry and no ended merge match the target, or the target is a batch member."""
    lock = Path(lock_path)
    digits = target.strip().lstrip("#")
    issue = int(digits) if digits.isdigit() else None
    branch = None if issue is not None else target.strip()

    def matches(e: _Entry) -> bool:
        return (issue in e.issues) if issue is not None else (e.branch == branch)

    running = _running_entry(lock)
    live = [e for e in _queued_entries(lock) + ([running] if running else []) if matches(e)]
    if not live:
        stale = _holder(lock)
        if stale is not None and matches(stale):  # no live holder here, so this one is stale: gone, nothing killed
            _free_stale_lock(lock, stale)
            return 0, f"nothing to withdraw: the holder pid {stale.pid} is gone (stale lock freed)"
        if _ended(Path(results_dir) if results_dir else _default_results_dir(), issue, branch):
            return 0, "nothing to withdraw"
        kind = "issue" if issue is not None else "branch"
        return 2, f"unknown {kind} {target}: no merge is in line, running or ended for it"

    if running is not None:
        for e in live:
            if _is_batch_member(lock, e, running):
                return 2, (f"{target}: member of a batch: withdraw the leader's batch by its branch "
                           f"(the leader is {running.branch})")

    reason = " ".join(why.split()) or "no reason given"
    who = by or _default_by()
    now = time.time()
    lock_obj = MergeLock(lock)
    for e in live:  # before any process ends: `detach` reads the marker after the run's exit
        try:
            _write_marker(e, reason)
        except OSError as exc:
            return 1, (f"withdraw failed: the marker of {e.branch} could not be written ({exc}); "
                       f"nothing was ended")
    try:
        done = [f"{e.state} {e.branch}: {_take_out(lock_obj, e)}" for e in live]
    except _KillNotConfirmed as exc:
        return 1, f"withdraw failed: {exc}; the lock is kept and nothing was posted"

    for n in sorted({n for e in live for n in e.issues}):
        entry = next(e for e in live if n in e.issues)
        body = _comment_body(entry, who, reason, now)
        try:
            gh_obj = gh if gh is not None else detach._default_gh()
            _post_one(gh_obj, n, body)
        except Exception as exc:  # noqa: BLE001 -- a gh failure is logged, never raised
            print(f"merge withdraw: the comment on #{n} was not posted: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        _notify_brain(n, f"#{n}: merge withdrawn by {who}: {reason}",
                      brain_chat or _default_brain_chat, send or _default_send)
    return 0, f"withdrawn: {'; '.join(done)}"


class SupersedeFailed(RuntimeError):
    """An older entry could not be ended (its marker is unwritable, or its pid outlived the kill): it stays in line."""


def supersede(lock: MergeLock, older: list[tuple[int, int, dict, Path | None]], new_head: str) -> int:
    """#219 rule 2: ends the older entries of a branch that a merge at ``new_head`` replaces. ``older`` holds the
    ``(arrival ns, pid, record, ticket file)`` of each: a queued one has its ticket file, the running one has none
    (it is the lock's own holder). Marker first, then `_take_out` (the same step a withdraw takes). Posts nothing and
    sends nothing; logs one line on stderr per entry. Returns the earliest arrival ns, the place the new head takes.
    Raises ``SupersedeFailed`` before ending an entry whose marker cannot be written or whose holder outlives the kill."""
    running = _running_entry(lock.path)
    entries: list[_Entry] = []
    for ns, pid, info, ticket in older:
        if ticket is not None:
            entries.append(_Entry("queued", str(info.get("branch") or ""), _ints(info.get("issues")),
                                  str(info.get("result") or ""), pid, arrived_ns=ns, ticket=ticket,
                                  head=str(info.get("head") or "")))
        elif running is not None and running.pid == pid:
            entries.append(running)
        else:
            raise SupersedeFailed(f"the holder pid {pid} of {info.get('branch')} is not the live holder of the line")
    for e in entries:  # before any process ends: `detach` reads the marker after the run's exit
        try:
            _write_marker(e, f"replaced by {new_head}", SUPERSEDED)
        except OSError as exc:
            raise SupersedeFailed(f"the marker of {e.branch} could not be written ({exc})") from exc
    for e in entries:
        try:
            done = _take_out(lock, e)
        except _KillNotConfirmed as exc:
            raise SupersedeFailed(str(exc)) from exc
        print(f"merge supersede: {e.state} {e.branch} at {e.head[:12] or '?'}: {done}; replaced by {new_head[:12]}",
              file=sys.stderr)
    return min(ns for ns, _pid, _info, _ticket in older)


def run_command(args) -> int:
    """`merge withdraw <issue|branch> --why "..."` (the CLI's `merge` subparser, dispatched by `run_merge_command`)."""
    target = getattr(args, "target", None)
    why = " ".join(str(getattr(args, "why", "") or "").split())
    if not target:
        print("merge withdraw: give the issue number or the branch name")
        return 2
    if not why:
        print('merge withdraw: --why "<one line>" is required')
        return 2
    rc, message = withdraw(str(target), why=why, by=str(getattr(args, "by", "") or ""),
                           lock_path=Path(getattr(args, "lock", DEFAULT_LOCK_PATH)))
    print(message)
    return rc
