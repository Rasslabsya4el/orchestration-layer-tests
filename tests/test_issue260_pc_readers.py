"""#260: a PC-only test that reads a file the public test copy does not take (the plan's reads data decides) runs on the
PC in the merge, not on the GitHub Windows job; the rest of the run stays on GitHub.

Seam: `run_merge` with the fakes of test_issue247_routing (a GitHub that answers from a queue; the spy on
`run_github_suite` shows what the Windows job was asked for). The PC run of the readers is a fake `_run_ids_on_pc`."""
from __future__ import annotations

import json
import sys

from tests.test_issue232_wiring import BAD, World, _git, red
from tests.test_issue247_routing import GREEN, NOT_STARTED, QFakes, ids_of, select_ids
from tools.agentops import merge as merge_module
from tools.agentops.test_select import map_dir

READER_A = "tests/test_win.py::test_reads_a"
READER_B = "tests/test_win.py::test_reads_b"
OTHER = "tests/test_other_win.py::test_plain"
EXCLUDED_FILE = "docs/research/notes.md"  # `docs/research/` is never in a test copy


class ReaderWorld(World):
    """A World whose branch lists two PC-only files and whose map holds reads data: the two tests of test_win.py read
    a file the copy does not take, the test of test_other_win.py reads a file it does."""

    def __init__(self, tmp_path, monkeypatch, *, reads=True, pc_result=(True, "ok\n"), **kw):
        super().__init__(tmp_path, monkeypatch, **kw)
        worker = tmp_path / "worker"
        _git(worker, "checkout", "-q", "feat")
        (worker / "tests").mkdir()
        for name, body in (("test_win.py", "def test_reads_a():\n    pass\n\ndef test_reads_b():\n    pass\n"),
                           ("test_other_win.py", "def test_plain():\n    pass\n")):
            (worker / "tests" / name).write_text(body, encoding="utf-8")
        (worker / "tests" / "pc_only.txt").write_text("tests/test_win.py\ntests/test_other_win.py\n", encoding="utf-8")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", "pc only")
        _git(worker, "push", "-q", "origin", "feat")
        if reads:
            reads_dir = map_dir(self.checkout) / "reads"
            reads_dir.mkdir(parents=True)
            dump = {"files": {READER_A: [EXCLUDED_FILE], READER_B: [EXCLUDED_FILE, "pkg/a.py"], OTHER: ["pkg/a.py"]},
                    "dirs": {}}
            (reads_dir / "reads-main-1.json").write_text(json.dumps(dump), encoding="utf-8")
        self.pc_ids_runs: list[list[str]] = []
        self.pc_result = pc_result

        def run_ids(suite_cmd, ids, suite_dir, run_env):
            self.pc_ids_runs.append(list(ids))
            return self.pc_result
        monkeypatch.setattr(merge_module, "_run_ids_on_pc", run_ids)


def test_readers_leave_the_windows_job_and_run_on_the_pc(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(3), READER_A, OTHER, READER_B])
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert len(w.calls) == 1
    assert w.calls[0]["ids"] == [*ids_of(3), OTHER] and w.calls[0]["tests"] == 4 and w.calls[0]["windows"] is True
    assert w.pc_ids_runs == [[READER_A, READER_B]]
    assert result.suite_place.startswith("suite on GitHub: ") and result.suite_place.endswith("(+ 2 tests on the PC)")


def test_a_red_reader_on_the_pc_makes_the_merge_red_and_is_named(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {READER_B} - boom\n1 failed\n"
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN), pc_result=(False, out))
    select_ids(monkeypatch, [*ids_of(3), READER_A, READER_B])
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [READER_B]
    assert READER_B in result.message and not result.pushed
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_red_on_both_runs_names_the_ids_of_both(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {READER_A} - boom\n1 failed\n"
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(red(), red()), pc_result=(False, out))
    select_ids(monkeypatch, [*ids_of(3), READER_A, BAD])
    result = w.merge()
    assert not result.ok and result.reason == "red-suite"
    assert set(result.failed_tests) == {BAD, READER_A}


def test_no_reads_data_keeps_every_pc_only_id_on_the_windows_job(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, reads=False, fakes=QFakes(GREEN))
    ids = [*ids_of(3), READER_A, OTHER]
    select_ids(monkeypatch, ids)
    result = w.merge()
    assert result.ok and w.pc_ids_runs == []
    assert w.calls[0]["ids"] == ids and w.calls[0]["windows"] is True
    assert "on the PC" not in result.suite_place


def test_a_selection_without_a_reader_is_unchanged(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    ids = [*ids_of(3), OTHER]
    select_ids(monkeypatch, ids)
    result = w.merge()
    assert result.ok and w.pc_ids_runs == []
    assert w.calls[0]["ids"] == ids and w.calls[0]["windows"] is True
    assert "on the PC" not in result.suite_place


def test_only_readers_start_no_github_run_and_no_windows_job(tmp_path, monkeypatch):
    fakes = QFakes(GREEN)
    w = ReaderWorld(tmp_path, monkeypatch, fakes=fakes)
    select_ids(monkeypatch, [READER_A, READER_B])
    result = w.merge()
    assert result.ok and result.pushed and w.calls == [] and fakes.dispatches == []
    assert w.pc_ids_runs == [[READER_A, READER_B]] and w.pc_runs == []
    assert result.suite_place.startswith("suite on GitHub (+ 2 tests on the PC)")


def test_the_real_pc_run_runs_only_the_ids_it_is_given(tmp_path):
    (tmp_path / "test_two.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert False\n", encoding="utf-8")
    suite = f'"{sys.executable}" -m pytest tests -q'
    ok, out = merge_module._run_ids_on_pc(suite, ["test_two.py::test_ok"], tmp_path, None)
    assert ok and "1 passed" in out
    ok, out = merge_module._run_ids_on_pc(suite, ["test_two.py::test_bad"], tmp_path, None)
    assert not ok and merge_module._failed_ids(out) == ["test_two.py::test_bad"]


def test_one_reader_green_says_one_test_on_the_pc(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(3), READER_A])
    result = w.merge()
    assert result.ok and w.pc_ids_runs == [[READER_A]]
    assert result.suite_place.startswith("suite on GitHub: ") and result.suite_place.endswith("(+ 1 test on the PC)")


def test_a_github_red_recovered_by_its_recheck_is_not_named_when_the_pc_reader_is_red(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {READER_A} - boom\n1 failed\n"
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(red(), GREEN), pc_result=(False, out))
    select_ids(monkeypatch, [*ids_of(3), READER_A, BAD])
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [READER_A]
    assert BAD not in result.message and READER_A in result.message


def test_a_github_red_left_to_the_pc_recheck_and_a_red_reader_share_one_summary(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {READER_A} - boom\n1 failed\n"
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(red(), NOT_STARTED), pc_result=(False, out))
    w.recheck_still = [BAD, READER_A]
    select_ids(monkeypatch, [*ids_of(3), READER_A, BAD])
    result = w.merge()
    assert w.rechecks == [[BAD, READER_A]]  # the caller's PC re-check got both sets of ids
    assert not result.ok and result.reason == "red-suite" and set(result.failed_tests) == {BAD, READER_A}
