"""#343 unit 2 (rules 1, 3, 4): one decision for the three heavy runs of the merge flow (map-build, prequeue,
red-on-base): GitHub when the repo's ``github_suite`` is on, ``merge_github.plan_for`` gives a plan and the minutes cap
allows; else the PC with plan_for's own note as the reason (the merge suite's wording, merge.py:585). A map build that
a newer one follows within ``map_build_min_interval_minutes`` is ``skip`` (coalesced); the newest main's sha is then
``pending`` until a build starts. Seam: ``heavy_place.decide`` with the injected plan (or the real ``plan_for`` with the
minutes reader monkeypatched), the clock (``now``), the root and the settings dict; no repo name is in the code under test.
The log line is exactly ``<kind> on github``, ``<kind> on pc (<reason>)`` or ``<kind> on skip (<reason>)``."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agentops import core_settings, github_minutes, heavy_place, merge_github

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
PLAN = merge_github.Plan(remote="https://github.com/o/t.git", own_repo=False, repo_slug="o/r", repo_name="r")
ON = {**core_settings.DEFAULTS, "github_suite": True, "repo": "o/r", "test_copy_remote": "https://github.com/o/t.git"}
OFF = {**core_settings.DEFAULTS, "github_suite": False, "repo": "o/r"}
CAP_NOTE = "own-runners repo over free minutes: 1900 used + median 300 > 2000 until 2026-11-01"
SHA = "c" * 40


def stub(plan, note=""):
    """A ``plan_for`` that answers the same whatever the settings: the injected plan."""
    return lambda settings, root, now=None: (plan, note)


def raising(settings, root, now=None):
    raise RuntimeError("settings file unreadable")


def deps(plan, note="", **kw):
    return heavy_place.Deps(plan_for=stub(plan, note), **kw)


# -- routing: github on and cap ok -> github; every other case -> the PC (or skip) with its reason ----------------------

@pytest.mark.parametrize("kind", ["map-build", "prequeue", "red-on-base"])
def test_github_on_and_cap_ok_sends_all_three_kinds_to_github(tmp_path, kind):
    place = heavy_place.decide(kind, ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.reason, place.pending) == ("github", "", False)
    assert place.line == f"{kind} on github"


def test_suite_off_goes_to_the_pc_with_reason_github_suite_off(tmp_path):
    place = heavy_place.decide("prequeue", OFF, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.reason) == ("pc", "github_suite off")
    assert place.line == "prequeue on pc (github_suite off)"


def test_no_plan_goes_to_the_pc_with_reason_no_plan(tmp_path):
    place = heavy_place.decide("red-on-base", ON, now=NOW, root=tmp_path, deps=deps(None))
    assert (place.where, place.reason) == ("pc", "no plan")
    assert place.line == "red-on-base on pc (no plan)"


def test_plan_refused_by_the_minutes_cap_gives_plan_for_note_as_the_reason(tmp_path):
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(None, CAP_NOTE))
    assert (place.where, place.reason) == ("pc", CAP_NOTE)
    assert place.line == f"map-build on pc ({CAP_NOTE})"


def test_unreadable_settings_go_to_the_pc_with_reason_github_settings_unreadable(tmp_path):
    place = heavy_place.decide("prequeue", None, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.reason) == ("pc", "github settings unreadable")
    assert place.line == "prequeue on pc (github settings unreadable)"


def test_a_plan_for_that_raises_goes_to_the_pc_and_decide_does_not_raise(tmp_path):
    place = heavy_place.decide("red-on-base", ON, now=NOW, root=tmp_path, deps=heavy_place.Deps(plan_for=raising))
    assert (place.where, place.reason) == ("pc", "github settings unreadable")


def test_root_none_is_refused_before_plan_for_is_called(tmp_path):
    seen = []

    def spy(settings, root, now=None):
        seen.append(root)
        return PLAN, ""

    with pytest.raises(ValueError):
        heavy_place.decide("prequeue", ON, now=NOW, root=None, deps=heavy_place.Deps(plan_for=spy))
    assert seen == []


# -- the real plan_for: its minutes reader is the injected one (github_minutes.over_budget) ------------------------------

OWN = {**core_settings.DEFAULTS, "github_suite": True, "github_own_runners": True, "test_copy_remote": "",
       "repo": "o/own"}


def test_real_plan_for_own_runners_under_the_cap_goes_to_github(tmp_path, monkeypatch):
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (False, ""))
    place = heavy_place.decide("prequeue", OWN, now=NOW, root=tmp_path)
    assert (place.where, place.reason) == ("github", "")


def test_real_plan_for_own_runners_over_the_cap_gives_the_plan_for_note(tmp_path, monkeypatch):
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (True, "1900 used + median 300 > 2000"))
    place = heavy_place.decide("prequeue", OWN, now=NOW, root=tmp_path)
    expected = "own-runners repo over free minutes: 1900 used + median 300 > 2000"
    assert (place.where, place.reason) == ("pc", expected)
    assert place.line == f"prequeue on pc ({expected})"


def test_a_repo_with_a_test_copy_has_no_minutes_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (True, "over"))
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path)
    assert (place.where, place.reason) == ("github", "")


# -- rule 4: coalescing of map builds -----------------------------------------------------------------------------------

def test_default_interval_is_thirty_minutes_in_defaults_and_the_settings_file():
    assert core_settings.DEFAULTS["map_build_min_interval_minutes"] == 30
    file = Path(__file__).resolve().parents[1] / "config" / "orchestration.json"
    assert json.loads(file.read_text(encoding="utf-8"))["map_build_min_interval_minutes"] == 30


def test_a_second_map_build_within_the_interval_is_skipped_as_coalesced(tmp_path):
    last = NOW - timedelta(minutes=10)
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(PLAN, last_build_start=lambda r: last))
    assert (place.where, place.reason) == ("skip", "coalesced")
    assert place.line == "map-build on skip (coalesced)"


def test_a_map_build_after_the_interval_runs_on_github(tmp_path):
    last = NOW - timedelta(minutes=45)
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(PLAN, last_build_start=lambda r: last))
    assert (place.where, place.reason) == ("github", "")


def test_the_interval_is_a_setting(tmp_path):
    last = NOW - timedelta(minutes=45)
    settings = {**ON, "map_build_min_interval_minutes": 60}
    place = heavy_place.decide("map-build", settings, now=NOW, root=tmp_path, deps=deps(PLAN, last_build_start=lambda r: last))
    assert (place.where, place.reason) == ("skip", "coalesced")


def test_coalescing_applies_to_map_builds_only(tmp_path):
    last = NOW - timedelta(minutes=10)
    for kind in ("prequeue", "red-on-base"):
        place = heavy_place.decide(kind, ON, now=NOW, root=tmp_path, deps=deps(PLAN, last_build_start=lambda r: last))
        assert (place.where, place.reason) == ("github", "")


def test_the_build_start_marker_round_trips_and_coalesces_by_default(tmp_path):
    heavy_place.note_build_start(tmp_path, NOW - timedelta(minutes=5))
    assert heavy_place.read_build_start(tmp_path) == NOW - timedelta(minutes=5)
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.reason) == ("skip", "coalesced")


def test_no_build_start_marker_means_no_coalescing(tmp_path):
    assert heavy_place.read_build_start(tmp_path) is None
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.reason) == ("github", "")


# -- pending: a coalesced build leaves the newest main's sha for the next map build ------------------------------------

def test_pending_round_trips_and_is_none_when_nothing_is_pending(tmp_path):
    assert heavy_place.pending(tmp_path) is None
    heavy_place.note_pending(tmp_path, SHA)
    assert heavy_place.pending(tmp_path) == SHA


def test_a_coalesced_build_with_a_pending_sha_reports_pending_then_github_and_cleared_by_the_build_start(tmp_path):
    heavy_place.note_build_start(tmp_path, NOW - timedelta(minutes=10))
    heavy_place.note_pending(tmp_path, SHA)  # the caller records the newest main sha it did not build
    skipped = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (skipped.where, skipped.reason, skipped.pending) == ("skip", "coalesced", True)

    later = NOW + timedelta(minutes=25)  # 35 min after the last build's start: past the 30-min interval
    due = heavy_place.decide("map-build", ON, now=later, root=tmp_path, deps=deps(PLAN))
    assert (due.where, due.pending) == ("github", True)
    assert due.line == "map-build on github"

    heavy_place.note_build_start(tmp_path, later)  # the pending sha's build starts: the marker is cleared
    assert heavy_place.pending(tmp_path) is None
    after = heavy_place.decide("map-build", ON, now=later + timedelta(minutes=1), root=tmp_path, deps=deps(PLAN))
    assert (after.where, after.reason, after.pending) == ("skip", "coalesced", False)  # the build just started


def test_pending_is_reported_for_map_builds_only(tmp_path):
    heavy_place.note_pending(tmp_path, SHA)
    place = heavy_place.decide("prequeue", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    assert (place.where, place.pending) == ("github", False)


# -- settings only: another repo's settings dict routes the same way -----------------------------------------------------

def test_another_repos_settings_give_the_same_routing(tmp_path):
    other = {**ON, "repo": "acme/other-app", "test_copy_remote": "https://github.com/acme/other-tests.git"}
    other_plan = merge_github.Plan(remote=other["test_copy_remote"], own_repo=False, repo_slug="acme/other-app",
                                   repo_name="other-app")
    ours = heavy_place.decide("prequeue", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
    theirs = heavy_place.decide("prequeue", other, now=NOW, root=tmp_path / "other-app", deps=deps(other_plan))
    assert (ours.where, ours.reason, ours.line) == (theirs.where, theirs.reason, theirs.line) == (
        "github", "", "prequeue on github")


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError):
        heavy_place.decide("merge-suite", ON, now=NOW, root=tmp_path, deps=deps(PLAN))
