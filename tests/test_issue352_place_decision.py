"""#352 unit 2: ``place_decision.decide_place`` (rules 2, 4, 6 of the issue): GitHub when the start time of a machine
plus the run there is sooner than the wait in the PC line plus the run on the PC, else the PC.

Seam: ``decide_place(ids, settings, runner=, now=, deps=)`` with a fake ``Deps`` (no disk, no network); the pure wait
helper ``line_wait_seconds`` for the PC line's formula.
"""
from __future__ import annotations

from tools.agentops import merge_github
from tools.agentops.place_decision import Deps, TestPlace, decide_place, line_wait_seconds

PLAN = merge_github.Plan(remote="https://example.invalid/copy.git", own_repo=False, repo_slug="o/r", repo_name="r")
ON = {"github_suite": True, "github_start_seconds_linux": 90, "github_start_seconds_windows": 150}  # the tests' own start times, not the measured defaults
IDS = [f"tests/test_a.py::t{i}" for i in range(10)]
NOW = 1_000_000.0


def deps(plan=PLAN, note="", run=300.0, wait=0.0, needs=()):
    return Deps(
        plan_for=lambda: (plan, note),
        pc_run_seconds=lambda ids: run,
        pc_wait_seconds=lambda now: wait,
        needs_pc=lambda ids: [i for i in ids if i in needs],
    )


def place(ids=IDS, settings=ON, runner="linux", **kw) -> TestPlace:
    return decide_place(ids, settings, runner=runner, now=NOW, deps=deps(**kw))


def test_rule1_github_suite_off_goes_to_pc():
    got = place(settings={"github_suite": False})
    assert (got.where, got.reason) == ("pc", "github_suite off")


def test_rule1_no_plan_with_cap_note_goes_to_pc_naming_the_cap():
    got = place(plan=None, note="own-runners repo over free minutes: 2000 of 2000")
    assert got.where == "pc"
    assert got.reason == "GitHub over the minutes cap: own-runners repo over free minutes: 2000 of 2000"


def test_rule1_no_plan_without_note_goes_to_pc():
    got = place(plan=None, note="")
    assert (got.where, got.reason) == ("pc", "no GitHub plan")


def test_rule2_ids_that_need_the_pc_are_split_off_and_the_rest_is_decided():
    seen = []
    d = deps(needs=(IDS[0], IDS[1]), run=600.0, wait=1800.0)
    d = Deps(plan_for=d.plan_for, pc_run_seconds=lambda ids: seen.append(list(ids)) or 600.0,
             pc_wait_seconds=d.pc_wait_seconds, needs_pc=d.needs_pc)
    got = decide_place(IDS, ON, now=NOW, deps=d)
    assert seen == [IDS[2:]]
    assert got.pc_ids == [IDS[0], IDS[1]]
    assert got.where == "github"


def test_rule2_empty_rest_goes_to_pc():
    got = place(needs=tuple(IDS), run=600.0, wait=1800.0)
    assert got.where == "pc"
    assert got.pc_ids == IDS
    assert got.reason == "every id needs the PC"


def test_rule2_no_ids_goes_to_pc():
    got = place(ids=[])
    assert (got.where, got.pc_ids) == ("pc", [])


def test_rule3_unknown_duration_goes_to_pc():
    got = place(run=None, wait=1800.0)
    assert (got.where, got.reason) == ("pc", "no duration data: PC")


def test_rule4_short_run_stays_on_pc_even_when_the_line_is_busy():
    got = place(run=40.0, wait=3600.0)
    assert got.where == "pc"
    assert got.reason == "run 40 s < floor 2 min"
    assert got.pc_seconds == 40.0


def test_rule4_floor_is_a_setting():
    got = place(settings={**ON, "place_pc_only_floor_seconds": 600}, run=300.0, wait=3600.0)
    assert (got.where, got.reason) == ("pc", "run 5 min < floor 10 min")


def test_rule4_run_equal_to_floor_is_decided_by_rule5():
    got = place(run=120.0, wait=3600.0)
    assert got.where == "github"


def test_rule5_github_faster_only_because_the_pc_line_is_busy():
    busy = place(run=180.0, wait=28 * 60.0)
    idle = place(run=180.0, wait=0.0)
    assert busy.where == "github"
    assert busy.reason == "GitHub 4 min 30 s < PC 31 min (wait 28 min + run 3 min)"
    assert (busy.github_seconds, busy.pc_seconds) == (270.0, 1860.0)
    assert idle.where == "pc"
    assert idle.reason == "PC 3 min <= GitHub 4 min 30 s (wait 0 s + run 3 min)"


def test_rule5_tie_goes_to_pc():
    got = place(run=180.0, wait=90.0)  # GitHub 1 min 30 s + 3 min = PC 1 min 30 s + 3 min
    assert got.where == "pc"
    assert got.github_seconds == got.pc_seconds == 270.0


def test_rule5_start_time_is_per_runner_and_a_setting():
    kw = dict(run=180.0, wait=100.0)  # PC 4 min 40 s
    assert place(runner="linux", **kw).where == "github"      # 1 min 30 s + 3 min = 4 min 30 s
    assert place(runner="windows", **kw).where == "pc"        # 2 min 30 s + 3 min = 5 min 30 s
    quick = {**ON, "github_start_seconds_windows": 60}
    assert place(settings=quick, runner="windows", **kw).where == "github"  # 1 min + 3 min = 4 min


def test_rule5_speed_ratio_scales_the_run_on_github():
    kw = dict(run=180.0, wait=300.0)  # PC 8 min
    assert place(**kw).where == "github"                                         # 1 min 30 s + 3 min = 4 min 30 s
    slow = {**ON, "github_speed_ratio": 2.0}
    got = place(settings=slow, **kw)                                             # 1 min 30 s + 6 min = 7 min 30 s < 8 min
    assert (got.where, got.github_seconds) == ("github", 450.0)
    slower = {**ON, "github_speed_ratio": 3.0}
    assert place(settings=slower, **kw).where == "pc"                            # 1 min 30 s + 9 min = 10 min 30 s


def test_rule6_ids_needing_the_pc_are_returned_on_github():
    got = place(needs=(IDS[3],), run=600.0, wait=1800.0)
    assert got.where == "github"
    assert got.pc_ids == [IDS[3]]


def test_rule6_ids_needing_the_pc_are_returned_on_pc_too():
    got = place(needs=(IDS[3],), run=40.0)
    assert (got.where, got.pc_ids) == ("pc", [IDS[3]])


def test_unreadable_pc_line_counts_zero_and_is_said_in_the_reason():
    def broken(now):
        raise OSError("line folder unreadable")
    d = Deps(plan_for=lambda: (PLAN, ""), pc_run_seconds=lambda ids: 180.0, pc_wait_seconds=broken,
             needs_pc=lambda ids: [])
    got = decide_place(IDS, ON, now=NOW, deps=d)
    assert got.where == "pc"
    assert "PC line unreadable, wait counted 0" in got.reason


def test_line_wait_is_the_holders_remaining_time_from_its_take_plus_each_waiter_in_full():
    # joined the line long ago (since), took the berth 20 s ago (taken): the run began at the take
    holder = {"tests": 100, "since": NOW - 1000.0, "taken": NOW - 20.0}
    waiters = [{"tests": 40}, {"tests": 10}]
    # per test 0.5 s: holder 50 s run - 20 s since its take = 30 s; waiters 20 s and 5 s
    assert line_wait_seconds([holder], waiters, 0.5, NOW) == 55.0


def test_line_wait_holder_past_its_expected_time_counts_zero_and_no_line_is_zero():
    assert line_wait_seconds([{"tests": 10, "taken": NOW - 500.0}], [], 0.5, NOW) == 0.0
    assert line_wait_seconds([], [], 0.5, NOW) == 0.0


def test_line_wait_is_divided_by_the_berths():
    holders = [{"tests": 100, "taken": NOW - 20.0}, {"tests": 100, "taken": NOW - 20.0}]  # 30 s each
    waiters = [{"tests": 40}]                                                           # 20 s
    assert line_wait_seconds(holders, waiters, 0.5, NOW, berths=2) == 40.0
    assert line_wait_seconds(holders, waiters, 0.5, NOW, berths=1) == 80.0
    assert line_wait_seconds(holders, waiters, 0.5, NOW, berths=0) == 0.0  # 0 berths = no limit, no line


def _fake_line(monkeypatch, holders, waiters):
    from tools.agentops import machine_berths, place_decision
    monkeypatch.setattr(place_decision, "_stored", lambda root: {"t::a": 0.5, "t::b": 0.5})
    monkeypatch.setattr(machine_berths, "holders", lambda: holders)
    monkeypatch.setattr(machine_berths, "holder_is_live", lambda pid, started: pid == 1)
    monkeypatch.setattr(machine_berths, "waiting_rows", lambda **kw: waiters)


def test_default_wait_reads_taken_the_live_pc_holders_the_pc_waiters_and_the_berths_setting(monkeypatch):
    from tools.agentops import place_decision
    holders = [{"pid": 1, "tests": 100, "taken": NOW - 20.0, "since": NOW - 900.0},
               {"pid": 1, "tests": 100, "taken": NOW - 20.0, "kind": "github"},   # a GitHub holder: not the PC
               {"pid": 2, "tests": 100, "taken": NOW - 20.0}]                     # dead: not counted
    waiters = [{"tests": 40}, {"tests": 400, "kind": "github"}]
    _fake_line(monkeypatch, holders, waiters)
    one = place_decision._defaults(Deps(), {}, None).pc_wait_seconds
    assert one(NOW) == 50.0                                                       # 30 s + 20 s
    two = place_decision._defaults(Deps(), {"machine_heavy_runs": 2}, None).pc_wait_seconds
    assert two(NOW) == 25.0


def test_rule1_is_checked_before_the_pc_only_reads():
    def must_not_run(ids):
        raise AssertionError("needs_pc called for a run that rule 1 sends to the PC")
    d = Deps(plan_for=lambda: (None, ""), pc_run_seconds=lambda ids: 300.0, pc_wait_seconds=lambda now: 0.0,
             needs_pc=must_not_run)
    got = decide_place(IDS, ON, now=NOW, deps=d)
    assert got.where == "pc"
    off = decide_place(IDS, {"github_suite": False}, now=NOW, deps=d)
    assert (off.where, off.reason, off.pc_ids) == ("pc", "github_suite off", [])


def test_failed_pc_only_read_goes_to_pc_with_every_id_in_pc_ids():
    def broken(ids):
        raise OSError("pc_only list unreadable")
    d = Deps(plan_for=lambda: (PLAN, ""), pc_run_seconds=lambda ids: 600.0, pc_wait_seconds=lambda now: 1800.0,
             needs_pc=broken)
    got = decide_place(IDS, ON, now=NOW, deps=d)
    assert got.where == "pc"
    assert got.reason.startswith("PC-only list unreadable")
    assert got.pc_ids == IDS


def test_rule4_floor_is_at_least_the_runners_start_time():
    # a Windows run of 2 min 10 s is shorter than the 2 min 30 s start: it stays on the PC whatever the line
    got = place(runner="windows", run=130.0, wait=40 * 60.0)
    assert got.where == "pc"
    assert got.reason == "run 2 min 10 s < GitHub start 2 min 30 s"
    assert place(runner="linux", run=130.0, wait=40 * 60.0).where == "github"  # start 1 min 30 s, floor 2 min


def test_seconds_not_computed_are_none_not_zero():
    assert (place(settings={"github_suite": False}).pc_seconds, place(settings={"github_suite": False}).github_seconds) == (None, None)
    assert (place(run=None).pc_seconds, place(run=None).github_seconds) == (None, None)
    short = place(run=40.0)
    assert (short.pc_seconds, short.github_seconds) == (40.0, None)
    full = place(run=180.0, wait=28 * 60.0)
    assert (full.pc_seconds, full.github_seconds) == (1860.0, 270.0)


def test_unreadable_test_copy_rules_are_said_in_the_reason(tmp_path, monkeypatch):
    from tools.agentops import place_decision, test_copy_check
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "pc_only.txt").write_text("tests/test_win.py\n", encoding="utf-8")

    def boom(root):
        raise RuntimeError("git ls-files failed")
    monkeypatch.setattr(test_copy_check, "snapshot_files", boom)
    needs = place_decision._defaults(Deps(), ON, tmp_path).needs_pc
    found = needs(["tests/test_win.py::a", "tests/test_other.py::b"])
    assert list(found) == ["tests/test_win.py::a"]          # the pc_only list alone still decides
    assert "test-copy rules unreadable: git ls-files failed" in found.note
    d = Deps(plan_for=lambda: (PLAN, ""), pc_run_seconds=lambda ids: 600.0, pc_wait_seconds=lambda now: 1800.0,
             needs_pc=lambda ids: found)
    got = decide_place(IDS, ON, now=NOW, deps=d)
    assert got.reason.endswith("; test-copy rules unreadable: git ls-files failed")


def test_invalid_setting_is_said_in_the_reason_and_the_default_is_used():
    got = place(settings={**ON, "github_speed_ratio": "fast"}, run=180.0, wait=28 * 60.0)
    assert got.where == "github"
    assert got.github_seconds == 270.0                       # the default ratio 1.0
    assert got.reason.endswith("; setting github_speed_ratio invalid, default used")
    assert "invalid" not in place(run=180.0, wait=28 * 60.0).reason
