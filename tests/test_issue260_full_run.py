"""#260 unit 2: a FULL run (no ids) of a merge. The PC-only tests that read a file the public test copy does not take
(the plan's reads data) are not run by GitHub's Windows job -- the merge sends their ids as ``skip`` -- and the merge
runs them on the PC; red if either side is red.

Seams: `run_merge` with the fakes of test_issue260_pc_readers (the spy on `run_github_suite` shows the ``skip`` it was
given), the shard plugin's collection hook (``TEST_SKIP_FILE``), `github_run.dispatch` (the ``skip`` input) and the
workflow text (the Windows job reads the input)."""
from __future__ import annotations

import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from tests.test_issue232_github_run import FakeGh, REPO, Clock, BASE, run_row
from tests.test_issue232_wiring import _git
from tests.test_issue247_routing import GREEN, QFakes
from tests.test_issue260_pc_readers import OTHER, READER_A, READER_B, ReaderWorld
from tools.agentops import github_run, workflow_sync
from tools.agentops import test_shard_plugin as shard

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


# -- the merge --------------------------------------------------------------------------------------------------------

def test_a_full_run_sends_the_readers_as_skip_and_runs_them_on_the_pc(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert len(w.calls) == 1
    assert (w.calls[0]["ids"], w.calls[0]["tests"], w.calls[0]["windows"]) == (None, None, True)
    assert w.calls[0]["skip"] == [READER_A, READER_B]
    assert w.pc_ids_runs == [[READER_A, READER_B]]
    assert result.suite_place.startswith("suite on GitHub: ") and result.suite_place.endswith("(+ 2 tests on the PC)")


def test_a_red_reader_makes_a_full_run_red_and_is_named(tmp_path, monkeypatch):
    out = f"short test summary info\nFAILED {READER_B} - boom\n1 failed\n"
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN), pc_result=(False, out))
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [READER_B]
    assert READER_B in result.message and not result.pushed
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_a_full_run_without_reads_data_skips_nothing(tmp_path, monkeypatch):
    w = ReaderWorld(tmp_path, monkeypatch, reads=False, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and w.pc_ids_runs == []
    assert w.calls[0]["ids"] is None and w.calls[0]["windows"] is True and not w.calls[0].get("skip")
    assert "on the PC" not in result.suite_place


def test_a_selected_run_gives_no_skip(tmp_path, monkeypatch):
    from tests.test_issue247_routing import ids_of, select_ids
    w = ReaderWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(3), READER_A, OTHER])
    result = w.merge()
    assert result.ok and not w.calls[0].get("skip")  # unit 1: the readers left the ids, nothing to skip
    assert w.calls[0]["ids"] == [*ids_of(3), OTHER] and w.pc_ids_runs == [[READER_A]]


# -- the pieces the skip goes through ---------------------------------------------------------------------------------

def test_dispatch_sends_the_skip_input_encoded_like_ids():
    clock = Clock()
    gh = FakeGh(clock, None, list_runs=[run_row(12, 1)])
    github_run.dispatch(REPO, "i260", 5, gh, wall=lambda: BASE, sleep=clock.sleep, windows=True,
                        skip=[READER_A, READER_B])
    assert gh.calls[0] == ["workflow", "run", "tests.yml", "-R", REPO, "--ref", "i260", "--json"]
    assert json.loads(gh.stdins[0]) == {
        "ref": "i260", "parts": "5", "windows": "true", "skip": github_run.encode_ids([READER_A, READER_B])}


def _run_plugin(monkeypatch, tmp_path, listed_skip, nodes):
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "pc_only.txt").write_text("tests/test_win.py\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("\n".join(listed_skip) + "\n", encoding="utf-8")
    for name in ("TEST_SHARD", "TEST_IDS_FILE", "TEST_PC_ONLY", "TEST_SKIP_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_PC_ONLY", "1")
    monkeypatch.setenv("TEST_SKIP_FILE", str(tmp_path / "skip.txt"))
    items = [SimpleNamespace(nodeid=n) for n in nodes]
    deselected: list = []
    config = SimpleNamespace(rootpath=tmp_path, hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)))
    shard.pytest_record_collected_ids(config, items)
    shard.pytest_collection_modifyitems(config, items)
    return [i.nodeid for i in items]


def test_the_windows_run_drops_the_skipped_ids_and_keeps_the_other_pc_only_tests(monkeypatch, tmp_path):
    kept = _run_plugin(monkeypatch, tmp_path, ["tests/test_win.py::test_a"],
                       ["tests/test_win.py::test_a", "tests/test_win.py::test_b", "tests/test_x.py::test_c"])
    assert kept == ["tests/test_win.py::test_b"]


def test_a_windows_run_with_every_test_skipped_collects_nothing(monkeypatch, tmp_path):
    kept = _run_plugin(monkeypatch, tmp_path, ["tests/test_win.py::test_a"], ["tests/test_win.py::test_a"])
    assert kept == []


def test_the_workflow_gives_the_windows_job_the_skip_input_and_tolerates_nothing_collected():
    text = WORKFLOW.read_text(encoding="utf-8")
    windows = text[text.index("\n  windows:\n    if:"):]
    assert "skip:" in text.split("jobs:")[0] and "TEST_SKIP_FILE" in windows and "inputs.skip" in windows
    line = next(ln for ln in windows.splitlines() if "-eq 5" in ln)
    assert 'TEST_SKIP_FILE:-}" ]' in line


@pytest.fixture(autouse=True)
def _workflow_files_in_sync(monkeypatch):
    """#343 rule 1: a dispatch first makes the workflow files exist on the default branch. The tests here are about the
    dispatch's own calls, so the sync is answered as done (tests/test_issue343_workflow_sync.py tests the sync itself)."""
    monkeypatch.setattr(workflow_sync, "prepare_dispatch", lambda *args, **kwargs: [])
