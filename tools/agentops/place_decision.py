"""#352 unit 2: where one test run goes, GitHub or the PC. ``decide_place(ids, settings, runner=, now=, deps=)`` ->
``TestPlace(where, reason, pc_seconds, github_seconds, pc_ids)``.

Rule, in this order (the first that applies decides):

1. the settings' ``github_suite`` is off, or ``merge_github.plan_for`` gives no plan (its minutes cap, ADR 0119, is
   checked there) -> ``pc``; nothing else is read for such a run;
2. the ids that need the PC (``Deps.needs_pc``) are split off into ``pc_ids``; the rest is what is decided; an empty
   rest -> ``pc``; a ``needs_pc`` that raises -> ``pc`` with every id in ``pc_ids``;
3. the expected PC run time of the rest is unknown -> ``pc``;
4. that time is below the floor, the larger of the setting ``place_pc_only_floor_seconds`` and the runner's start
   seconds -> ``pc`` (a machine start would not pay, and a run shorter than the start never goes to GitHub);
5. ``github_total = github_start_seconds_<runner> + pc_run * github_speed_ratio`` against
   ``pc_total = wait in the PC line + pc_run``: ``github`` when ``github_total < pc_total``, else ``pc`` (a tie -> pc);
6. ``pc_ids`` come back once the split is made (rule 2 on): on ``github`` they run on the PC beside it (ADR 0119,
   #341); a run rule 1 sent to the PC stops before the split and has ``pc_ids`` = [].

``pc_seconds`` is the expected finish on the PC as far as the rule got (the run alone when it stopped at rule 4, the
wait plus the run at rule 5), ``github_seconds`` the expected finish on GitHub; None where the rule stopped before it
was computed. A read that failed or a setting that is invalid is said in the reason as a short suffix. ``reason`` names the term that decided, durations written for a human.

Only settings decide, never a repo's name, a role, a path or a test name. ``Deps`` holds the four injectable
callables; a field left None takes its default, read from disk under ``root`` (default: the current folder).
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

START_KEY = "github_start_seconds_{}"
START_DEFAULTS = {"linux": 16.0, "windows": 26.0}  # the measured medians of core_settings DEFAULTS (#352 unit 1)
FLOOR_KEY, FLOOR_DEFAULT = "place_pc_only_floor_seconds", 120.0
RATIO_KEY, RATIO_DEFAULT = "github_speed_ratio", 1.0


@dataclass
class TestPlace:
    __test__ = False  # not a pytest class, whatever its name starts with

    where: str   # "github" or "pc"
    reason: str
    pc_seconds: float | None  # None: the rule stopped before it was computed
    github_seconds: float | None
    pc_ids: list[str] = field(default_factory=list)


@dataclass
class Deps:
    """The injectable reads of one decision; None = the default (``_defaults``)."""
    plan_for: Callable[[], tuple] | None = None                    # () -> (Plan | None, note)
    pc_run_seconds: Callable[[list[str]], float | None] | None = None  # ids -> seconds on the PC, None = unknown
    pc_wait_seconds: Callable[[float], float] | None = None        # now -> seconds in the PC line before this run
    needs_pc: Callable[[list[str]], list[str]] | None = None       # ids -> the ids that must stay on the PC


def human(seconds: float) -> str:
    """Core principle 7: ``40 s``, ``3 min 20 s`` (under 10 min), ``28 min``, ``1 h 47 min`` (past 90 min)."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    if minutes >= 90:
        return f"{minutes // 60} h {minutes % 60:02d} min"
    if seconds < 600:
        whole, rest = divmod(round(seconds), 60)
        return f"{whole} min" + (f" {rest} s" if rest else "")
    return f"{minutes} min"


class PcIds(list):
    """The ids ``Deps.needs_pc`` names, with ``note``: what could not be read on the way ("" = all read)."""

    def __init__(self, ids=(), note: str = ""):
        super().__init__(ids)
        self.note = note


def _number(settings: dict, key: str, default: float, notes: list[str]) -> float:
    """The setting as a non-negative number; a present but invalid one is the default, said in ``notes``."""
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        notes.append(f"setting {key} invalid, default used")
        return default
    return float(value)


def line_wait_seconds(holders: list[dict], waiters: list[dict], per_test: float, now: float, berths: int = 1) -> float:
    """The PC line's wait for one more run: each holder's remaining time (its ``tests`` x ``per_test`` minus the time
    since its take, ``taken``, never below 0) plus each waiter's whole expected run, divided by the ``berths`` the
    line has (0 berths = no limit: no wait). Pure."""
    if berths < 1:
        return 0.0
    remaining = sum(max(0.0, int(h.get("tests") or 0) * per_test - (now - float(h.get("taken") or now))) for h in holders)
    return (remaining + sum(int(w.get("tests") or 0) * per_test for w in waiters)) / berths


def _berths(settings: dict) -> int:
    """The setting ``machine_heavy_runs`` (default 1); anything but an int is the default."""
    value = settings.get("machine_heavy_runs", 1)
    return 1 if isinstance(value, bool) or not isinstance(value, int) else value


# ---- default reads (disk); every test of the rule injects its own ----

def _stored(root: Path) -> dict[str, float]:
    from .suite_plan_plugin import durations_source, stored_durations  # imports pytest: only when read
    stored = stored_durations(durations_source())
    return stored or stored_durations(str(root / ".testmondata"))


def _expand(ids: list[str], stored: dict[str, float]) -> list[str]:
    """A bare file or folder id stands for the stored tests under it; an id with none stays one test."""
    found: list[str] = []
    for i in ids:
        if i in stored:
            found.append(i)
            continue
        prefix = i.replace("\\", "/").rstrip("/")
        under = [k for k in stored if k.startswith(prefix + "::") or k.startswith(prefix + "/")]
        found.extend(under or [i])
    return found


def default_run_seconds(root: Path) -> Callable[[list[str]], float | None]:
    def run(ids: list[str]) -> float | None:
        from .suite_plan_plugin import plan_seconds
        stored = _stored(root)
        seconds = plan_seconds(_expand(list(ids), stored), stored)
        return None if seconds is None else float(sum(seconds))
    return run


def _default_wait_seconds(root: Path, settings: dict) -> Callable[[float], float]:
    def wait(now: float) -> float:
        from . import machine_berths
        stored = _stored(root)
        if not stored:
            raise ValueError("no stored test durations")
        per_test = statistics.median(stored.values())
        live = [r for r in machine_berths.holders()
                if r.get("kind") != "github" and machine_berths.holder_is_live(int(r.get("pid") or 0), r.get("started") or "")]
        waiting = [r for r in machine_berths.waiting_rows(now=now) if r.get("kind") != "github"]
        return line_wait_seconds(live, waiting, per_test, now, _berths(settings))
    return wait


def _default_needs_pc(root: Path) -> Callable[[list[str]], list[str]]:
    def needs(ids: list[str]) -> PcIds:
        from .pc_only import load_pc_only
        listed = load_pc_only(root)  # a list that exists and cannot be read raises: the caller says it
        kept_out: set[str] = set()
        readers: set[str] = set()
        note = ""
        try:
            from .test_copy_check import snapshot_files
            from .test_copy_plan import load_reads, pc_only_tests, tracked_files
            from .test_select import map_dir
            copied, tracked = set(snapshot_files(root)), tracked_files(root)
            kept_out = tracked - copied
            reads_dir = map_dir(root) / "reads"
            if reads_dir.is_dir():
                reads, dir_reads = load_reads(reads_dir)
                readers = set(pc_only_tests(reads, copied, dir_reads, tracked))
        except Exception as exc:  # noqa: BLE001 -- the public copy's rules unreadable: pc_only list alone, said
            note = f"test-copy rules unreadable: {exc}"
        named = [i for i in ids if i.split("::", 1)[0].replace("\\", "/") in listed | kept_out or i in readers]
        return PcIds(named, note)
    return needs


def _defaults(deps: Deps, settings: dict, root: Path | None) -> Deps:
    from . import merge_github
    base = Path(root) if root is not None else Path.cwd()
    return Deps(
        plan_for=deps.plan_for or (lambda: merge_github.plan_for(settings, base, None)),
        pc_run_seconds=deps.pc_run_seconds or default_run_seconds(base),
        pc_wait_seconds=deps.pc_wait_seconds or _default_wait_seconds(base, settings),
        needs_pc=deps.needs_pc or _default_needs_pc(base),
    )


def decide_place(ids: list[str], settings: dict, *, runner: str = "linux", now: float,
                 deps: Deps | None = None, root=None) -> TestPlace:
    """Where the run of ``ids`` goes (the rule is in the module doc). Raises ``ValueError`` for a ``runner`` that is
    not ``linux`` or ``windows``; every read that fails is said in the reason, never raised."""
    if runner not in START_DEFAULTS:
        raise ValueError(f"unknown runner {runner!r}: one of {', '.join(START_DEFAULTS)}")
    ids = list(ids)
    d = _defaults(deps or Deps(), settings, root)
    if settings.get("github_suite") is not True:
        return TestPlace("pc", "github_suite off", None, None, [])
    try:
        plan, note = d.plan_for()
    except Exception as exc:  # noqa: BLE001 -- unreadable GitHub settings = no GitHub
        return TestPlace("pc", f"github settings unreadable: {exc}", None, None, [])
    if plan is None:
        return TestPlace("pc", f"GitHub over the minutes cap: {note}" if note else "no GitHub plan", None, None, [])
    try:
        pc_ids = d.needs_pc(ids)
    except Exception as exc:  # noqa: BLE001 -- a PC-only list that cannot be read: the whole run stays on the PC
        return TestPlace("pc", f"PC-only list unreadable: {exc}", None, None, ids)
    notes = [pc_ids.note] if getattr(pc_ids, "note", "") else []
    pc_ids = list(pc_ids)

    def said(reason: str) -> str:
        return "; ".join([reason, *notes])

    def pc(reason: str, pc_s: float | None = None, gh_s: float | None = None) -> TestPlace:
        return TestPlace("pc", said(reason), pc_s, gh_s, pc_ids)

    rest = [i for i in ids if i not in set(pc_ids)]
    if not rest:
        return pc("every id needs the PC" if ids else "no ids")
    run = d.pc_run_seconds(rest)
    if run is None:
        return pc("no duration data: PC")
    start = _number(settings, START_KEY.format(runner), START_DEFAULTS[runner], notes)
    floor = _number(settings, FLOOR_KEY, FLOOR_DEFAULT, notes)
    if run < max(floor, start):
        term = f"floor {human(floor)}" if floor >= start else f"GitHub start {human(start)}"
        return pc(f"run {human(run)} < {term}", run)
    try:
        wait, unread = max(0.0, float(d.pc_wait_seconds(now))), ""
    except Exception:  # noqa: BLE001 -- a line that cannot be read counts 0, said in the reason
        wait, unread = 0.0, ", PC line unreadable, wait counted 0"
    github_total = start + run * _number(settings, RATIO_KEY, RATIO_DEFAULT, notes)
    pc_total = wait + run
    terms = f"(wait {human(wait)} + run {human(run)}{unread})"
    if github_total < pc_total:
        return TestPlace("github", said(f"GitHub {human(github_total)} < PC {human(pc_total)} {terms}"),
                         pc_total, github_total, pc_ids)
    return pc(f"PC {human(pc_total)} <= GitHub {human(github_total)} {terms}", pc_total, github_total)
