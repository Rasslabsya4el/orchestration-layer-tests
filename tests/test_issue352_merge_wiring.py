"""#352 unit 3 (merge-flow wiring): the place decision of unit 2 (``place_decision.decide_place``) reaches the heavy runs
of the merge flow, and a GitHub run that ends red without a named test never sends the whole run to the PC.

A. ``heavy_place.decide(..., ids=)`` (prequeue, red-on-base) and ``merge._run_suite_placed`` decide with the selection:
   a selection below the floor stays on the PC; a selection whose durations are unknown keeps the GitHub-first rule of
   #343 (a heavy merge-flow run is never sent to the PC for what cannot be measured); the map build keeps its rule.
B. Row 8: a red GitHub run with ids it could not collect (``ids sent but not collected``) runs only those ids on the PC
   (``_run_ids_on_pc``, as #341) and the rest again on GitHub, at the same time; a red with no named test and no
   uncollected id is re-run on GitHub once, then the PC takes the run as before.

Seams: ``heavy_place.decide`` with injected deps, ``red_on_base._reds`` with the seams of #343, ``run_merge`` with the
fakes of test_issue247_routing (a GitHub that answers from a queue; the spy on ``run_github_suite``)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tests.test_issue232_wiring import BAD, World, red
from tests.test_issue247_routing import GREEN, QFakes, ids_of, select_ids
from tools.agentops import core_settings, github_suite, heavy_place, machine_berths, merge as merge_module
from tools.agentops import merge_github, place_decision, red_on_base

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
PLAN = merge_github.Plan(remote="https://github.com/o/t.git", own_repo=False, repo_slug="o/r", repo_name="r")
ON = {**core_settings.DEFAULTS, "github_suite": True, "repo": "o/r", "test_copy_remote": PLAN.remote}
IDS = ids_of(10)


def place_deps(run, wait=0.0, pc_ids=()):
    return place_decision.Deps(pc_run_seconds=lambda ids: run, pc_wait_seconds=lambda now: wait,
                               needs_pc=lambda ids: [i for i in ids if i in pc_ids])


def hp_deps(run, wait=0.0, pc_ids=(), plan=PLAN):
    return heavy_place.Deps(plan_for=lambda settings, root, now=None: (plan, ""),
                            place=place_deps(run, wait, pc_ids))


# -- A: heavy_place.decide with the selection -----------------------------------------------------------------------

def test_a_selection_below_the_floor_stays_on_the_pc_for_prequeue_and_red_on_base(tmp_path):
    for kind in ("prequeue", "red-on-base"):
        place = heavy_place.decide(kind, ON, now=NOW, root=tmp_path, deps=hp_deps(run=30.0), ids=IDS)
        assert place.where == "pc" and place.reason == "run 30 s < floor 2 min", place
        assert place.line == f"{kind} on pc (run 30 s < floor 2 min)"


def test_a_long_selection_on_a_busy_pc_goes_to_github_with_the_plan_and_the_pc_ids(tmp_path):
    place = heavy_place.decide("red-on-base", ON, now=NOW, root=tmp_path, ids=[*IDS, "tests/pcwin.py::t"],
                               deps=hp_deps(run=600.0, wait=300.0, pc_ids=["tests/pcwin.py::t"]))
    assert place.where == "github" and place.line == "red-on-base on github"
    assert place.plan is PLAN and place.repo_name == "r" and place.pc_ids == ["tests/pcwin.py::t"]


def test_a_long_selection_on_an_idle_pc_stays_on_the_pc(tmp_path):
    place = heavy_place.decide("prequeue", ON, now=NOW, root=tmp_path, deps=hp_deps(run=600.0, wait=0.0), ids=IDS)
    assert place.where == "pc" and place.reason.startswith("PC 10 min <= GitHub "), place.reason


def test_no_ids_is_todays_decision_whatever_the_size(tmp_path):
    place = heavy_place.decide("prequeue", ON, now=NOW, root=tmp_path, deps=hp_deps(run=1.0))
    assert (place.where, place.reason) == ("github", "")


def test_the_map_build_keeps_its_rule_with_ids_given(tmp_path):
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path, deps=hp_deps(run=1.0), ids=IDS)
    assert (place.where, place.reason) == ("github", "")


def test_unknown_durations_keep_the_github_first_rule_of_a_heavy_run(tmp_path):
    place = heavy_place.decide("red-on-base", ON, now=NOW, root=tmp_path, deps=hp_deps(run=None), ids=IDS)
    assert place.where == "github" and place.plan is PLAN and "no duration data" in place.reason


def test_settings_off_or_no_plan_say_their_reasons_with_ids_too(tmp_path):
    off = heavy_place.decide("prequeue", {**ON, "github_suite": False}, now=NOW, root=tmp_path,
                             deps=hp_deps(run=600.0), ids=IDS)
    assert (off.where, off.reason) == ("pc", "github_suite off")
    none = heavy_place.decide("prequeue", ON, now=NOW, root=tmp_path, deps=hp_deps(run=600.0, plan=None), ids=IDS)
    assert none.where == "pc" and none.reason
    unreadable = heavy_place.decide("prequeue", None, now=NOW, root=tmp_path, deps=hp_deps(run=600.0), ids=IDS)
    assert (unreadable.where, unreadable.reason) == ("pc", "github settings unreadable")


# -- A: red-on-base passes the ids it collected ---------------------------------------------------------------------

def test_red_on_base_keeps_a_small_collected_selection_on_the_pc(tmp_path, monkeypatch, capsys):
    ids = [f"tests/a.py::t{i}" for i in range(3)]
    monkeypatch.setattr(place_decision, "_stored", lambda root: {i: 1.0 for i in ids})
    github_calls = []
    seams = red_on_base.Seams(load_settings=lambda: ON, collect_ids=lambda cwd, tests, t: ids,
                              github_run=lambda *a, **kw: github_calls.append(1),
                              pc_reds=lambda cwd, tests, t: ["tests/a.py::t1"], pc_only_files=lambda cwd: [])
    reds = red_on_base._reds(tmp_path, "tests/a.py", 60, seams)
    assert reds == ["tests/a.py::t1"] and github_calls == []
    assert "red-on-base on pc (run 3 s < floor 2 min" in capsys.readouterr().out


# -- A: the merge's own place -----------------------------------------------------------------------------------------

def stored_for(monkeypatch, per_test, n=50):
    monkeypatch.setattr(place_decision, "_stored", lambda root: {i: per_test for i in ids_of(n)})
    monkeypatch.setattr(machine_berths, "holders", lambda: [])
    monkeypatch.setattr(machine_berths, "waiting_rows", lambda now=None: [])


def test_a_merge_whose_selection_is_below_the_floor_stays_on_the_pc(tmp_path, monkeypatch):
    stored_for(monkeypatch, 1.0)
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and w.calls == [] and len(w.pc_runs) == 1
    assert result.suite_place == "suite on the PC (run 50 s < floor 2 min)"
    assert any("suite on the PC (run 50 s < floor 2 min)" in x for x in w.log_lines())


def test_a_merge_whose_selection_pays_off_on_github_goes_there(tmp_path, monkeypatch):
    stored_for(monkeypatch, 6.0)  # 5 min on the PC against 16 s + half of it on GitHub
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    world_settings = core_settings.load()
    monkeypatch.setattr(core_settings, "load", lambda path=None: {**world_settings, "github_speed_ratio": 0.5})
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1 and w.calls[0]["ids"] == ids_of(50)


def test_a_merge_with_unknown_durations_keeps_the_github_first_path(tmp_path, monkeypatch):
    monkeypatch.setattr(place_decision, "_stored", lambda root: {})
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1


def test_a_full_merge_run_keeps_the_github_first_path(tmp_path, monkeypatch):
    stored_for(monkeypatch, 0.01)
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.calls[0]["ids"] is None


# -- B: the uncollected ids -------------------------------------------------------------------------------------------

NOT_COLLECTED_ID = ids_of(20)[3]
MESSAGE = f"ERROR: ids sent but not collected: 1 of 20: {NOT_COLLECTED_ID} ====\n"
RED_FULL = {"run_id": 82, "parts": [{"part": 1, "seconds": 9}], "minutes_used": 1, "outcome": "red-test",
            "failed_ids": [], "full_recheck": True}
THREE_PASSED = "...\n3 passed in 0.10s\n"


class LogFakes(QFakes):
    """QFakes whose failed-run log carries ``log``."""

    def __init__(self, *answers, log="", **kw):
        super().__init__(*answers, **kw)
        self.log = log

    def deps(self):
        deps = super().deps()
        deps.failed_log = lambda copy, run_id: self.log
        return deps


class UncollectedWorld(World):
    def __init__(self, tmp_path, monkeypatch, *, pc_result=(True, THREE_PASSED), **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        self.pc_ids_runs: list[list[str]] = []
        self.pc_result = pc_result

        def run_ids(suite_cmd, ids, suite_dir, run_env):
            self.pc_ids_runs.append(list(ids))
            return self.pc_result
        monkeypatch.setattr(merge_module, "_run_ids_on_pc", run_ids)


def test_the_log_parser_names_every_uncollected_id_and_keeps_commas_inside_brackets():
    log = "x\nERROR: ids sent but not collected: 3 of 9: tests/a.py::t[1, 2], tests/b.py, tests/c.py::u ====\n"
    assert github_suite.not_collected_ids(log) == ["tests/a.py::t[1, 2]", "tests/b.py", "tests/c.py::u"]
    assert github_suite.not_collected_ids("FAILED tests/y.py::q") == []


def test_a_red_run_carries_its_uncollected_ids(tmp_path, monkeypatch):
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=MESSAGE))
    select_ids(monkeypatch, ids_of(20))
    w.merge()
    assert w.pc_ids_runs == [[NOT_COLLECTED_ID]]


def test_row_8_one_uncollected_id_runs_on_the_pc_and_the_rest_again_on_github(tmp_path, monkeypatch):
    fakes = LogFakes(RED_FULL, GREEN, log=MESSAGE)
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and result.pushed
    assert w.pc_runs == []  # 0 whole-suite PC fallbacks
    assert w.pc_ids_runs == [[NOT_COLLECTED_ID]]  # exactly the uncollected id on the PC
    assert len(w.calls) == 2
    assert w.calls[0]["ids"] == ids_of(20)
    rest = [i for i in ids_of(20) if i != NOT_COLLECTED_ID]
    assert w.calls[1]["ids"] == rest and w.calls[1]["tests"] == 19
    assert "1 not collected on GitHub" in result.suite_place and "(+ 1 test on the PC)" in result.suite_place
    assert w.brain_lines() == []  # no whole-suite fallback, no brain line


def test_row_8_a_red_uncollected_id_on_the_pc_makes_the_merge_red_naming_it(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {NOT_COLLECTED_ID} - boom\n1 failed in 0.10s\n"
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=MESSAGE), pc_result=(False, out))
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and NOT_COLLECTED_ID in result.failed_tests
    assert w.pc_runs == [] and not result.pushed


def test_row_8_a_failed_test_on_the_second_github_run_is_named_and_rechecked_there(tmp_path, monkeypatch):
    fakes = LogFakes(RED_FULL, red(), GREEN, log=MESSAGE)
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and result.rechecked == [BAD]
    assert w.pc_runs == [] and w.rechecks == [] and len(w.calls) == 3 and w.pc_ids_runs == [[NOT_COLLECTED_ID]]


def test_row_8_every_selected_id_uncollected_leaves_nothing_for_github(tmp_path, monkeypatch):
    only = ids_of(20)[3]
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=MESSAGE.replace("of 20", "of 1")))
    select_ids(monkeypatch, [only])
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1 and w.pc_ids_runs == [[only]]


def test_row_8_the_second_run_still_red_without_a_test_falls_back_to_the_pc_as_today(tmp_path, monkeypatch):
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, RED_FULL, log=MESSAGE))
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and len(w.calls) == 2 and len(w.pc_runs) == 1
    assert len(w.brain_lines()) == 1


def test_row_8_a_red_with_no_test_and_no_uncollected_id_is_rerun_on_github_once(tmp_path, monkeypatch):
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log="no message here"))
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.pc_ids_runs == [] and len(w.calls) == 2
    assert w.calls[1]["ids"] == ids_of(20)
    assert "red with no test named, run again on GitHub" in result.suite_place


def test_row_8_a_whole_suite_problem_reruns_once_then_the_pc_takes_the_run(tmp_path, monkeypatch):
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, RED_FULL, log="no message here"))
    result = w.merge()  # a full run: no selection
    assert result.ok and len(w.calls) == 2 and len(w.pc_runs) == 1 and w.pc_ids_runs == []
    assert len(w.brain_lines()) == 1


def test_row_8_a_full_run_red_without_a_test_is_rerun_on_github_whole(tmp_path, monkeypatch):
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log="no message here"))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 2 and w.calls[1]["ids"] is None


def test_the_github_side_result_keeps_the_ids_for_a_red_with_a_message():
    deps = github_suite.Deps(
        send=lambda root, branch, remote: SimpleNamespace(sent=True, reason=None),
        acquire_machines=lambda *a, **k: SimpleNamespace(machines=2, release=lambda: None),
        dispatch=lambda *a, **k: 7,
        poll=lambda copy, run_id, **k: {"run_id": run_id, "parts": [], "minutes_used": 0, "outcome": "red-test",
                                        "failed_ids": [], "full_recheck": True},
        record=lambda record: None, failed_log=lambda copy, run_id: MESSAGE)
    result = github_suite.run_github_suite(root=None, branch="b", repo_slug="o/r", remote=PLAN.remote, repo_name="r",
                                           priority=False, settings=ON, deps=deps)
    assert result.not_collected == NOT_COLLECTED_ID and result.not_collected_ids == [NOT_COLLECTED_ID]


# -- C: send-back items (several messages, a count above the named ids, one run's numbers, own PC file, timeout) ------

def message(n, of, *ids):
    return f"ERROR: ids sent but not collected: {n} of {of}: {', '.join(ids)} ====\n"


def test_the_log_parser_reads_every_message_and_unions_the_ids_in_order():
    log = f"part 1\n{message(1, 9, 'tests/a.py::t1')}part 2\n{message(2, 9, 'tests/b.py', 'tests/a.py::t1')}"
    assert github_suite.not_collected_ids(log) == ["tests/a.py::t1", "tests/b.py"]
    assert github_suite.not_collected_count(log) == 3
    assert github_suite.not_collected_count("FAILED tests/y.py::q") == 0


def test_the_github_side_result_carries_the_count_of_the_messages():
    log = message(15, 20, NOT_COLLECTED_ID)
    deps = github_suite.Deps(
        send=lambda root, branch, remote: SimpleNamespace(sent=True, reason=None),
        acquire_machines=lambda *a, **k: SimpleNamespace(machines=2, release=lambda: None),
        dispatch=lambda *a, **k: 7,
        poll=lambda copy, run_id, **k: {"run_id": run_id, "parts": [], "minutes_used": 0, "outcome": "red-test",
                                        "failed_ids": [], "full_recheck": True},
        record=lambda record: None, failed_log=lambda copy, run_id: log)
    result = github_suite.run_github_suite(root=None, branch="b", repo_slug="o/r", remote=PLAN.remote, repo_name="r",
                                           priority=False, settings=ON, deps=deps)
    assert result.not_collected_count == 15 and result.not_collected_ids == [NOT_COLLECTED_ID]


def test_row_8_two_parts_each_losing_one_id_send_both_to_the_pc_and_the_rest_to_github(tmp_path, monkeypatch):
    ids = ids_of(20)
    log = message(1, 20, ids[3]) + message(1, 20, ids[7])
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=log))
    select_ids(monkeypatch, ids)
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.pc_ids_runs == [[ids[3], ids[7]]]
    assert w.calls[1]["ids"] == [i for i in ids if i not in (ids[3], ids[7])]
    assert len(w.calls) == 2 and w.brain_lines() == []


def test_row_8_a_count_above_the_named_ids_goes_straight_to_the_whole_run_on_the_pc(tmp_path, monkeypatch):
    log = message(15, 20, NOT_COLLECTED_ID)
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=log))
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and len(w.calls) == 1 and len(w.pc_runs) == 1 and w.pc_ids_runs == []
    assert "15 ids not collected, only 1 named" in result.suite_place
    assert "15 ids not collected, only 1 named" in w.brain_lines()[0]["line"]


def test_row_8_when_no_id_is_left_for_github_the_run_is_counted_once(tmp_path, monkeypatch):
    only = ids_of(20)[3]
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=message(1, 1, only)))
    select_ids(monkeypatch, [only])
    result = w.merge()
    assert result.ok and len(w.calls) == 1
    assert result.suite_minutes == RED_FULL["minutes_used"] == 1
    assert result.suite_part_seconds == [9]
    assert "none left for GitHub" in result.suite_place and result.suite_place.count("run 82") == 1


def test_row_8_the_pc_part_and_the_github_rerun_write_to_different_files(tmp_path, monkeypatch):
    paths = {"github": [], "pc": []}
    real_run = merge_github.run

    def run_spy(plan, **kw):
        paths["github"].append(kw["output_path"])
        return real_run(plan, **kw)

    def heavy_spy(cwd, name, tests_line, priority, body, output_path, *rest):
        paths["pc"].append(output_path)
        return body()
    monkeypatch.setattr(merge_github, "run", run_spy)
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=MESSAGE))
    monkeypatch.setattr(merge_module, "_run_heavy_suite", heavy_spy)
    select_ids(monkeypatch, ids_of(20))
    result = w.merge()
    assert result.ok and len(paths["github"]) == 2 and len(paths["pc"]) == 1
    assert set(paths["github"]) == {paths["github"][0]}
    assert paths["pc"][0] != paths["github"][0] and paths["pc"][0].name == paths["github"][0].name + ".pc"


def test_row_8_a_pc_part_that_outlasts_the_bound_makes_the_merge_red_and_says_so(tmp_path, monkeypatch):
    import threading
    w = UncollectedWorld(tmp_path, monkeypatch, fakes=LogFakes(RED_FULL, GREEN, log=MESSAGE))
    base = core_settings.load()
    monkeypatch.setattr(core_settings, "load", lambda path=None: {**base, "test_run_timeout_minutes": 0.005})
    release = threading.Event()

    def slow(suite_cmd, ids, suite_dir, run_env):
        release.wait(10)
        return True, THREE_PASSED
    monkeypatch.setattr(merge_module, "_run_ids_on_pc", slow)
    select_ids(monkeypatch, ids_of(20))
    try:
        result = w.merge()
    finally:
        release.set()
    assert not result.ok and result.reason == "red-suite" and NOT_COLLECTED_ID in result.failed_tests
    assert "timed out after 0.005 min" in result.suite_place and w.pc_runs == []


def test_a_failed_selection_decision_keeps_github_first_and_says_so(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("no")
    monkeypatch.setattr(heavy_place, "decide_selection", boom)
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1
    assert "selection decision failed: ValueError" in result.suite_place
    assert any("selection decision failed: ValueError" in x for x in w.log_lines())
