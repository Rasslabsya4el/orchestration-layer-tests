"""#247 unit 3: a merge's pytest run goes to GitHub whenever there is a plan (selected, full, re-check, PC-only), the PC
only on a #232 rule-4 fallback.

Seam: `run_merge` with the fakes of test_issue232_wiring (a GitHub that answers from a queue, a PC suite that is a
fake); the spy on `run_github_suite` shows what each merge asked GitHub for (tests, ids, windows)."""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_issue232_wiring import BAD, Fakes, World, _git, red
from tools.agentops import merge_github, test_select

PC_ID = "tests/test_win.py::test_boom"
GREEN = {"run_id": 77, "parts": [{"part": 1, "seconds": 61}], "minutes_used": 4, "outcome": "green",
         "failed_ids": [], "full_recheck": False}
NOT_STARTED = {"run_id": 80, "parts": [], "minutes_used": 0, "outcome": "not-started", "failed_ids": [],
               "full_recheck": False}


class QFakes(Fakes):
    """A GitHub that answers its runs in order (the last answer repeats) and remembers the machines it was asked for."""

    def __init__(self, *answers, machines=3):
        super().__init__(answers[0] if answers else None, machines)
        self.queue = list(answers) or [self.poll_result]
        self.wants: list = []
        self.on_dispatch = None

    def acquire_machines(self, repo, run, want=None, **kw):
        self.wants.append(want)
        return super().acquire_machines(repo, run, want, **kw)

    def dispatch(self, repo, branch, parts, *a, **kw):
        if self.on_dispatch is not None and not self.dispatches:
            self.on_dispatch()
        return super().dispatch(repo, branch, parts, *a, **kw)

    def poll(self, repo, run_id, *a, **kw):
        self.polls.append((repo, run_id))
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


class PcWorld(World):
    """A World whose worker branch carries tests/test_win.py and the `tests/pc_only.txt` that lists it."""

    def __init__(self, tmp_path, monkeypatch, *, listing="tests/test_win.py\n", **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        worker = tmp_path / "worker"
        _git(worker, "checkout", "-q", "feat")
        (worker / "tests").mkdir()
        (worker / "tests" / "test_win.py").write_text("def test_boom():\n    pass\n", encoding="utf-8")
        (worker / "tests" / "pc_only.txt").write_text(listing, encoding="utf-8")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", "pc only")
        _git(worker, "push", "-q", "origin", "feat")


def select_ids(monkeypatch, ids, total=4000):
    monkeypatch.setattr("tools.agentops.merge.resolve_select_tests", lambda: True)
    monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: test_select.Selection(
        False, "", list(ids), 0, total, "m" * 40, f"selected {len(ids)} of {total} (map mmmmmmmm)"))


def ids_of(n):
    return [f"tests/t.py::t{i}" for i in range(n)]


# -- row 1: the machines and the ids a selected or a full run asks for ---------------------------------------------

def test_179_selected_tests_ask_one_machine_and_send_their_ids(tmp_path, monkeypatch):
    fakes = QFakes(GREEN)
    w = World(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, ids_of(179))
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == []
    assert len(w.calls) == 1
    assert (w.calls[0]["tests"], w.calls[0]["ids"], w.calls[0]["windows"]) == (179, ids_of(179), False)
    assert fakes.wants == [1]


def test_1000_selected_tests_ask_two_machines(tmp_path, monkeypatch):
    fakes = QFakes(GREEN)
    w = World(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, ids_of(1000))
    result = w.merge()
    assert result.ok and w.pc_runs == []
    assert w.calls[0]["tests"] == 1000 and len(w.calls[0]["ids"]) == 1000 and fakes.wants == [2]


def test_a_full_suite_asks_every_machine_and_no_ids(tmp_path, monkeypatch):
    fakes = QFakes(GREEN)
    w = World(tmp_path, monkeypatch, fakes=fakes)
    result = w.merge()
    assert result.ok and w.pc_runs == []
    assert (w.calls[0]["tests"], w.calls[0]["ids"], w.calls[0]["windows"]) == (None, None, False)
    assert fakes.wants == [5]


# -- row 2: place, red, re-check on GitHub --------------------------------------------------------------------------

def test_a_selected_run_green_on_github_records_the_github_place(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.rechecks == []
    assert result.suite_place.startswith("suite on GitHub: 3 machines") and result.suite_machines == 3
    assert result.selection.startswith("selected 50 of 4000")
    assert any(result.suite_place in x for x in w.log_lines() if "| pushed |" in x)


def test_a_red_names_its_failed_id_and_the_recheck_goes_to_github(tmp_path, monkeypatch):
    fakes = QFakes(red(), GREEN)
    w = World(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and result.pushed and result.rechecked == [BAD]
    assert len(w.calls) == 2 and w.pc_runs == [] and w.rechecks == []  # no PC suite, no PC re-check
    assert (w.calls[1]["tests"], w.calls[1]["ids"], w.calls[1]["windows"]) == (1, [BAD], False)
    assert fakes.wants == [1, 1]


def test_a_failed_id_that_fails_again_on_github_is_red_naming_it(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(red(), red()))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [BAD] and BAD in result.message
    assert len(w.calls) == 2 and w.pc_runs == [] and w.rechecks == []
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_a_full_run_red_is_rechecked_on_github_too(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(red(), GREEN))
    result = w.merge()
    assert result.ok and result.rechecked == [BAD]
    assert w.calls[0]["tests"] is None and w.calls[1]["tests"] == 1 and w.calls[1]["ids"] == [BAD]
    assert w.pc_runs == [] and w.rechecks == []


def test_a_recheck_github_cannot_start_falls_back_to_the_pc_recheck(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(red(), NOT_STARTED))
    select_ids(monkeypatch, ids_of(50))
    w.recheck_still = []
    result = w.merge()
    assert result.ok and result.rechecked == [BAD]
    assert w.pc_runs == [] and w.rechecks == [[BAD]]  # the PC re-checks the id, not the suite
    lines = w.brain_lines()
    assert len(lines) == 1 and lines[0]["line"].endswith(": GitHub not-started, suite on the PC")


# -- row 3: a PC-only file among the selected ids runs on the Windows job -----------------------------------------

def test_a_pc_only_id_among_the_selected_sets_windows_and_starts_no_pc_run(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(3), PC_ID])
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert w.calls[0]["windows"] is True and w.calls[0]["ids"] == [*ids_of(3), PC_ID] and w.calls[0]["tests"] == 4


def test_no_pc_only_id_among_the_selected_leaves_windows_off(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(3))
    assert w.merge().ok
    assert w.calls[0]["windows"] is False


def test_a_full_run_asks_for_windows_when_pc_only_files_exist(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and w.pc_runs == []
    assert (w.calls[0]["tests"], w.calls[0]["ids"], w.calls[0]["windows"]) == (None, None, True)


def test_a_failed_pc_only_id_is_rechecked_on_github_with_windows_and_no_pc_run(tmp_path, monkeypatch):
    fakes = QFakes(red(ids=(PC_ID,)), GREEN)
    w = PcWorld(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, [*ids_of(3), PC_ID])
    result = w.merge()
    assert result.ok and result.rechecked == [PC_ID]
    assert w.calls[1]["ids"] == [PC_ID] and w.calls[1]["windows"] is True and w.calls[1]["tests"] == 1
    assert w.pc_runs == [] and w.rechecks == []


def test_a_pc_only_id_still_red_after_the_github_recheck_is_red(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(red(ids=(PC_ID,)), red(ids=(PC_ID,))))
    select_ids(monkeypatch, [*ids_of(3), PC_ID])
    result = w.merge()
    assert not result.ok and result.failed_tests == [PC_ID] and w.pc_runs == [] and w.rechecks == []


def test_an_empty_pc_only_list_leaves_windows_off(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, listing="# none\n", fakes=QFakes(GREEN))
    assert w.merge().ok and w.calls[0]["windows"] is False


# -- the PC runs only on a rule-4 fallback -------------------------------------------------------------------------

def test_a_selected_run_github_does_not_start_runs_on_the_pc_as_before(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(NOT_STARTED))
    select_ids(monkeypatch, ids_of(50))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and '"@' in w.pc_runs[0]  # the selected ids, on the PC
    assert result.suite_place == "suite on the PC (GitHub not-started)"
    assert len(w.brain_lines()) == 1


def test_a_pc_only_selected_run_that_falls_back_runs_once_on_the_pc(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(NOT_STARTED))
    select_ids(monkeypatch, [*ids_of(3), PC_ID])
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1


def test_a_suite_command_that_cannot_name_files_still_goes_to_github(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge_with(f'"{__import__("sys").executable}" -m pytest -q')
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1
    assert (w.calls[0]["ids"], w.calls[0]["windows"]) == (None, True)  # windows decided from the PC-only file list


def test_a_red_that_names_no_test_runs_once_on_the_pc_and_says_so(tmp_path, monkeypatch):
    nameless = {"run_id": 81, "parts": [{"part": 1, "seconds": 9}], "minutes_used": 1, "outcome": "red-test",
                "failed_ids": [], "full_recheck": False}  # ids sent but nothing collected (unit 4's exit-4 failure)
    w = World(tmp_path, monkeypatch, fakes=QFakes(nameless))
    select_ids(monkeypatch, ids_of(179))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and len(w.calls) == 2 and w.rechecks == []  # #352 row 8: one more GitHub run first
    assert "red: no job read; ids found: 0" in result.suite_place  # #345: the reason names what the reader saw
    assert any("red: no job read; ids found: 0" in x and "| pushed |" in x for x in w.log_lines())
    assert len(w.brain_lines()) == 1


def test_a_partial_run_whose_ids_cannot_be_read_goes_to_the_pc_with_its_reason(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, ids_of(50))
    monkeypatch.setattr("tools.agentops.merge._ids_in_cmd", lambda cmd: None)
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.calls == []
    assert result.suite_place == "suite on the PC (partial run, ids unreadable)"
    assert any("suite on the PC (partial run, ids unreadable)" in x for x in w.log_lines())


def test_unreadable_github_settings_go_to_the_pc_with_their_reason(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    monkeypatch.setattr(merge_github, "plan_for", lambda settings, cwd: (_ for _ in ()).throw(OSError("bad")))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.calls == []
    assert result.suite_place == "suite on the PC (github settings unreadable)"
    assert any("suite on the PC (github settings unreadable)" in x for x in w.log_lines())


def test_an_empty_selection_still_asks_nobody(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [])
    assert w.merge().ok and w.calls == [] and w.pc_runs == []


# -- #212: a re-check after main moved goes to GitHub ---------------------------------------------------------------

def test_a_recheck_after_main_moved_goes_to_github(tmp_path, monkeypatch):
    fakes = QFakes(GREEN, GREEN)
    w = World(tmp_path, monkeypatch, fakes=fakes)

    def move():  # another writer lands a code commit on main while the first run is on GitHub
        (w.checkout / "pkg" / "b.py").write_text("B = 2\n")
        _git(w.checkout, "add", "-A")
        _git(w.checkout, "commit", "-q", "-m", "move b")
        _git(w.checkout, "push", "-q", "origin", "main")
    fakes.on_dispatch = move
    moved = ids_of(2)
    monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: test_select.Selection(
        False, "", moved, 0, 100, "m" * 40, f"selected {len(moved)} of 100 (map mmmmmmmm)"))
    result = w.merge()
    assert result.ok and result.pushed and result.suite_runs == 2
    assert len(w.calls) == 2 and w.pc_runs == []
    assert (w.calls[0]["tests"], w.calls[0]["ids"]) == (None, None)
    assert (w.calls[1]["tests"], w.calls[1]["ids"], w.calls[1]["windows"]) == (2, moved, False)
    assert re.fullmatch(r"suite .+ \+ re-check after main moved: 2 tests, .+", result.selection), result.selection
