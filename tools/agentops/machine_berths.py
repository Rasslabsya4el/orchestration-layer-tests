"""Machine berths: heavy suite runs from every repo on the PC take turns (#224).

A heavy run is a merge suite or line check that runs the full suite or more than `HEAVY_TEST_LIMIT` selected tests
(`is_heavy`). Before it starts it calls `acquire`, which blocks until the run holds one of `berths` places (setting
`machine_heavy_runs`, default 1; 0 = no limit and nothing is recorded); `release` (or leaving the `with` block) frees
it. A run of `HEAVY_TEST_LIMIT` tests or fewer calls nothing here.

The same line also counts GitHub machines (#232, D119): `acquire_machines` blocks until the merge holds
`min(want, free)` of `github_machines_total` machines (setting, default 20; 0 = no limit and nothing is recorded),
`want` being at most `github_machines_per_merge` (default 5). A place is either the PC's berth (`kind` `pc`) or a count
of machines (`kind` `github`). Both kinds wait in the ONE order below, but a waiter is blocked only by waiters of its
OWN kind ahead of it, so a busy PC berth never holds back a merge that finds machines free. A merge takes machines only
as the head of the github waiters, and gets fewer than it wants only when fewer are free and no github waiter is ahead
of it. The PC's berth counts holders of kind `pc`, machines count the `machines` of holders of kind `github`: every log
line carries `kind`, and a PC line's `holders` counts PC holders only (a github line's `holders` counts github
holders and it adds `machines` and `machines_in_use`); `max_simultaneous_holders` and `max_simultaneous_machines`
replay the log for each kind.

Everything lives under `machine_dir()` (`machine_mode.py`), outside every repo: `heavy_runs/` holds one file per
holder and one per waiter, and `heavy_runs.jsonl` is the log, one line per take and per free. A holder is alive by
its pid and the time it took the berth (`util.holder_is_live`), never by CPU activity, so a run the hold engine froze
in `play` mode keeps its berth, and a holder whose process is dead is free: the next try of any waiter removes it
and logs the free. A live PC holder that stays silent is ended too (#249): on any waiter's try, a holder whose
output file (`out_path`, written at take time) has not grown for more than `berth_silent_max_minutes` is stopped with
its whole process tree, unless the hold engine froze it; a holder with no output path is ended past 3 h held. Waiters go in this order: a Priority ticket's run first (oldest first), then a waiter that has
waited more than `line_max_wait_minutes` (oldest first, whatever its repo), then the rest by the owner's repo order
(`repo_order.py`) and within a repo by the time they began waiting. A free berth goes only to the head waiter.
A short lock file makes each look-and-take atomic across processes.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import core_settings, hold, repo_identity, repo_order
from .machine_mode import MachineModeUnreadable, current_mode, machine_dir, stood_between
from .util import AgentOpsError, append_jsonl, holder_is_live, pid_alive, write_json

#: a run of more selected tests than this (or a full suite) is heavy and takes a berth.
HEAVY_TEST_LIMIT = 500

_FOLDER = "heavy_runs"
_LOG = "heavy_runs.jsonl"
_LOCK_STALE_SECONDS = 30.0

Liveness = Callable[[int, str], bool]


def is_heavy(selected_tests: int, full_suite: bool) -> bool:
    """True for a full suite or more than `HEAVY_TEST_LIMIT` selected tests."""
    return bool(full_suite) or selected_tests > HEAVY_TEST_LIMIT


def log_path() -> Path:
    return machine_dir() / _LOG


def _folder() -> Path:
    return machine_dir() / _FOLDER


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _hhmm(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def _lock_is_stale(lock: Path) -> bool:
    try:
        if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
            return True
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False  # unreadable or half-written: its writer is still at it
    return not pid_alive(pid)


def _unlink_retry(path: Path) -> None:
    """Delete `path`, retrying while Windows refuses because another process has it open for a moment (reading
    a lock to judge it stale); a lock left behind by one refused delete would block everyone."""
    for _ in range(400):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.005)
    path.unlink(missing_ok=True)


def _create_exclusive(path: Path) -> bool:
    """Create `path` holding our pid; False when it already exists. On Windows a lock another thread has just deleted
    can still be pending its delete and refuses the create with PermissionError: that is "not ours yet" too (#233)."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, PermissionError):
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _reclaim_stale(lock: Path) -> None:
    """Remove a lock left by a dead process. Racers take a short guard file first and look again under it, so
    a lock another racer just re-created is never removed; the stale lock is renamed to a unique name and the
    renamed file is deleted. A guard left by a process that died is old and goes the same way."""
    guard = lock.with_name("lock.reclaim")
    if not _create_exclusive(guard):
        try:
            if time.time() - guard.stat().st_mtime > _LOCK_STALE_SECONDS:
                guard.unlink()
        except OSError:
            pass
        return
    try:
        if lock.exists() and _lock_is_stale(lock):
            gone = lock.with_name(f"lock.stale.{uuid.uuid4().hex}")
            try:
                os.replace(lock, gone)
                _unlink_retry(gone)
            except OSError:
                pass
    finally:
        _unlink_retry(guard)


@contextmanager
def _locked(timeout: float = 30.0):
    """The short cross-process lock around one look-and-take. A lock whose writer is dead never blocks."""
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / "lock"
    deadline = time.monotonic() + timeout
    while True:
        if not _create_exclusive(lock):
            if _lock_is_stale(lock):
                _reclaim_stale(lock)
                continue
            if time.monotonic() > deadline:
                raise AgentOpsError(f"could not take the machine berth lock {lock} within {timeout:.0f}s")
            time.sleep(0.005)
            continue
        break
    try:
        yield
    finally:
        _unlink_retry(lock)


def _holder_path(ident: str) -> Path:
    return _folder() / f"holder-{ident}.json"


def _waiter_path(ident: str) -> Path:
    return _folder() / f"waiter-{ident}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _records(kind: str) -> list[tuple[Path, dict]]:
    folder = _folder()
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob(f"{kind}-*.json")):
        data = _read(path)
        if data is not None:
            found.append((path, data))
    return found


def _live(record: dict, is_live: Liveness) -> bool:
    return bool(is_live(int(record.get("pid") or 0), record.get("started") or ""))


def _kind(record: dict) -> str:
    return "github" if record.get("kind") == "github" else "pc"


def _machines_of(holders: list[dict]) -> int:
    return sum(int(r.get("machines") or 0) for r in holders if _kind(r) == "github")


def _pc_holders(holders: list[dict]) -> int:
    return sum(1 for r in holders if _kind(r) == "pc")


def _log(action: str, record: dict, remaining: list[dict], now: float, reason: str | None = None) -> None:
    """One log line; `remaining` is every holder record after the take or free, of both kinds."""
    kind = _kind(record)
    line = {
        "time": _stamp(now), "action": action, "kind": kind, "id": record["id"], "repo": record["repo"],
        "run": record["run"], "tests": record["tests"],
    }
    if kind == "github":
        line["machines"] = int(record.get("machines") or 0)
        line["machines_in_use"] = _machines_of(remaining)
        line["holders"] = sum(1 for r in remaining if _kind(r) == "github")
    else:
        line["holders"] = _pc_holders(remaining)
    if action == "take":  # #239: how long the run waited, from its waiter's start (the holder record carries `since`)
        line["waited_min"] = round((now - float(record.get("since", now))) / 60.0, 1)
    if reason:
        line["reason"] = reason
    append_jsonl(log_path(), line)


def _max_wait_seconds(max_wait_minutes: float | None = None) -> float:
    """#233: the bound a waiter may wait before it goes next whatever its repo, in seconds. `None` reads the
    setting `line_max_wait_minutes`; an unreadable settings file or a non-number is the default (60)."""
    default = core_settings.DEFAULTS["line_max_wait_minutes"]
    if max_wait_minutes is None:
        try:
            value = core_settings.load().get("line_max_wait_minutes", default)
        except Exception:  # noqa: BLE001 -- the line never fails on the settings file
            value = default
        max_wait_minutes = value if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    return float(max_wait_minutes) * 60.0


def _rank_label(repo_name: str) -> str | None:
    """#233 rule 4: the repo's place in the order, `"<place> of <N>"` (a repo absent from the order is place N+1), or None when no order is set."""
    position, n = repo_order.rank(repo_name)
    if n == 0:
        return None
    return f"{position + 1} of {n}"


def _order(waiters: list[tuple[Path, dict]], now: float, max_wait_s: float) -> list[tuple[Path, dict]]:
    """#233: Priority first (oldest first); then waiters past `max_wait_s` (oldest first, any repo); then the rest
    by repo rank, then wait start, then id. With no order set every repo ranks alike: Priority, then wait start."""
    def key(waiter: tuple[Path, dict]):
        record = waiter[1]
        since = record.get("since", 0.0)
        if record.get("priority"):
            tier, place = 0, 0
        elif now - since > max_wait_s:
            tier, place = 1, 0
        else:
            tier, place = 2, repo_order.rank(record["repo"])[0]
        return (tier, place, since, record["id"])

    return sorted(waiters, key=key)


def _reap_dead_holders(is_live: Liveness, now: float) -> list[dict]:
    """The live holders; a dead one is removed and logged as freed."""
    holders, dead = [], []
    for path, record in _records("holder"):
        if _live(record, is_live):
            holders.append(record)
        else:
            path.unlink(missing_ok=True)
            dead.append(record)
    for record in dead:
        _log("free", record, holders, now, reason="holder process dead")
    return holders


#: #249: a holder with no output path is ended once it has held the berth this long (seconds).
_NO_OUT_PATH_MAX_S = 3 * 3600.0


def _silent_max_seconds(silent_max_minutes: float | None = None) -> float:
    """#249: the silence a live PC holder may show before it is ended, in seconds. `None` reads the setting
    `berth_silent_max_minutes`; an unreadable settings file or a non-number is the default (15)."""
    default = core_settings.DEFAULTS["berth_silent_max_minutes"]
    if silent_max_minutes is None:
        try:
            value = core_settings.load().get("berth_silent_max_minutes", default)
        except Exception:  # noqa: BLE001 -- the line never fails on the settings file
            value = default
        silent_max_minutes = value if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    return float(silent_max_minutes) * 60.0


def _pid_of(record: dict) -> int:
    return int(record.get("pid") or 0)


@dataclass(frozen=True)
class _Silence:
    """What a waiter's try needs to end a silent PC holder: the silence bound in seconds, the killer of a holder's
    process tree (given the holder's pid) and the hold engine's freeze check (given the holder's record)."""

    max_s: float
    killer: Callable[[int], None]
    frozen: Callable[[dict], bool]


def _silence(silent_max_minutes: float | None, killer: Callable[[int], None] | None) -> _Silence:
    return _Silence(max_s=_silent_max_seconds(silent_max_minutes), killer=killer or _stop_tree, frozen=_hold_frozen)


def _hold_frozen(record: dict) -> bool:
    """True when the hold engine froze this holder: its pid is listed in a hold record (this installation's, or the
    holder's own repo's) while the machine mode is `play`. A mode that cannot be read freezes nothing it can judge,
    so it counts as frozen."""
    try:
        if current_mode() != "play":
            return False
    except MachineModeUnreadable:
        return True
    pid = _pid_of(record)
    roots = [hold.ROOT] + ([Path(str(record["root"]))] if record.get("root") else [])
    return any(entry["pid"] == pid for root in roots for entry in hold.read_record(root))


def _stop_tree(pid: int) -> None:
    """#249: stop `pid` and its whole process tree, deepest first and the pid last; a process already gone is fine.
    Never this process."""
    import psutil  # noqa: PLC0415 -- only a silent holder's stop needs it

    if pid <= 0:
        return
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    found: list[tuple[int, object]] = []

    def walk(proc, depth: int) -> None:
        try:
            kids = proc.children()
        except psutil.Error:
            kids = []
        for kid in kids:
            walk(kid, depth + 1)
        found.append((depth, proc))

    walk(root, 0)
    for _depth, proc in sorted(found, key=lambda item: -item[0]):
        if proc.pid == os.getpid():
            continue
        try:
            proc.kill()
        except psutil.Error:
            pass


def _silent_reason(record: dict, now: float, silence_max_s: float) -> str | None:
    """#249: why a live PC holder is ended, or None. With an output path: the file untouched for more than the bound
    (measured from the take while the file is absent). Without one: held past `_NO_OUT_PATH_MAX_S`."""
    taken = float(record.get("taken") or now)
    out = record.get("out_path")
    if not out:
        held = max(0.0, now - taken - stood_between(taken, now))  # play time is not held time (D117)
        if held <= _NO_OUT_PATH_MAX_S:
            return None
        minutes = int(held // 60)
        return f"holder held {minutes // 60} h {minutes % 60} min, no output path"
    try:
        since = max(Path(str(out)).stat().st_mtime, taken)  # a file older than the take is an earlier run's
    except OSError:
        since = taken
    silent = now - since - stood_between(since, now)  # time the pipeline stood in play is not silence (D117)
    if silent <= silence_max_s:
        return None
    return f"holder silent {int(silent // 60)} min"


def _reap_silent_holders(holders: list[dict], now: float, silence: _Silence) -> list[dict]:
    """#249: end every live PC holder that is silent past its bound and not frozen by the hold engine: its process
    tree is stopped, its holder file removed and a `free` line logged with the reason. Returns the holders still
    standing. Never this process, never a record without a pid."""
    kept, ended = [], []
    for record in holders:
        reason = None
        if _kind(record) == "pc" and _pid_of(record) > 0 and _pid_of(record) != os.getpid():
            reason = _silent_reason(record, now, silence.max_s)
        if reason is None or silence.frozen(record):
            kept.append(record)
            continue
        silence.killer(_pid_of(record))
        _holder_path(record["id"]).unlink(missing_ok=True)
        ended.append((record, reason))
    for record, reason in ended:
        _log("free", record, kept, now, reason=reason)
    return kept


def _try_take(waiter: dict, limit: int, is_live: Liveness, now: float, max_wait_s: float,
              silence: _Silence | None = None) -> dict | None:
    """One look-and-take under the lock: the holder record when `waiter` held the head of the waiters of its own
    kind (#233 order: Priority, past `max_wait_s`, repo order) and a berth was free, else None. `limit` is the PC's
    berths for a waiter of kind `pc` and the machines in total for kind `github`, which takes `min(want, free)`. When
    the waiter's own file is missing or unreadable it is written again and this try takes nothing. `silence` (#249)
    ends a silent PC holder first, so this same try can take its berth."""
    holders = _reap_dead_holders(is_live, now)
    if silence is not None:
        holders = _reap_silent_holders(holders, now, silence)
    live_waiters = _live_waiters(waiter, is_live)
    if not any(record["id"] == waiter["id"] for _, record in live_waiters):
        write_json(_waiter_path(waiter["id"]), waiter)
        return None
    same_kind = [w for w in live_waiters if _kind(w[1]) == _kind(waiter)]
    if _order(same_kind, now, max_wait_s)[0][1]["id"] != waiter["id"]:
        return None
    holder = {**waiter, "taken": now}
    if _kind(waiter) == "github":
        free = limit - _machines_of(holders)
        if free < 1:
            return None
        holder["machines"] = min(int(waiter["want"]), free)
    elif _pc_holders(holders) >= limit:
        return None
    write_json(_holder_path(waiter["id"]), holder)
    _waiter_path(waiter["id"]).unlink(missing_ok=True)
    _log("take", holder, holders + [holder], now)
    return holder


def _live_waiters(waiter: dict, is_live: Liveness) -> list[tuple[Path, dict]]:
    """The waiters of both kinds that are alive (the caller's own always), a dead one's file removed."""
    live = []
    for path, record in _records("waiter"):
        if record["id"] == waiter["id"] or _live(record, is_live):
            live.append((path, record))
        else:
            path.unlink(missing_ok=True)
    return live


def _blockers(waiter: dict, limit: int, is_live: Liveness, now: float, max_wait_s: float) -> str:
    """Who a github waiter waits for, for its one wait line: the github waiters ahead of it, else the holders."""
    ahead = []
    github_waiters = [w for w in _records("waiter") if _live(w[1], is_live) and _kind(w[1]) == "github"]
    for _, record in _order(github_waiters, now, max_wait_s):
        if record["id"] == waiter["id"]:
            break
        ahead.append(f"{record['repo']} ({record.get('want') or record['tests']})")
    if ahead:
        return f"waits behind {', '.join(ahead)}"
    holders = [r for _, r in _records("holder") if _live(r, is_live) and _kind(r) == "github"]
    names = ", ".join(f"{r['repo']} ({r['machines']})" for r in holders)
    return f"{_machines_of(holders)} of {limit} in use by {names}"


class HeavyBerth:
    """A place a heavy run or a merge holds: the PC's berth, or `machines` GitHub machines. `release()` frees it
    (once); leaving a `with` block releases it."""

    def __init__(self, record: dict | None, clock: Callable[[], float], machines: int = 0):
        self._record = record
        self._clock = clock
        self._machines = machines  # a line with no limit holds nothing but still says how many parts to split into

    @property
    def machines(self) -> int:
        """The GitHub machines held (0 for the PC's berth); with no limit the number wanted, recorded nowhere."""
        return int((self._record or {}).get("machines") or self._machines)

    def release(self) -> None:
        record, self._record = self._record, None
        if record is None:
            return
        with _locked():
            path = _holder_path(record["id"])
            if not path.exists():  # already freed as dead, or never recorded
                return
            record = _read(path) or record  # the holder file carries the machines taken
            path.unlink()
            _log("free", record, [r for _, r in _records("holder")], self._clock())

    def __enter__(self) -> "HeavyBerth":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(
    repo,
    run: str,
    tests: int,
    *,
    priority: bool = False,
    berths: int = 1,
    on_wait: Callable[[str], None] | None = None,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    is_live: Liveness = holder_is_live,
    pid: int | None = None,
    max_wait_minutes: float | None = None,
    out_path: str | Path | None = None,
    silent_max_minutes: float | None = None,
    killer: Callable[[int], None] | None = None,
) -> HeavyBerth:
    """Block until the run holds a berth, polling every `poll` seconds. `repo` is a path in the repo (kept
    as the repo's GitHub name, #239); `priority` is True for a Priority ticket's run. While waiting, `on_wait` is called once with
    `waits for the machine: <repo GitHub name> runs <N> tests since HH:MM`, plus ` (order: <rank>)` when an order
    is set. `max_wait_minutes` None = the setting `line_max_wait_minutes`. `berths` <= 0 returns at once and
    records nothing. A wait that raises (or is interrupted) leaves no waiter behind.
    #249: `out_path` is the file the run writes its output to, kept in the holder record; a live holder silent
    past `silent_max_minutes` (None = the setting `berth_silent_max_minutes`) is ended by `killer` (None = the
    process-tree stop) on any waiter's try, unless the hold engine froze it."""
    if berths <= 0:
        return HeavyBerth(None, clock)
    max_wait_s = _max_wait_seconds(max_wait_minutes)
    since = clock()
    waiter = {
        "id": f"{time.time_ns()}-{uuid.uuid4().hex[:6]}", "repo": repo_identity.repo_name(repo), "run": run, "kind": "pc",
        "tests": int(tests), "priority": bool(priority), "since": since, "pid": pid or os.getpid(),
        "started": _stamp(since), "root": str(repo),
        "out_path": str(out_path) if out_path is not None else None,
    }
    rank = _rank_label(waiter["repo"])
    line = (f"waits for the machine: {waiter['repo']} runs {waiter['tests']} tests since {_hhmm(since)}"
            + (f" (order: {rank})" if rank is not None else ""))
    return _wait_and_take(waiter, berths, lambda now: line, on_wait, poll, sleep, clock, is_live, max_wait_s,
                          _silence(silent_max_minutes, killer))


def _wait_and_take(waiter, limit, wait_line, on_wait, poll, sleep, clock, is_live, max_wait_s,
                   silence: _Silence | None = None) -> HeavyBerth:
    """The one wait loop of both kinds: join the line, try every `poll` seconds, say `wait_line(now)` once after the
    first failed try. A wait that raises (or is interrupted) leaves no waiter behind."""
    berth = None
    announced = False
    try:
        with _locked():
            write_json(_waiter_path(waiter["id"]), waiter)
        while True:
            with _locked():
                now = clock()
                took = _try_take(waiter, limit, is_live, now, max_wait_s, silence)
                line = wait_line(now) if not took and not announced and on_wait else None
            if took:
                berth = HeavyBerth(took, clock)
                return berth
            if not announced:
                announced = True
                if on_wait:
                    on_wait(line)
            sleep(poll)
    finally:
        if berth is None:  # an error or an interrupt, even one right after the take: leave no waiter, no holder
            with _locked():
                _waiter_path(waiter["id"]).unlink(missing_ok=True)
            HeavyBerth(waiter, clock).release()


def acquire_machines(
    repo,
    run: str,
    want: int | None = None,
    *,
    total: int = core_settings.DEFAULTS["github_machines_total"],
    per_merge: int = core_settings.DEFAULTS["github_machines_per_merge"],
    priority: bool = False,
    on_wait: Callable[[str], None] | None = None,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    is_live: Liveness = holder_is_live,
    pid: int | None = None,
    max_wait_minutes: float | None = None,
) -> HeavyBerth:
    """Block until one merge holds GitHub machines, polling every `poll` seconds; the berth's `machines` says how
    many. It wants `min(want, per_merge)` (`want` None = `per_merge`) and takes `min(wanted, free)` machines of
    `total`, but only as the head of the github waiters in the one line; with 0 free it waits. While
    waiting, `on_wait` is called once with `waits for GitHub machines: <repo> wants <N> since HH:MM, <who>`. `total`
    <= 0 returns at once, records nothing and holds `machines` = the number wanted. A wait that raises (or is interrupted) leaves no waiter behind."""
    wanted = per_merge if want is None else min(int(want), per_merge)
    if total <= 0 or wanted <= 0:
        return HeavyBerth(None, clock, machines=max(wanted, 0))
    max_wait_s = _max_wait_seconds(max_wait_minutes)
    since = clock()
    waiter = {
        "id": f"{time.time_ns()}-{uuid.uuid4().hex[:6]}", "repo": repo_identity.repo_name(repo), "run": run, "kind": "github",
        "tests": 0, "want": wanted, "priority": bool(priority), "since": since, "pid": pid or os.getpid(),
        "started": _stamp(since),
    }

    def wait_line(now: float) -> str:
        rank = _rank_label(waiter["repo"])
        return (
            f"waits for GitHub machines: {waiter['repo']} wants {wanted} since {_hhmm(since)}, "
            f"{_blockers(waiter, total, is_live, now, max_wait_s)}" + (f" (order: {rank})" if rank is not None else "")
        )

    # #249: a GitHub waiter's try also ends a silent PC holder; the GitHub rows themselves are never judged
    return _wait_and_take(waiter, total, wait_line, on_wait, poll, sleep, clock, is_live, max_wait_s,
                          _silence(None, None))


def release_machines(berth: HeavyBerth) -> None:
    """Free the machines a merge holds (once); the same as leaving the berth's `with` block."""
    berth.release()


def holders() -> list[dict]:
    """The runs holding a berth now (every holder file, alive or not), oldest file name first."""
    return [record for _, record in _records("holder")]


def waiting_rows(
    is_live: Liveness = holder_is_live,
    now: float | None = None,
    max_wait_minutes: float | None = None,
) -> list[dict]:
    """The runs waiting for a place now, head of the line first (for `merge status`). A waiter whose process
    is dead is left out. A merge waiting for GitHub machines also carries `kind` `github` and `machines` (wanted).
    A row carries `"order"` (#233 rule 4: `"<place> of <N>"`) when an order is set; with no order the key is absent
    (read it with `.get`)."""
    at = time.time() if now is None else now
    max_wait_s = _max_wait_seconds(max_wait_minutes)
    rows = []
    for _, r in _order([w for w in _records("waiter") if _live(w[1], is_live)], at, max_wait_s):
        row = {
            "repo": r["repo"], "run": r["run"], "tests": r["tests"],
            "since": _hhmm(r["since"]), "priority": bool(r.get("priority")),
        }
        if _kind(r) == "github":
            row.update(kind="github", machines=int(r.get("want") or 0))
        label = _rank_label(r["repo"])
        if label is not None:
            row["order"] = label
        rows.append(row)
    return rows


def machines_in_use(is_live: Liveness = holder_is_live) -> int:
    """The GitHub machines held now across every repo (for the monitor and `merge status`); a holder whose process
    is dead is left out."""
    return _machines_of([r for _, r in _records("holder") if _live(r, is_live)])


def _replay(log: Path | None, kind: str) -> int:
    """The most held at one time by holders of `kind`, replayed from the take and free lines of the log."""
    held: dict[str, int] = {}
    most = 0
    try:
        text = Path(log if log is not None else log_path()).read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if _kind(row) != kind:
            continue
        if row.get("action") == "take":
            held[row.get("id")] = int(row.get("machines") or 1) if kind == "github" else 1
            most = max(most, sum(held.values()))
        elif row.get("action") == "free":
            held.pop(row.get("id"), None)
    return most


def max_simultaneous_holders(log: Path | None = None) -> int:
    """The most PC-berth holders at one time, replayed from the take and free lines of the log (0 when it has none)."""
    return _replay(log, "pc")


def max_simultaneous_machines(log: Path | None = None) -> int:
    """The most GitHub machines held at one time, replayed from the log only (0 when it has none)."""
    return _replay(log, "github")
