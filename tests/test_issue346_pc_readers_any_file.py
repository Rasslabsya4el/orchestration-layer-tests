"""#346 unit 3: a test that reads a file the public test copy does not take runs on the PC whatever file it sits in, not
only in a PC-only file (``_pc_readers`` takes every id the reads data names). The test is no GitHub id; the place says
``(+ N tests on the PC)`` as #260 says it; the Windows job decision stays as it was.

Seam: `run_merge` with the fakes of test_issue232_wiring (a GitHub that answers from a queue; the spy on
`run_github_suite` shows what each merge asked GitHub for); the PC run of the readers is a fake `_run_ids_on_pc`."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_issue232_wiring import World, _git
from tests.test_issue247_routing import GREEN, QFakes, ids_of, select_ids
from tools.agentops import merge as merge_module
from tools.agentops.test_select import map_dir

TEST_X = "tests/test_a.py::test_x"  # reads scripts/secret.py, a file the copy does not take; test_a.py is in no PC-only list
TEST_Y = "tests/test_a.py::test_y"  # reads pkg/a.py only
EXCLUDED = "scripts/secret.py"


class AnyFileWorld(World):
    """A World whose worker branch holds tests/test_a.py (in no PC-only list) and tests/test_win.py (PC-only), and whose
    reads data names the excluded file for test_x alone."""

    def __init__(self, tmp_path, monkeypatch, *, fakes):
        super().__init__(tmp_path, monkeypatch, fakes=fakes)
        worker = tmp_path / "worker"
        _git(worker, "checkout", "-q", "feat")
        (worker / "tests").mkdir()
        (worker / "tests" / "test_a.py").write_text("def test_x():\n    pass\n\ndef test_y():\n    pass\n", encoding="utf-8")
        (worker / "tests" / "test_win.py").write_text("def test_boom():\n    pass\n", encoding="utf-8")
        (worker / "tests" / "pc_only.txt").write_text("tests/test_win.py\n", encoding="utf-8")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", "any file reader")
        _git(worker, "push", "-q", "origin", "feat")
        reads_dir = map_dir(self.checkout) / "reads"
        reads_dir.mkdir(parents=True)
        dump = {"files": {TEST_X: [EXCLUDED], TEST_Y: ["pkg/a.py"]}, "dirs": {}}
        (reads_dir / "reads-main-1.json").write_text(json.dumps(dump), encoding="utf-8")
        self.pc_ids_runs: list[list[str]] = []

        def run_ids(suite_cmd, ids, suite_dir, run_env):
            self.pc_ids_runs.append(list(ids))
            return True, "ok\n"
        monkeypatch.setattr(merge_module, "_run_ids_on_pc", run_ids)


def test_a_full_run_sends_a_reader_outside_the_pc_only_list_to_the_pc(tmp_path, monkeypatch):
    w = AnyFileWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert len(w.calls) == 1
    assert w.calls[0]["ids"] is None and w.calls[0]["skip"] == [TEST_X]
    assert w.calls[0]["windows"] is True  # test_win.py is PC-only and exists: the Windows job decision is unchanged
    assert w.pc_ids_runs == [[TEST_X]]
    assert result.suite_place.startswith("suite on GitHub: ") and result.suite_place.endswith("(+ 1 test on the PC)")


def test_a_selected_reader_outside_the_pc_only_list_leaves_the_github_ids(tmp_path, monkeypatch):
    w = AnyFileWorld(tmp_path, monkeypatch, fakes=QFakes(GREEN))
    select_ids(monkeypatch, [*ids_of(3), TEST_X, TEST_Y])
    result = w.merge()
    assert result.ok and result.pushed
    assert len(w.calls) == 1
    assert w.calls[0]["ids"] == [*ids_of(3), TEST_Y] and w.calls[0]["windows"] is False
    assert w.pc_ids_runs == [[TEST_X]]
    assert result.suite_place.endswith("(+ 1 test on the PC)")


def test_the_linux_job_of_the_workflow_deselects_the_skip_ids_too():
    """#346: a reader outside the PC-only files runs on a Linux part in a full run, so that job needs the skip file."""
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    linux = text.split("\n  windows:", 1)[0]
    assert 'echo "TEST_SKIP_FILE=$RUNNER_TEMP/skip.txt" >> "$GITHUB_ENV"' in linux
    assert linux.index("TEST_SKIP_FILE") < linux.index("- name: pytest part")
