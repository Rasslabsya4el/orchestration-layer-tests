"""#343 unit 2 (rules 1, 3, 4): where one heavy run of the merge flow goes: the map build, the prequeue full suite,
the red-on-base run. ``decide(kind, settings, now=, root=, deps=)`` -> ``Place(where, reason, line, pending)``.

``where``:
- ``github`` when the settings' ``github_suite`` is on, ``merge_github.plan_for`` gives a plan (its minutes cap,
  ADR 0119 ``github_minutes``, is checked there) and the run is not coalesced.
- ``pc`` with the reason: ``github_suite off``, ``no plan``, plan_for's own note (the merge suite's wording,
  merge.py:585, e.g. the own-runners minutes spent), or ``github settings unreadable`` (merge.py:581-582; decide
  never raises on settings).
- ``skip`` with ``coalesced``: a map build within ``map_build_min_interval_minutes`` of the last build's start. Nothing
  runs now; the newest main's sha is left with ``note_pending`` and ``pending`` says so on the Place.

``line`` is the one log line naming the place, in exactly one of three forms: ``<kind> on github``,
``<kind> on pc (<reason>)``, ``<kind> on skip (<reason>)``. Only ``github`` and ``pc`` lines count as runs.

#352 unit 3: ``decide(kind, ..., ids=[...])`` for ``prequeue`` and ``red-on-base`` (and ``decide_selection`` for the
merge's own suite) decides with the selection: the whole decision is ``place_decision.decide_place`` (its rule is in that
module), so a selection whose expected PC run is below the floor stays on the PC. Where the selection's durations are
unknown, a heavy run of the merge flow keeps the GitHub-first rule above (an unmeasurable run is not a short one), said in
the reason. ``ids=None`` is the decision above exactly; the map build never takes ids (an hour-long build always goes to
GitHub). ``Place.pc_ids`` are the ids that need the PC; on ``github`` they run on the PC beside it.

Only settings decide, never a repo's name, path or test name: a target's settings give the same routing.
``Deps`` holds the injectable callables: ``plan_for`` (default ``merge_github.plan_for``, which reads the minutes
through ``github_minutes``) and ``last_build_start`` (default: the marker the map build leaves in its folder).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import core_settings, merge_github, place_decision
from .test_select import map_dir

KINDS = ("map-build", "prequeue", "red-on-base")
INTERVAL_KEY = "map_build_min_interval_minutes"
BUILD_START = "build_started.json"
PENDING = "pending_build.json"


@dataclass(frozen=True)
class Place:
    where: str   # "github", "pc", or "skip" (a coalesced map build: nothing runs now)
    reason: str  # "" on github; else github_suite off | no plan | plan_for's note | github settings unreadable | coalesced
    line: str    # the one log line naming the place
    pending: bool = False  # a newer main's sha waits for the next map build (note_pending)
    plan: object | None = None  # the merge's ``merge_github.Plan`` on github; None otherwise
    repo_name: str = ""  # on github: the Plan's repo name (the folder), the key github_minutes counts a repo's runs by
    pc_ids: list = field(default_factory=list)  # #352: with ids given, the ids that need the PC (run there beside github)


def _utc(when: datetime) -> datetime:
    """A naive time is taken as UTC, an aware one is converted to UTC."""
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def _read_json(path: Path, key: str):
    """``path``'s JSON object's ``key``, or None when the file, the key or the JSON is missing."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))[key]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_json(path: Path, key: str, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: value}), encoding="utf-8")


def read_build_start(root) -> datetime | None:
    """When the newest map build started (UTC), or None when no build was noted under ``root``. Never raises."""
    if root is None:
        return None
    started = _read_json(map_dir(root) / BUILD_START, "started")
    try:
        return None if started is None else _utc(datetime.fromisoformat(started))
    except (ValueError, TypeError):
        return None


def note_build_start(root, when: datetime) -> None:
    """A map build starts: its start time (UTC, ISO) goes in the map folder for the coalescing check, and the pending
    sha (if any) is taken by this build, so the marker is cleared."""
    mdir = map_dir(root)
    _write_json(mdir / BUILD_START, "started", _utc(when).isoformat())
    (mdir / PENDING).unlink(missing_ok=True)


def note_pending(root, sha: str) -> None:
    """A map build was coalesced: the newest main's sha is kept for the next map build ("newest main wins")."""
    _write_json(map_dir(root) / PENDING, "sha", sha)


def pending(root) -> str | None:
    """The sha a coalesced map build left for the next one, or None. Never raises."""
    if root is None:
        return None
    return _read_json(map_dir(root) / PENDING, "sha")


@dataclass(frozen=True)
class Deps:
    plan_for: Callable = merge_github.plan_for
    last_build_start: Callable = read_build_start
    place: place_decision.Deps = field(default_factory=place_decision.Deps)  # #352: the reads of a decision with ids


def _place(where: str, kind: str, reason: str, waiting: bool, repo_name: str = "") -> Place:
    """The one Place of a decision: the line in its one of three forms."""
    line = f"{kind} on github" if where == "github" else f"{kind} on {where} ({reason})"
    return Place(where, reason, line, waiting, repo_name=repo_name)


def seconds_until_due(settings: dict, *, now: datetime | None = None, root=None, deps: Deps | None = None) -> float:
    """Seconds until a map build stops being coalesced: the last build's start plus ``map_build_min_interval_minutes``
    minus ``now``; 0 when no build was noted, the interval has passed, or the setting is unreadable (the default
    interval is used then)."""
    deps = deps or Deps()
    at = _utc(now) if now is not None else datetime.now(timezone.utc)
    last = deps.last_build_start(root)
    if last is None:
        return 0.0
    minutes = settings.get(INTERVAL_KEY, core_settings.DEFAULTS[INTERVAL_KEY])
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        minutes = core_settings.DEFAULTS[INTERVAL_KEY]
    return max(0.0, (_utc(last) + timedelta(minutes=minutes) - at).total_seconds())


def decide_selection(kind: str, ids: list[str], settings: dict | None, *, now: datetime | None = None, root=None,
                     deps: Deps | None = None) -> Place:
    """#352: the place of the run of ``ids`` (``kind`` only names the line): ``place_decision.decide_place`` with
    ``deps.plan_for`` as its plan read, mapped to a ``Place`` (the plan and repo name on ``github``, ``pc_ids`` always).
    Durations unknown for a run that would otherwise be timed: ``github`` as for ``ids=None``, said in the reason."""
    deps = deps or Deps()
    at = _utc(now) if now is not None else datetime.now(timezone.utc)
    if settings is None:
        return _place("pc", kind, "github settings unreadable", False)
    box: dict = {}

    def plan_for():
        box["plan"], note = deps.plan_for(settings, root, at)
        return box["plan"], note

    base_seconds = deps.place.pc_run_seconds or place_decision.default_run_seconds(Path(root) if root is not None else Path.cwd())

    def run_seconds(rest):
        seconds = base_seconds(rest)
        box["unknown"] = seconds is None
        return seconds

    decided = place_decision.decide_place(
        list(ids), settings, now=at.timestamp(), root=root,
        deps=replace(deps.place, plan_for=plan_for, pc_run_seconds=run_seconds))
    plan = box.get("plan")
    if box.get("unknown") and decided.where == "pc" and plan is not None:
        return replace(_place("github", kind, "", False, plan.repo_name), plan=plan, pc_ids=list(decided.pc_ids),
                       reason="no duration data: GitHub as for a run it cannot time")
    if decided.where == "github":
        return replace(_place("github", kind, "", False, plan.repo_name), plan=plan, pc_ids=list(decided.pc_ids))
    return replace(_place("pc", kind, decided.reason, False), pc_ids=list(decided.pc_ids))


def decide(kind: str, settings: dict, *, now: datetime | None = None, root=None, deps: Deps | None = None,
           ids: list[str] | None = None) -> Place:
    if kind not in KINDS:
        raise ValueError(f"unknown heavy run {kind!r}: one of {', '.join(KINDS)}")
    if root is None:
        raise ValueError("decide needs the repo root: its map folder and its plan")
    deps = deps or Deps()
    if ids is not None and kind != "map-build":
        return decide_selection(kind, ids, settings, now=now, root=root, deps=deps)
    at = _utc(now) if now is not None else datetime.now(timezone.utc)
    waiting = kind == "map-build" and pending(root) is not None
    try:
        if settings.get("github_suite") is not True:
            return _place("pc", kind, "github_suite off", waiting)
        plan, note = deps.plan_for(settings, root, at)
        interval = timedelta(minutes=settings.get(INTERVAL_KEY, core_settings.DEFAULTS[INTERVAL_KEY]))
    except Exception:  # noqa: BLE001 - an unreadable setting is "off", said in the log (merge.py:581-582)
        return _place("pc", kind, "github settings unreadable", waiting)
    if plan is None:
        return _place("pc", kind, note or "no plan", waiting)
    if kind == "map-build":
        last = deps.last_build_start(root)
        if last is not None and at - _utc(last) < interval:
            return _place("skip", kind, "coalesced", waiting)
    return replace(_place("github", kind, "", waiting, plan.repo_name), plan=plan)


def read_settings(load: Callable | None = None) -> dict | None:
    """The settings as the merge reads them (``core_settings.load``, looked up now, or ``load``); None when unreadable,
    which ``decide`` says in its line. Never raises."""
    try:
        return (load or core_settings.load)()
    except Exception:  # noqa: BLE001 - an unreadable setting is "off", said in the line
        return None
