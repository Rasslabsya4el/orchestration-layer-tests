"""One command, the only way a session stops a process that is not its own child (#250).

    python -m tools.agentops stop <pid> [<pid> ...] [--dry-run]

The night of 2026-09-28 the chat brain stopped two live processes by judging them from their CPU
total and a `while True: pass` in the command line alone -- it read neither the parent nor the
birth time of what it stopped (issue #250). This command reuses the one tie the orphan sweep
already has (`tools/agentops/orphans.py`: `real_process_table`, `_row_maps`, `_chain_pids`,
`taskkill_killer`) -- no second way to tie a process to a session is written here.

For each pid this prints exactly ONE line naming the process (its exe and a short command line),
its birth time and age (`tools.agentops.core_format.human_duration`, never a raw second count), its
parent pid, the live Claude session its chain reaches (a `claude.exe` pid) or `none`, and the
verdict at the end of that same line (#258 -- was two lines: a description line, then a separate
verdict line). Then:

- the chain reaches a live Claude session: `refused`, nothing is stopped;
- it reaches none: the process is stopped through the injectable killer (`taskkill_killer` by
  default);
- `--dry-run` prints the same lines and stops nothing.

There is no `--force`: a process tied to a live session is ended by that session itself (a message
to it, or stopping the session), never by this command.

#258: when the MSYS table (`ps -eW`, `tools/agentops/orphans.py`'s `real_msys_table`) was found but
could not be read -- a failed run, a timeout, or output that did not parse
(`orphans.MsysTableUnreadable`) -- EVERY pid of the call is refused outright, without computing a
chain at all: a chain this command could not read is not "no session ties this pid", and treating
the two alike was the night's second mistake (#250). `ps` simply not being installed
(`_find_ps_exe` returning `None`) is unchanged: the Windows `ppid` chain alone decides, as before
this issue.

"A live Claude session" is a live `claude.exe` process. `dispatch.py` itself never derives this
list from the process table alone -- it tracks each session's own `claude_pid` (`claude agents
--json`) as sessions start and end (`_Current.claude_pid`, `Dispatcher._sweep_orphans`) -- so a
free-standing `stop` command, which knows nothing of the dispatcher's own session bookkeeping,
derives it the only way it can from a bare process table: `live_claude_pids` below, every row whose
own leading executable (`orphans._exe_name`/`orphans._leading_exe_token`) is `claude`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from .core_format import human_duration
from .orphans import (MsysTableUnreadable, ProcRow, _chain_pids, _exe_name, _leading_exe_token,
                      _row_maps, real_process_table, taskkill_killer)

#: actions that leave 0 processes stopped-and-refused -- an exit code of 0 counts only these.
_CLEAN_ACTIONS = frozenset({"stopped", "dry-run"})


@dataclass(frozen=True, slots=True)
class StopOutcome:
    pid: int
    row: ProcRow | None
    reaches_claude: int | None
    action: str  # "refused" | "stopped" | "dry-run" | "not-found" | "kill-failed"


def live_claude_pids(processes: Sequence[ProcRow]) -> list[int]:
    """Every row of `processes` whose own leading executable is `claude` (`claude.exe`, matched by
    token the same way `is_orphan_command` matches its own list -- never a substring). No standing
    function of this shape exists in `dispatch.py`: it tracks live sessions' `claude_pid`s itself as
    they start and end, which this free-standing command has no access to."""

    return [p.pid for p in processes if _exe_name(_leading_exe_token(p.command_line)) == "claude"]


def _describe(row: ProcRow, *, reaches: int | None, now: float, verdict: str) -> str:
    """One line: process, birth, age, parent, the session its chain reaches (or `none`), and the
    verdict at the end of the same line (#258 -- was a separate line, see the module docstring)."""

    exe = _exe_name(_leading_exe_token(row.command_line)) or "?"
    short_cmd = row.command_line.strip()[:160]
    birth = datetime.fromtimestamp(row.create_time).strftime("%Y-%m-%d %H:%M:%S")
    age = human_duration(max(0.0, now - row.create_time))
    reaches_str = str(reaches) if reaches is not None else "none"
    return (f"pid {row.pid}: {exe} ({short_cmd}) -- born {birth}, age {age}, "
           f"parent {row.ppid}, reaches {reaches_str} -- {verdict}")


def _chain_reach_claude(row: ProcRow, live_claude: set[int], by_pid: dict[int, ProcRow],
                        by_msys_pid: dict[int, ProcRow]) -> int | None:
    """The live `claude.exe` pid `row`'s chain reaches, or `None` -- `row.pid` itself counts too
    (a live `claude.exe` named directly on the command line)."""

    if row.pid in live_claude:
        return row.pid
    for ancestor in _chain_pids(row, by_pid, by_msys_pid):
        if ancestor in live_claude:
            return ancestor
    return None


def _wrapper_command_child(row: ProcRow, processes: Sequence[ProcRow]) -> ProcRow | None:
    """#662: when `row` is a `tools.agentops.detach run ... -- <argv>` wrapper, its direct child whose
    command line is that `<argv>`; `None` for any other row or a wrapper whose child is gone."""

    cmd = row.command_line
    if "tools.agentops.detach" not in cmd or " run " not in cmd:
        return None
    _, sep, tail = cmd.partition(" -- ")
    if not sep:
        return None
    want = " ".join(tail.split())
    if not want:
        return None
    for p in processes:
        if p.ppid == row.pid and p.pid != row.pid and " ".join(p.command_line.split()) == want:
            return p
    return None


_DISPATCH_MARK = "-m tools.agentops dispatch"


def _is_dispatch_cmd(row: ProcRow) -> bool:
    return _DISPATCH_MARK in " ".join(row.command_line.split())


def _dispatch_child(row: ProcRow, processes: Sequence[ProcRow]) -> ProcRow | None:
    """#905: the dispatcher's wrapper (`cmd /c ... -m tools.agentops dispatch`) and its python are two
    processes with the same marker; the wrapper's direct child carrying it."""

    if not _is_dispatch_cmd(row):
        return None
    for p in processes:
        if p.ppid == row.pid and p.pid != row.pid and _is_dispatch_cmd(p):
            return p
    return None


def _dispatch_parent(row: ProcRow, by_pid: dict[int, ProcRow]) -> ProcRow | None:
    """#905: the dispatcher wrapper above `row` (its python), or `None`."""

    if not _is_dispatch_cmd(row):
        return None
    parent = by_pid.get(row.ppid)
    if parent is not None and parent.pid != row.pid and _is_dispatch_cmd(parent):
        return parent
    return None


def stop_processes(pids: Sequence[int], *, table: Callable[[], Sequence[ProcRow]] | None = None,
                   msys_ok: Callable[[], bool] | bool | None = None,
                   killer: Callable[[int], bool] | None = None, dry_run: bool = False,
                   now: float | None = None, log: Callable[[str], None] = print) -> list[StopOutcome]:
    """One `StopOutcome` per pid of `pids`, in order; one `log` line per pid (#258 -- was two).
    `table` defaults to `real_process_table`, `killer` to `taskkill_killer` -- both overridable so a
    test never touches a real process.

    `msys_ok` says whether the MSYS table (`ps -eW`) was read: `False` (or a callable returning
    `False`) means it was found but could not be read (`orphans.MsysTableUnreadable`), and every
    pid is refused outright -- no chain is computed at all, the killer is never called (#258). Left
    `None`: a real run (`table` also left `None`) reads the table once with `strict_msys=True` and refuses on
    `MsysTableUnreadable`; a test that
    supplies its own `table` is assumed unaffected by this failure (`True`) unless it says
    otherwise -- `ps` simply not being installed is a separate, unchanged case (`real_msys_table`
    itself still returns `{}` for that one, never raises)."""

    if table is None and msys_ok is None:
        # #258: one `ps -eW` read decides both the chains and the refusal -- a second read could
        # succeed where the one behind the chains failed, and a chain built on an empty MSYS table
        # is exactly what must not be trusted.
        try:
            processes = real_process_table(strict_msys=True)
            ok = True
        except MsysTableUnreadable as exc:
            processes = list(exc.rows)
            ok = False
    else:
        processes = (table or real_process_table)()
        if msys_ok is None:
            ok = True
        else:
            ok = msys_ok() if callable(msys_ok) else bool(msys_ok)

    by_pid, by_msys_pid = _row_maps(processes)
    live_claude = set(live_claude_pids(processes))
    now = time.time() if now is None else now
    kill = killer or taskkill_killer

    outcomes: list[StopOutcome] = []
    for pid in pids:
        row = by_pid.get(pid)
        if row is None:
            log(f"pid {pid}: not found")
            outcomes.append(StopOutcome(pid=pid, row=None, reaches_claude=None, action="not-found"))
            continue

        if not ok:
            log(_describe(row, reaches=None, now=now,
                          verdict="refused -- the MSYS table (ps -eW) could not be read, "
                                  "refusing rather than trusting the Windows chain alone"))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=None, action="refused"))
            continue

        reaches = _chain_reach_claude(row, live_claude, by_pid, by_msys_pid)

        if reaches is not None:
            log(_describe(row, reaches=reaches, now=now,
                          verdict=f"refused -- chain reaches live claude.exe pid {reaches}"))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=reaches, action="refused"))
            continue

        # #662: a detach wrapper's one command child goes down with it (child first).
        child = _wrapper_command_child(row, processes) or _dispatch_child(row, processes)
        parent = _dispatch_parent(row, by_pid)
        child_reaches = (_chain_reach_claude(child, live_claude, by_pid, by_msys_pid)
                         if child is not None else None)
        if child_reaches is not None:
            log(_describe(row, reaches=child_reaches, now=now,
                          verdict=f"refused -- its command pid {child.pid}: chain reaches live "
                                  f"claude.exe pid {child_reaches}"))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=child_reaches,
                                        action="refused"))
            continue

        if dry_run:
            extra = f" (its command pid {child.pid} would stop)" if child is not None else ""
            log(_describe(row, reaches=reaches, now=now, verdict="dry-run -- would stop" + extra))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=None, action="dry-run"))
            continue

        if child is not None and not kill(child.pid):
            log(_describe(row, reaches=reaches, now=now,
                          verdict=f"stop failed (its command pid {child.pid} stop failed, "
                                  f"wrapper left alone)"))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=None, action="kill-failed"))
            continue

        if kill(pid):
            extra = f" (its command pid {child.pid} stopped)" if child is not None else ""
            if parent is not None:
                kill(parent.pid)  # #905: the dispatcher's wrapper goes with its python
                extra += f" (its wrapper pid {parent.pid} stopped)"
            log(_describe(row, reaches=reaches, now=now, verdict="stopped" + extra))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=None, action="stopped"))
        else:
            log(_describe(row, reaches=reaches, now=now, verdict="stop failed"))
            outcomes.append(StopOutcome(pid=pid, row=row, reaches_claude=None, action="kill-failed"))

    return outcomes


# -- #871: a returned worker's leftover processes ------------------------------------------------

#: a command line that is a detached / queued / merge / benchmark job is never a worker's leftover.
_PROTECTED_RE = re.compile(r"tools[.]agentops[.]detach|runs[.]py|\bruns (submit|run)\b|\bmerge\b|map[_-]?run|full[_-]?run|"
                           r"benchmark\.py", re.IGNORECASE)


def _names_worktree(command_line: str, name: str) -> bool:
    """True when `command_line` holds `.claude/worktrees/<name>` as a whole path segment (so
    `wt-871-u1` never matches `wt-871-u10`)."""

    cmd = command_line.replace("\\", "/").lower()
    return re.search(r"\.claude/worktrees/" + re.escape(name.lower()) + r"(?![\w.-])", cmd) is not None


def _worktree_selection(name: str, processes: Sequence[ProcRow]) -> list[ProcRow]:
    by_pid, by_msys_pid = _row_maps(processes)
    live_claude = set(live_claude_pids(processes))
    me = os.getpid()
    protected = {p.pid for p in processes
                 if _PROTECTED_RE.search(re.sub(r"[^ ]*\.claude[\\/]worktrees[\\/][^ \"']*", "", p.command_line))}
    chosen: list[ProcRow] = []
    for p in processes:
        if p.pid in live_claude or p.pid == me or not _names_worktree(p.command_line, name):
            continue
        if p.pid in protected or any(a in protected for a in _chain_pids(p, by_pid, by_msys_pid)):
            continue
        chosen.append(p)
    ids = {p.pid for p in chosen}

    def depth(p: ProcRow) -> int:
        return sum(1 for a in _chain_pids(p, by_pid, by_msys_pid) if a in ids)

    return sorted(chosen, key=depth, reverse=True)  # deepest first: children before parents


def stop_worker_worktree(name: str, *, table: Callable[[], Sequence[ProcRow]] | None = None,
                         killer: Callable[[int], bool] | None = None, dry_run: bool = False,
                         now: float | None = None, log: Callable[[str], None] = print) -> list[StopOutcome]:
    """#871: stop the processes whose command line names `.claude/worktrees/<name>`, selected by
    that path alone -- they may chain through the live ORCH's claude.exe (a worker's Bash/Monitor
    children do), which is the only refusal this lifts. Detach wrappers, `runs` jobs, merge/map-run (and an old full-run)
    and benchmark processes, their descendants and any live claude.exe are never selected. A pid not
    selected here is untouched; `stop <pid>` keeps refusing it as before."""

    name = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    processes = (table or real_process_table)()
    now = time.time() if now is None else now
    kill = killer or taskkill_killer
    outcomes: list[StopOutcome] = []
    for row in _worktree_selection(name, processes):
        if dry_run:
            log(_describe(row, reaches=None, now=now, verdict="dry-run -- would stop (named by worktree path)"))
            outcomes.append(StopOutcome(pid=row.pid, row=row, reaches_claude=None, action="dry-run"))
        elif kill(row.pid):
            log(_describe(row, reaches=None, now=now, verdict="stopped (named by worktree path)"))
            outcomes.append(StopOutcome(pid=row.pid, row=row, reaches_claude=None, action="stopped"))
        else:
            log(_describe(row, reaches=None, now=now, verdict="stop failed"))
            outcomes.append(StopOutcome(pid=row.pid, row=row, reaches_claude=None, action="kill-failed"))
    if not outcomes:
        log(f"no process names .claude/worktrees/{name}")
    return outcomes


def exit_code(outcomes: Sequence[StopOutcome]) -> int:
    """0 only when every pid was stopped (or, under `--dry-run`, would have been); non-zero when
    any pid was refused, not found, or its kill failed."""

    return 0 if all(o.action in _CLEAN_ACTIONS for o in outcomes) else 1


# -- CLI wiring ---------------------------------------------------------------------------------
#
# `stop.py` imports nothing of the repo it runs in (`.core_format`, `.orphans`, stdlib only) --
# a core file (`core_manifest.txt`), wired the same way as `merge.py`'s `commit` command: a
# top-level import in `cli.py`, not a local module of `config/orchestration.json`.

def add_stop_subparser(sub) -> None:
    parser = sub.add_parser(
        "stop", help="stop a process that is not this session's own child, refusing a live session's own (#250)")
    parser.add_argument("pids", nargs="*", type=int)
    parser.add_argument("--worker-worktree", metavar="WT_NAME",
                        help="stop a returned worker's leftovers: every process naming .claude/worktrees/WT_NAME (#871)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the chain and verdict for each pid, stop nothing")


def run_stop_command(args) -> int:
    if args.worker_worktree:
        return exit_code(stop_worker_worktree(args.worker_worktree, dry_run=args.dry_run))
    if not args.pids:
        print("stop: give pids or --worker-worktree")
        return 2
    outcomes = stop_processes(args.pids, dry_run=args.dry_run)
    return exit_code(outcomes)
