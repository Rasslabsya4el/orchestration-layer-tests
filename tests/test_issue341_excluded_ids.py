"""#341 unit 1: an id whose file the public test copy does not take leaves the GitHub ids and runs on the PC.

A merge that changes a test file listed in `test_copy_exclude` sent that file's bare path to GitHub, where it is not
collected (`ids sent but not collected`), and the whole suite fell back to the PC. The excluded ids (a bare path or a
node id) now run on the PC beside the GitHub run, folded into the verdict like #260's readers.

Seam: `run_merge` with the fakes of test_issue247_routing (a GitHub that answers from a queue; the spy on
`run_github_suite` shows the ids it was given). The PC run of the excluded ids is a fake `_run_ids_on_pc`."""
from __future__ import annotations

import json

from tests.test_issue232_wiring import World, _git
from tests.test_issue247_routing import GREEN, QFakes, ids_of, select_ids
from tools.agentops import github_run
from tools.agentops import merge as merge_module

EXCLUDED = "tests/test_comment_tasks_extract.py"  # in config/orchestration.json `test_copy_exclude`
NODE_A = f"{EXCLUDED}::test_a"
NODE_B = f"{EXCLUDED}::test_b"
THREE_PASSED = "...\n3 passed in 0.10s\n"


class ExcludedWorld(World):
    """A World whose branch carries a tracked test file the settings keep out of the copy (3 tests) and the settings
    that say so."""

    def __init__(self, tmp_path, monkeypatch, *, pc_result=(True, THREE_PASSED), **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        worker = tmp_path / "worker"
        _git(worker, "checkout", "-q", "feat")
        (worker / "tests").mkdir()
        (worker / "tests" / "test_comment_tasks_extract.py").write_text(
            "def test_a():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n", encoding="utf-8")
        (worker / "config").mkdir()
        (worker / "config" / "orchestration.json").write_text(
            json.dumps({"test_copy_exclude": [EXCLUDED]}), encoding="utf-8")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", "an excluded test file")
        _git(worker, "push", "-q", "origin", "feat")
        self.pc_ids_runs: list[list[str]] = []
        self.pc_result = pc_result

        def run_ids(suite_cmd, ids, suite_dir, run_env):
            self.pc_ids_runs.append(list(ids))
            return self.pc_result
        monkeypatch.setattr(merge_module, "_run_ids_on_pc", run_ids)


def test_a_changed_excluded_file_leaves_the_github_ids_and_runs_on_the_pc(tmp_path, monkeypatch):
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [EXCLUDED, *ids_of(20)])
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert len(w.calls) == 1
    assert w.calls[0]["ids"] == ids_of(20) and w.calls[0]["tests"] == 20
    assert not [i for i in w.calls[0]["ids"] if i.split("::")[0] == EXCLUDED]
    assert w.pc_ids_runs == [[EXCLUDED]]
    assert result.suite_place.startswith("suite on GitHub: ") and result.suite_place.endswith("(+ 3 tests on the PC)")


def test_excluded_node_ids_from_the_map_leave_the_github_ids(tmp_path, monkeypatch):
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(10), NODE_A, *ids_of(20)[10:], NODE_B])
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1
    assert w.calls[0]["ids"] == ids_of(20) and w.calls[0]["tests"] == 20
    assert w.pc_ids_runs == [[NODE_A, NODE_B]]
    assert result.suite_place.endswith("(+ 2 tests on the PC)")


def test_a_red_excluded_file_makes_the_merge_red_and_names_its_id(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {NODE_B} - boom\n1 failed, 2 passed in 0.10s\n"
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN), pc_result=(False, out))
    select_ids(monkeypatch, [EXCLUDED, *ids_of(20)])
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [NODE_B]
    assert NODE_B in result.message and not result.pushed
    assert w.pc_runs == [] and len(w.calls) == 1 and w.calls[0]["ids"] == ids_of(20)


def test_only_excluded_ids_start_no_github_run(tmp_path, monkeypatch):
    fakes = QFakes(GREEN)
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, [EXCLUDED])
    result = w.merge()
    assert result.ok and result.pushed and w.calls == [] and fakes.dispatches == [] and w.pc_runs == []
    assert w.pc_ids_runs == [[EXCLUDED]]
    assert result.suite_place == ("suite on GitHub (+ 3 tests on the PC): no test left for GitHub "
                                  "(every selected test is in a file kept out of the copy)")


def test_an_id_of_an_untracked_file_stays_on_github(tmp_path, monkeypatch):
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    ids = ids_of(5)  # tests/t.py is not a tracked file of the branch: nothing says the copy refuses it
    select_ids(monkeypatch, ids)
    result = w.merge()
    assert result.ok and w.pc_ids_runs == [] and w.calls[0]["ids"] == ids
    assert "on the PC" not in result.suite_place


def test_an_id_of_a_tracked_file_the_copy_takes_stays_on_github(tmp_path, monkeypatch):
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    copied = ["pkg/a.py::test_x", "pkg/a.py"]  # pkg/a.py is tracked and not in test_copy_exclude
    select_ids(monkeypatch, [*copied, EXCLUDED])
    result = w.merge()
    assert result.ok and w.pc_ids_runs == [[EXCLUDED]] and w.calls[0]["ids"] == copied
    assert result.suite_place.endswith("(+ 3 tests on the PC)")


def test_a_too_big_selection_becomes_a_full_github_run_and_the_excluded_ids_still_run_on_the_pc(tmp_path, monkeypatch):
    monkeypatch.setattr(github_run, "inputs_fit", lambda *a, **kw: False)
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [EXCLUDED, *ids_of(20)])
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1
    assert w.calls[0]["ids"] is None and w.calls[0]["tests"] is None  # GitHub runs the full suite
    assert not w.calls[0].get("skip")  # an excluded id is not in the copy: nothing to skip
    assert w.pc_ids_runs == [[EXCLUDED]]
    assert "selection too big for one dispatch (20 ids), full suite" in result.suite_place
    assert result.suite_place.endswith("(+ 3 tests on the PC)")


def test_a_full_run_sends_nothing_new_to_the_pc(tmp_path, monkeypatch):
    w = ExcludedWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and w.pc_ids_runs == [] and w.pc_runs == []
    assert w.calls[0]["ids"] is None
    assert "on the PC" not in result.suite_place
