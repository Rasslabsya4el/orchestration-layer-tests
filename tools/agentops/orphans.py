"""A background shell an ORCH session started (Git Bash's `find /`, `grep`, `rg`, `tail -f`,
`sleep`, a `bash -c "until ..."` wait loop) can outlive the session that started it, and, measured
live, a `find /` over every drive burns a core for hours. An earlier fix here -- "no live Windows
parent" -- was wrong: Git Bash starts every child through a launcher process of its own that
exits at once, so a live shell's children look exactly like an orphan's under `Win32_Process` alone.
Measured live that the child's MSYS ancestor chain (`ps -eW`'s own
PID/PPID, not Windows `ParentProcessId`) survives this and correctly reaches the live shell that owns
it; only when THAT chain is unavailable (a native, non-MSYS child) does the Windows `ParentProcessId`
chain mean anything.

A process is stopped only when its chain (MSYS-preferred, falling back to the Windows `ppid` for
whichever hop the MSYS chain cannot resolve -- `_chain_pids`) reaches the ENDED session's own
`claude.exe` pid -- recorded while it was still live (`_Current.claude_pid`, dispatch.py) -- never
merely for lacking a live Windows parent. A chain that reaches ANY other still-live session's
`claude.exe` is never touched, whatever else is true of it. A candidate whose chain reaches neither
is left alone and named once in the log -- a human decides, the sweep never
guesses.

A later measurement (the OPEN RISK found once the first fix landed) showed live that ending the
session can kill the very bridge rows (an intermediate `bash.exe`) a fresh chain walk needs, so a
genuine orphan can come up tied to nothing at all once its owning shells are gone. The fix: the
dispatcher takes a live snapshot of "whose chain reaches me" (`snapshot_owned_pids`) on a throttled
tick while each session is still alive, accumulates it into that session's `_Current.owned_pids`,
and hands it to the sweep at the end as `recorded_owned_pids` -- a second, live-caught tie
`find_orphans`/`find_untied_candidates` accept beside the freshly recomputed chain, never in place
of it.

This module lists such survivors from an injectable process table and stops them through an
injectable killer, so the whole logic is tested on a fake table -- no real process is ever touched by
a test.

Wiring: `Dispatcher._finish` / `_timeout` (dispatch.py) call `sweep_orphans` right after
`end_session`, with the real process table (`real_process_table`) and a killer that calls
`taskkill /PID <pid> /F` (no `/T`: only the orphan itself, never a child that might have its own live
parent by then). `Dispatcher.tick` also calls `sweep_stale_whole_disk_searches` on a throttled tick:
a whole-disk `find`/`grep`/`rg` tied to ANY live `claude.exe` (any session)
and older than `WHOLE_DISK_MIN_AGE_SECONDS` is stopped outright -- the project rule forbids such a
search for every agent, so no live/ended distinction applies there.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

#: #1071: the side files of one sweep's left-over candidates -- machine output, an ordinary folder
#: under the artifacts link, created on first use, pruned by age on every sweep.
ORPHANS_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "runs" / "orphans"
ORPHANS_KEEP_DAYS = 14

#: base names (lower-case, with or without the Windows `.exe` suffix) of executables counted as an
#: orphaned search/wait shell (the original problem's own list). Matched against the command line's own leading
#: executable token, never a substring, so an unrelated program is never caught.
ORPHAN_EXE_NAMES = frozenset({"find", "grep", "rg", "tail", "sleep"})

#: `until` is a bash loop keyword, not its own executable -- a `bash -c "until ..."` process is
#: recognised by its `-c` argument containing a bare `until`, not by its own exe name.
_UNTIL_RE = re.compile(r"\buntil\b")
_SHELL_EXE_NAMES = frozenset({"bash", "sh"})


class MsysTableUnreadable(RuntimeError):
    """Raised by `real_msys_table` when `ps` WAS found (`_find_ps_exe` returned a path) but its
    table could not be read: the run failed, timed out, or its output did not parse as `ps -eW`'s
    own `PID PPID PGID WINPID ...` columns. Distinct from `ps` not being found at all, which is not
    a failure and still returns `{}` (the Windows `ppid` chain decides alone, as before this
    exception existed).

    #258: the orphan sweep (`real_process_table` / `_merge_msys`) catches this and falls back to
    the Windows chain exactly as it did when `real_msys_table` used to return `{}` for every
    failure -- its behaviour is unchanged. `tools/agentops/stop.py` is the one caller that must NOT
    swallow it: a chain it could not read is not "no session ties this pid", and mistaking the two
    was the night's second mistake (#250)."""

    #: the Windows rows read in the same call, set by `_merge_msys(strict=True)` so `stop` can still
    #: describe each pid it refuses without a second process-table read.
    rows: list = []


@dataclass(frozen=True, slots=True)
class ProcRow:
    """One row of a process table, real or fake: `pid` and `ppid` are the WINDOWS process id and
    `Win32_Process.ParentProcessId` (broken for a Git Bash child -- see the module docstring),
    wall-clock create time (`time.time()`-style epoch seconds, matching `real_process_table`'s own
    unit) and the full command line.

    `msys_pid` / `msys_ppid` are the same process's identity in MSYS's OWN pid space (`ps
    -eW`'s `PID`/`PPID` columns) when the process is a Cygwin/MSYS one and the table could read it --
    `None` for a native process, or when `real_msys_table` itself was unavailable. `msys_ppid` of `0`
    or `1` means MSYS itself has no MSYS-tracked parent for this row -- NOT that its owning shell
    exited (measured live: it is exactly the boundary where a native
    process, e.g. `python.exe` or another session's `claude.exe`, forked straight into `bash.exe`, or
    one `bash.exe` exec'd into `.../usr/bin/bash.exe`; the row itself is routinely alive). A chain
    walk (`_chain_pids`) falls back to this SAME row's own Windows `ppid` for that one hop rather than
    stopping, since that Windows relationship is intact right at this boundary even though it is
    broken (a dead launcher stub) for a genuinely detached leaf's own immediate `ppid`.

    `msys_pgid` is `ps -eW`'s own `PGID` column (#239): measured live that a subshell-wrapped
    background pipeline (`(cmd | cmd &)`, no job control in a non-interactive shell) reports BOTH its
    MSYS `ppid` AND its Windows `ppid` as already-dead/unresolvable the moment the wrapping subshell
    exits -- `_chain_pids` cannot cross that hop at all -- while every row born of the same command
    keeps the SAME `msys_pgid` as the still-live shell that started it. `_chain_pids` falls back to
    treating `msys_pgid` as one more hop (looked up by MSYS pid) only once BOTH the MSYS-ppid and
    Windows-ppid hops for that row are unresolvable, so a row whose ordinary chain already resolves
    never takes this extra hop. `None` for a native process or when `real_msys_table` was
    unavailable, exactly like `msys_pid`/`msys_ppid`."""

    pid: int
    ppid: int
    create_time: float
    command_line: str
    msys_pid: int | None = None
    msys_ppid: int | None = None
    msys_pgid: int | None = None


def _exe_name(token: str) -> str:
    base = token.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    return base


_LEADING_TOKEN_RE = re.compile(r'^\s*"([^"]+)"|^\s*(\S+)')


def _leading_exe_token(command_line: str) -> str:
    """The command line's own first token -- quoted (`"C:\\Program Files\\...\\find.exe" / ...`)
    or bare -- without splitting a quoted path that itself contains spaces on those spaces."""

    m = _LEADING_TOKEN_RE.match(command_line)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def is_orphan_command(command_line: str) -> bool:
    """True when `command_line`'s own leading executable is one of `ORPHAN_EXE_NAMES`, or it is a
    `bash -c "until ..."` wait loop. Matched by token, never by substring, so e.g. `grepper.exe` or
    a path merely containing `find` is never caught."""

    stripped = command_line.strip()
    if not stripped:
        return False
    exe = _exe_name(_leading_exe_token(stripped))
    if exe in ORPHAN_EXE_NAMES:
        return True
    if exe in _SHELL_EXE_NAMES and "-c" in stripped.split():
        rest = stripped.split("-c", 1)[1]
        if _UNTIL_RE.search(rest):
            return True
    return False


def _row_maps(processes: Sequence[ProcRow]) -> tuple[dict[int, ProcRow], dict[int, ProcRow]]:
    """`(by Windows pid, by MSYS pid)` -- the two lookups every chain walk needs."""

    by_pid = {p.pid: p for p in processes}
    by_msys_pid = {p.msys_pid: p for p in processes if p.msys_pid is not None}
    return by_pid, by_msys_pid


def _chain_pids(row: ProcRow, by_pid: dict[int, ProcRow], by_msys_pid: dict[int, ProcRow], *,
                max_depth: int = 64) -> Iterable[int]:
    """Yield `row`'s ancestors' Windows pids, one hop at a time: the MSYS ppid chain when it
    resolves, else the Windows `ppid` chain for that same hop, else (#239) the row's own MSYS
    `msys_pgid` treated as one more hop. Stops on a missing row, a cycle, or `max_depth` hops.

    Measured live, added as a risk note after the first fix landed, that an MSYS
    `msys_ppid` of `0` or `1` does NOT mean "the owning shell exited": Git Bash's own launcher
    boundary (a native process -- `python.exe`, another session's `claude.exe` -- execing straight
    into `bash.exe`, or one `bash.exe` execing into `.../usr/bin/bash.exe`) reports exactly that
    sentinel for a row that is very much alive, while that SAME row's own Windows `ParentProcessId`
    is correct and live. Falling back to the Windows `ppid` only when the MSYS chain is exhausted --
    never for the leaf's OWN immediate `ppid`, which is the original problem's already-dead launcher
    stub and stays unresolvable either way -- lets the walk cross that boundary instead of stopping
    short of it.

    #239: a subshell-wrapped background pipeline (`(cmd | cmd &)`) is a DIFFERENT boundary --
    measured live that once the wrapping subshell exits, BOTH the row's MSYS `ppid` and its Windows
    `ppid` come back unresolvable/wrong (the Windows `ParentProcessId` was reused/stale, not merely
    absent), so neither of the two hops above can cross it. What DOES survive, measured on the same
    live rows: every process born of that one command line keeps the SAME `msys_pgid` as the
    still-live shell that started it (non-interactive bash never gives the backgrounded pipeline its
    own process group). So, only once neither the MSYS-ppid nor the Windows-ppid hop resolved a
    parent, the walk takes one more hop through `by_msys_pid.get(current.msys_pgid)` -- the pgid
    leader's own row -- as long as that is not the row's own MSYS pid (a group leader pointing at
    itself would loop forever)."""

    seen = {row.pid}
    current = row
    for _ in range(max_depth):
        parent = None
        if current.msys_pid is not None and current.msys_ppid is not None:
            parent = by_msys_pid.get(current.msys_ppid)
        if parent is None:
            parent = by_pid.get(current.ppid)
        if parent is None and current.msys_pgid is not None and current.msys_pgid != current.msys_pid:
            parent = by_msys_pid.get(current.msys_pgid)
        if parent is None or parent.pid in seen:
            return
        seen.add(parent.pid)
        yield parent.pid
        current = parent


def _chain_reaches(row: ProcRow, targets: set[int], by_pid: dict[int, ProcRow],
                   by_msys_pid: dict[int, ProcRow]) -> bool:
    if not targets:
        return False
    if row.pid in targets:
        return True
    return any(pid in targets for pid in _chain_pids(row, by_pid, by_msys_pid))


def find_orphans(processes: Sequence[ProcRow], *, session_started_at: float,
                 live_pids: Iterable[int], ended_claude_pid: int | None = None,
                 live_claude_pids: Iterable[int] = (),
                 recorded_owned_pids: Iterable[int] = ()) -> list[ProcRow]:
    """Candidates: `create_time >= session_started_at` (started after the session did) and a command
    line `is_orphan_command`.

    `ended_claude_pid` is the chain tie: given, a candidate is an orphan when its chain
    (`_chain_reaches`, MSYS-preferred, falling back to the Windows `ppid` per hop -- see
    `_chain_pids`) reaches `ended_claude_pid`, OR (the OPEN RISK measured live once the chain-tie fix
    had landed) its pid is one of `recorded_owned_pids` -- pids the dispatcher itself saw, WHILE the
    session was still alive, chain up to `ended_claude_pid` (`_Current.owned_pids`, refreshed each
    throttled tick). The recorded tie exists because ending the session can kill the very bridge
    rows (an intermediate `bash.exe`) a fresh chain walk would need: once they are gone, NOTHING in
    the current table can reach back up to `ended_claude_pid` any more, live or not. Either way, a
    candidate that also reaches any of `live_claude_pids` (nor has a Windows `ppid` in `live_pids` --
    kept as a second signal, never the only one) is never touched. `ended_claude_pid=None` (no chain
    tie data at all) falls back to the original rule -- `ppid` missing from `live_pids` -- for a table
    that carries no MSYS/claude-pid information (the fake tables of the original tests, the acceptance
    row that must still pass unchanged); `recorded_owned_pids` is then ignored too."""

    live = set(live_pids)
    live_claude = set(live_claude_pids)
    recorded = set(recorded_owned_pids)
    by_pid, by_msys_pid = _row_maps(processes)
    candidates = (p for p in processes
                 if p.create_time >= session_started_at and is_orphan_command(p.command_line))
    if ended_claude_pid is None:
        return [p for p in candidates if p.ppid not in live]
    out = []
    for p in candidates:
        if p.ppid in live or _chain_reaches(p, live_claude, by_pid, by_msys_pid):
            continue
        if _chain_reaches(p, {ended_claude_pid}, by_pid, by_msys_pid) or p.pid in recorded:
            out.append(p)
    return out


def find_untied_candidates(processes: Sequence[ProcRow], *, session_started_at: float,
                          ended_claude_pid: int | None, live_claude_pids: Iterable[int],
                          recorded_owned_pids: Iterable[int] = ()) -> list[ProcRow]:
    """Candidates (`is_orphan_command`, started after the session) whose chain
    reaches neither the ended session's own `claude.exe` nor any live one, AND are not in
    `recorded_owned_pids` (the follow-up's recorded tie -- see `find_orphans`) -- left alone,
    named once in the log by the caller, never guessed at. Empty when `ended_claude_pid` is `None`
    (no tie data: the original callers never asked for this)."""

    if ended_claude_pid is None:
        return []
    live_claude = set(live_claude_pids)
    recorded = set(recorded_owned_pids)
    by_pid, by_msys_pid = _row_maps(processes)
    out = []
    for p in processes:
        if p.create_time < session_started_at or not is_orphan_command(p.command_line):
            continue
        if p.pid in recorded:
            continue
        if _chain_reaches(p, live_claude, by_pid, by_msys_pid):
            continue
        if _chain_reaches(p, {ended_claude_pid}, by_pid, by_msys_pid):
            continue
        out.append(p)
    return out


def snapshot_owned_pids(claude_pids: Iterable[int], *,
                        table: Callable[[], Sequence[ProcRow]] | None = None) -> dict[int, frozenset[int]]:
    """The follow-up's OPEN RISK: while a session's `claude.exe` is still alive, the pids of
    every OTHER row in `processes` whose chain (`_chain_reaches`, MSYS-preferred, Windows-`ppid`
    fallback) reaches it -- one dict entry per pid of `claude_pids`, each a live-caught set the
    dispatcher folds into that session's `_Current.owned_pids` (accumulated, never shrunk, across
    ticks: a pid seen tied once stays recorded even if a later sample cannot re-derive the tie).
    Read live at the sweep at session end, when the bridge rows that made the tie derivable may
    already be dead (`find_orphans`'s own `recorded_owned_pids` tie). `table` defaults to
    `real_process_table` (overridable so a test never touches a real process)."""

    processes = (table or real_process_table)()
    by_pid, by_msys_pid = _row_maps(processes)
    targets = list(claude_pids)
    out: dict[int, set[int]] = {cp: set() for cp in targets}
    for p in processes:
        for cp in targets:
            if p.pid == cp:
                continue
            if _chain_reaches(p, {cp}, by_pid, by_msys_pid):
                out[cp].add(p.pid)
    return {cp: frozenset(pids) for cp, pids in out.items()}


def stop_orphans(orphans: Iterable[ProcRow], *, killer: Callable[[int], bool],
                 log: Callable[[str], None]) -> list[ProcRow]:
    """Stop each of `orphans` through the injectable `killer(pid) -> bool`; one `log` line per
    process actually stopped. Returns the rows the killer confirmed
    stopped -- a killer returning `False` for a pid logs nothing and is left out."""

    stopped: list[ProcRow] = []
    for row in orphans:
        if killer(row.pid):
            stopped.append(row)
            log(f"stopped orphan pid {row.pid} left behind by the ended session "
                f"({row.command_line[:200]})")
    return stopped


def find_live_tied_candidates(processes: Sequence[ProcRow], *, session_started_at: float,
                              live_claude_pids: Iterable[int]) -> list[tuple[ProcRow, int]]:
    """#947: candidates (`is_orphan_command`, started after the session) whose chain reaches one of
    `live_claude_pids` -- the veto `find_orphans` applies, never stopped -- each paired with the live
    `claude.exe` pid it reaches (`stop._chain_reach_claude`), so `sweep_orphans` can name that session
    in the log instead of staying silent about it."""

    from .stop import _chain_reach_claude

    live_claude = set(live_claude_pids)
    if not live_claude:
        return []
    by_pid, by_msys_pid = _row_maps(processes)
    out: list[tuple[ProcRow, int]] = []
    for p in processes:
        if p.create_time < session_started_at or not is_orphan_command(p.command_line):
            continue
        hit = _chain_reach_claude(p, live_claude, by_pid, by_msys_pid)
        if hit is not None:
            out.append((p, hit))
    return out


def sweep_orphans(*, session_started_at: float, log: Callable[[str], None],
                  ended_claude_pid: int | None = None, live_claude_pids: Iterable[int] = (),
                  recorded_owned_pids: Iterable[int] = (),
                  table: Callable[[], Sequence[ProcRow]] | None = None,
                  killer: Callable[[int], bool] | None = None,
                  ended_session: str | None = None,
                  orphans_dir: Path | None = None,
                  now: datetime | None = None) -> list[ProcRow]:
    """The one call `Dispatcher._finish` / `_timeout` make: read the process table, find this
    session's orphans (tied to `ended_claude_pid` -- directly, or through `recorded_owned_pids`, the
    follow-up's live-caught tie -- never to a live claude.exe), stop them, log each; a
    candidate tied to neither is left alone. `table`
    defaults to `real_process_table`, `killer` to `taskkill_killer` -- both overridable so a test
    never touches a real process. `ended_claude_pid=None` (no chain tie data) falls back to the
    original rule, for a caller/table that never carries it.

    #947: the live set the sweep vetoes with is `live_claude_pids` (the ORCH sessions' pids, from the
    dispatcher) PLUS every live `claude.exe` in the table (an autopilot session the dispatcher does not
    run), MINUS `ended_claude_pid`. A candidate whose chain reaches any of them is never stopped; it is
    a left-over candidate like any other.

    #1071: the left-over candidates (tied or untied) are not logged one by one. When there are any, one
    summary line `left N orphan candidates (T tied to live sessions, U untied): <side file>` is logged,
    and the per-candidate lines (today's wording) go into the side file
    `<orphans_dir>/<YYYYMMDD-HHMM>-<ended_session or 'none'>.txt`. The folder is created on first use
    (`orphans_dir` defaults to `ORPHANS_DIR`, `now` to the wall clock); every sweep also removes the
    side files older than `ORPHANS_KEEP_DAYS` by mtime."""

    # local imports: `stop` imports this module, so a module-level import would be circular
    from .stop import live_claude_pids as _table_live_claude_pids

    processes = (table or real_process_table)()
    live_pids = {p.pid for p in processes}
    live_claude = (set(live_claude_pids) | set(_table_live_claude_pids(processes))) - {ended_claude_pid}
    orphans = find_orphans(processes, session_started_at=session_started_at, live_pids=live_pids,
                           ended_claude_pid=ended_claude_pid, live_claude_pids=live_claude,
                           recorded_owned_pids=recorded_owned_pids)
    stopped = stop_orphans(orphans, killer=killer or taskkill_killer, log=log)
    tied: list[tuple[ProcRow, int]] = []
    if ended_claude_pid is not None:
        tied = find_live_tied_candidates(processes, session_started_at=session_started_at,
                                         live_claude_pids=live_claude)
    untied = find_untied_candidates(processes, session_started_at=session_started_at,
                                    ended_claude_pid=ended_claude_pid,
                                    live_claude_pids=live_claude,
                                    recorded_owned_pids=recorded_owned_pids)
    lines = [f"left orphan candidate pid {row.pid} tied to live claude.exe pid {live_pid}, never "
             f"stopped ({row.command_line[:200]})" for row, live_pid in tied]
    lines += [f"left orphan candidate pid {row.pid} with no tie to the ended session or any live one "
              f"({row.command_line[:200]})" for row in untied]
    folder = orphans_dir or ORPHANS_DIR
    clock = now or datetime.now()
    _prune_side_files(folder, now=clock)
    if lines:
        side = _write_side_file(folder, lines, now=clock, ended_session=ended_session)
        log(f"left {len(lines)} orphan candidates ({len(tied)} tied to live sessions, "
            f"{len(untied)} untied): {side}")
    return stopped


def _write_side_file(folder: Path, lines: list[str], *, now: datetime,
                     ended_session: str | None) -> Path:
    """#1071: one side file per sweep that left candidates; the name carries the sweep's minute and the
    ended session's id (`none` when the sweep has none), with `-2`, `-3`... only on a same-minute clash."""

    folder.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", str(ended_session)) if ended_session else "none"
    stem = f"{now:%Y%m%d-%H%M}-{tag}"
    path = folder / f"{stem}.txt"
    n = 2
    while path.exists():
        path = folder / f"{stem}-{n}.txt"
        n += 1
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _prune_side_files(folder: Path, *, now: datetime) -> None:
    """#1071: remove the side files in `folder` whose mtime is older than `ORPHANS_KEEP_DAYS`. A
    folder that does not exist yet is left alone (no folder is created here)."""

    if not folder.is_dir():
        return
    cutoff = (now - timedelta(days=ORPHANS_KEEP_DAYS)).timestamp()
    for path in folder.glob("*.txt"):
        if path.stat().st_mtime < cutoff:
            path.unlink()


# -- the real process table and killer (Windows) ----------------------------------------------------

def real_process_table(*, strict_msys: bool = False) -> list[ProcRow]:
    """`Get-CimInstance Win32_Process` -> `ProcRow` rows, windowless (every subprocess call this
    module adds passes `CREATE_NO_WINDOW` on Windows). `CreationDate` is a CIM datetime
    (`yyyymmddHHMMSS.ffffff+UUU`); converted to a `time.time()`-style epoch so it compares directly
    with `_Current.started_at`'s wall-clock estimate. A row whose `CreationDate` is missing (a
    process that exited mid-query) is skipped."""

    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    kwargs: dict = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=30)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            **kwargs)
    if result.returncode != 0:
        return []
    import json

    try:
        data = json.loads(result.stdout or "[]", strict=False)  # PS 5.1 leaves raw control chars in command lines
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows: list[ProcRow] = []
    for entry in data:
        created = _parse_cim_datetime(entry.get("CreationDate"))
        if created is None:
            continue
        pid = entry.get("ProcessId")
        ppid = entry.get("ParentProcessId")
        if pid is None or ppid is None:
            continue
        rows.append(ProcRow(pid=int(pid), ppid=int(ppid), create_time=created,
                            command_line=entry.get("CommandLine") or ""))
    return _merge_msys(rows, strict=strict_msys)


def _merge_msys(rows: list[ProcRow], *, strict: bool = False) -> list[ProcRow]:
    """Fill in `msys_pid` / `msys_ppid` / `msys_pgid` on every row that `real_msys_table` also
    knows, by Windows pid (`ps -eW`'s own `WINPID` column). A row `real_msys_table` has no entry for
    (a native process, or the table read failed) keeps `msys_pid=None` -- its chain walk then falls
    back to the Windows `ppid` alone, unchanged from before this module's MSYS chain.

    #258: `real_msys_table` now raises `MsysTableUnreadable` instead of returning `{}` on a genuine
    read failure (`ps` found but not readable) -- caught here and treated exactly like `{}` so the
    orphan sweep's own behaviour is unchanged by that fix. `tools/agentops/stop.py` reads the table
    through `real_process_table(strict_msys=True)`, which re-raises it (one `ps -eW` read decides both
    the chain and the refusal)."""

    try:
        msys_by_winpid = real_msys_table()
    except MsysTableUnreadable as exc:
        if strict:  # #258: `stop` must see it, with the rows it still describes
            exc.rows = rows
            raise
        msys_by_winpid = {}
    if not msys_by_winpid:
        return rows
    out = []
    for row in rows:
        hit = msys_by_winpid.get(row.pid)
        if hit is None:
            out.append(row)
        else:
            msys_pid, msys_ppid, msys_pgid = hit
            out.append(ProcRow(pid=row.pid, ppid=row.ppid, create_time=row.create_time,
                               command_line=row.command_line, msys_pid=msys_pid, msys_ppid=msys_ppid,
                               msys_pgid=msys_pgid))
    return out


def _find_ps_exe() -> str | None:
    """Git Bash's own `ps.exe` (MSYS's process table, not Windows'): on `PATH` first, else the usual
    install locations. `None` when it cannot be found -- `real_msys_table` then returns `{}`, and
    every row's chain walk falls back to the Windows `ppid` alone (the original, pre-MSYS-chain
    behaviour)."""

    import shutil

    found = shutil.which("ps")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\usr\bin\ps.exe",
                     r"C:\Program Files (x86)\Git\usr\bin\ps.exe"):
        try:
            import os

            if os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    return None


def real_msys_table() -> dict[int, tuple[int, int, int]]:
    """`ps -eW` (Git Bash's own `ps`, MSYS's process table, not `Win32_Process`'s) -> `{winpid:
    (msys_pid, msys_ppid, msys_pgid)}`. Measured live that this chain survives a Git Bash child
    outliving the launcher process `Win32_Process.ParentProcessId` names (see the module docstring);
    `ps -eW`'s columns are `PID PPID PGID WINPID TTY UID STIME COMMAND` -- only the first four
    (always numeric, unlike `STIME`/`COMMAND` which vary in width) are read.

    `{}` ONLY when `ps` cannot be found at all (`_find_ps_exe` returns `None`) -- nothing failed,
    there is simply no MSYS table on this machine, and callers fall back to the Windows `ppid`
    alone exactly as before #258. Once `ps` IS found, a failed run, a timeout (15 s) or output that
    does not parse raises `MsysTableUnreadable` instead -- #258: a chain that could not be read must
    never be mistaken for "ps ran and found no session", which is what a silent `{}` used to look
    like to a caller with no other signal."""

    exe = _find_ps_exe()
    if exe is None:
        return {}
    kwargs: dict = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=15)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([exe, "-eW"], **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise MsysTableUnreadable(f"ps -eW timed out: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise MsysTableUnreadable(f"ps -eW could not be run: {exc}") from exc
    if result.returncode != 0:
        raise MsysTableUnreadable(f"ps -eW exited {result.returncode}")
    lines = (result.stdout or "").splitlines()
    if not lines:
        raise MsysTableUnreadable("ps -eW produced no output")
    data_lines = lines[1:]  # skip the header row
    out: dict[int, tuple[int, int, int]] = {}
    bad = 0
    for line in data_lines:
        parts = line.split(None, 4)
        if len(parts) < 4:
            bad += 1
            continue
        try:
            msys_pid, msys_ppid, pgid, winpid = (int(parts[0]), int(parts[1]), int(parts[2]),
                                                 int(parts[3]))
        except ValueError:
            bad += 1
            continue
        out[winpid] = (msys_pid, msys_ppid, pgid)
    if data_lines and bad == len(data_lines):
        raise MsysTableUnreadable("ps -eW output did not parse (no row read as PID PPID PGID WINPID)")
    return out


def _parse_cim_datetime(value: object) -> float | None:
    """`/Date(1234567890000)/`-style (PowerShell's `ConvertTo-Json` of a `DateTime`) or a plain
    CIM `yyyymmddHHMMSS.ffffff+UUU` string -> epoch seconds; `None` when unparseable."""

    if not isinstance(value, str) or not value:
        return None
    m = re.match(r"^/Date\((-?\d+)\)/$", value)
    if m:
        return int(m.group(1)) / 1000.0
    m = re.match(r"^(\d{14})\.(\d{6})", value)
    if not m:
        return None
    import datetime

    stamp = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp() + int(m.group(2)) / 1_000_000.0


def taskkill_killer(pid: int) -> bool:
    """End one orphan by pid: `taskkill /PID <pid> /F`, no `/T` -- only the orphan itself, never a
    subtree that might by now hold something with its own live parent. A pid already gone counts as
    stopped, matching `end_process`'s own convention (dispatch.py)."""

    kwargs: dict = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=15)
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], **kwargs)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    return "not found" in detail.lower()


# -- whole-disk find/grep/rg, stopped outright ------------------------------------------------------

#: the project rule (every agent's CLAUDE.md) forbids a search rooted at the whole disk; these are the
#: three tools the rule names.
WHOLE_DISK_EXE_NAMES = frozenset({"find", "grep", "rg"})

#: a bare drive root, in any of the forms Git Bash / cmd write it: `/`, `/c`, `/c/`, `c:\`, `c:/`.
_DRIVE_ROOT_RE = re.compile(r"^(/|/[a-zA-Z]/?|[a-zA-Z]:[\\/]?)$")

#: how long a whole-disk search runs before the sweep stops it (the project's own bar: "older than 10 min").
WHOLE_DISK_MIN_AGE_SECONDS = 600.0


def _is_drive_root_token(token: str) -> bool:
    return bool(_DRIVE_ROOT_RE.match(token.strip().strip('"')))


def is_whole_disk_search(command_line: str) -> bool:
    """True when `command_line`'s own leading executable is `find`, `grep` or `rg` (never a
    substring/lookalike -- `is_orphan_command`'s own `_leading_exe_token`/`_exe_name`) AND one of its
    own arguments is a bare drive root (`_is_drive_root_token`), e.g. `find / -name *.log`, `grep -r
    foo /c`, `rg pattern C:\\`. A search rooted at a real subdirectory (`find src -name *.py`) is
    never caught."""

    stripped = command_line.strip()
    if not stripped:
        return False
    exe = _exe_name(_leading_exe_token(stripped))
    if exe not in WHOLE_DISK_EXE_NAMES:
        return False
    import shlex

    try:
        tokens = shlex.split(stripped, posix=False)
    except ValueError:
        tokens = stripped.split()
    return any(_is_drive_root_token(t) for t in tokens[1:])


def find_stale_whole_disk_searches(processes: Sequence[ProcRow], *, now: float,
                                  min_age_seconds: float = WHOLE_DISK_MIN_AGE_SECONDS) -> list[ProcRow]:
    """Rows of `processes` that are `is_whole_disk_search`, older than `min_age_seconds`, and whose
    chain (`_chain_reaches`, MSYS-preferred) reaches ANY `claude.exe` process in `processes` itself
    (any session, live -- the project rule applies to every one of them, so no live/ended
    distinction is drawn here, unlike `find_orphans`)."""

    by_pid, by_msys_pid = _row_maps(processes)
    claude_pids = {p.pid for p in processes if _exe_name(_leading_exe_token(p.command_line)) == "claude"}
    if not claude_pids:
        return []
    return [p for p in processes
            if now - p.create_time >= min_age_seconds and is_whole_disk_search(p.command_line)
            and _chain_reaches(p, claude_pids, by_pid, by_msys_pid)]


def sweep_stale_whole_disk_searches(*, log: Callable[[str], None],
                                    table: Callable[[], Sequence[ProcRow]] | None = None,
                                    killer: Callable[[int], bool] | None = None,
                                    now: float | None = None,
                                    min_age_seconds: float = WHOLE_DISK_MIN_AGE_SECONDS) -> list[ProcRow]:
    """The one call `Dispatcher._maybe_sweep_whole_disk_searches` makes: read
    the process table, stop every stale whole-disk search tied to a live `claude.exe`, one log line
    each. `table`/`killer` overridable so a test never touches a real process."""

    import time as _time

    processes = (table or real_process_table)()
    stale = find_stale_whole_disk_searches(processes, now=now if now is not None else _time.time(),
                                          min_age_seconds=min_age_seconds)
    stopped = []
    kill = killer or taskkill_killer
    for row in stale:
        if kill(row.pid):
            stopped.append(row)
            log(f"stopped whole-disk search pid {row.pid} tied to a live claude.exe "
                f"({row.command_line[:200]})")
    return stopped
