"""#232 unit 4d (D119): the test files that run on the PC only (`tests/pc_only.txt`).

Seams: the shard plugin's `pytest_collection_modifyitems` (the file drops its listed files before it shards), the
file itself, and `run_merge` with the fakes of test_issue232_wiring (a GitHub that answers, a PC suite that is a fake):
GitHub green + PC-only green is green; a PC-only run that fails for any reason (test ids, a crash, a stall) is a red
merge for good, the PC re-check of GitHub's own ids never absorbs it; a GitHub fallback runs the whole suite on the PC and
no second run beside it; no `<out>.pconly*` file outlives a merge; an empty list starts no extra run."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import machine_berths, merge as merge_module, merge_machine, test_shard_plugin as shard
from tools.agentops.merge import PcOnly, SuiteRun, _PcOnlyRun
from tools.agentops.pc_only import drop_pc_only, load_pc_only
from tests.test_issue232_wiring import BAD, Fakes, World, _git, red

REPO = Path(__file__).resolve().parents[1]
PC_ID = "tests/test_win.py::test_boom"
LOST = {"run_id": 79, "parts": [], "minutes_used": 1, "outcome": "lost", "failed_ids": [], "full_recheck": False}
CRASH = "Traceback (most recent call last):\nOSError: disk gone\n"


# -- (a) the shard plugin ----------------------------------------------------------------------------------------

def _items() -> list:
    names = ["tests/pc_a.py::t0", "tests/pc_b.py::t0"] + [f"tests/test_x.py::t{k}" for k in range(8)]
    return [SimpleNamespace(nodeid=n) for n in names]


class _Config:
    def __init__(self, root: Path) -> None:
        self.rootpath = root
        self.deselected: list = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def _parts(monkeypatch, root: Path, n: int) -> tuple[list[list[str]], list[str]]:
    kept, dropped = [], []
    for i in range(1, n + 1):
        monkeypatch.setenv(shard.ENV_SHARD, f"{i}/{n}")
        config, items = _Config(root), _items()
        shard.pytest_collection_modifyitems(config, items)
        kept.append([x.nodeid for x in items])
        dropped += [x.nodeid for x in config.deselected]
    return kept, dropped


def test_the_shard_drops_the_pc_only_files_first_and_shards_the_rest(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "pc_only.txt").write_text("# win\n\ntests/pc_a.py\ntests/pc_b.py\n", encoding="utf-8")
    kept, dropped = _parts(monkeypatch, tmp_path, 2)
    union = kept[0] + kept[1]
    assert sorted(union) == sorted(f"tests/test_x.py::t{k}" for k in range(8))  # all the other 8, none of the 2
    assert len(union) == len(set(union))  # disjoint
    assert [len(p) for p in kept] == [4, 4]  # the 8 are cut by position among themselves
    assert {"tests/pc_a.py::t0", "tests/pc_b.py::t0"} <= set(dropped)


def test_without_a_pc_only_file_all_ten_items_are_spread_over_the_parts(tmp_path, monkeypatch):
    kept, _ = _parts(monkeypatch, tmp_path, 2)
    union = kept[0] + kept[1]
    assert sorted(union) == sorted(x.nodeid for x in _items()) and len(union) == 10


def test_one_of_one_drops_the_pc_only_files_and_keeps_the_rest(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "pc_only.txt").write_text("tests/pc_a.py\n", encoding="utf-8")
    monkeypatch.setenv(shard.ENV_SHARD, "1/1")
    items = _items()
    shard.pytest_collection_modifyitems(_Config(tmp_path), items)
    names = [x.nodeid for x in items]
    assert "tests/pc_a.py::t0" not in names  # the listed file is dropped
    assert sorted(names) == sorted(["tests/pc_b.py::t0"] + [f"tests/test_x.py::t{k}" for k in range(8)])  # the rest is kept


# -- (e) a real pytest run in a tmp project (the plugin driven by `-p`, no mock) ----------------------------------------

PLUGIN = "tools.agentops.test_shard_plugin"


def _project(root: Path) -> None:
    """A tmp project: a file that raises at import and is listed PC-only, a listed file that does not raise, and 3 ok tests."""
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")  # the rootdir: the plugin reads tests/pc_only.txt from it
    (root / "tests" / "pc_only.txt").write_text("tests/test_boom.py\ntests/test_listed.py\n", encoding="utf-8")
    (root / "tests" / "test_boom.py").write_text('raise RuntimeError("boom at import")\n', encoding="utf-8")
    (root / "tests" / "test_listed.py").write_text("def test_listed_a():\n    pass\n\n\ndef test_listed_b():\n    pass\n",
                                                   encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text("def test_ok_a():\n    pass\n\n\ndef test_ok_b():\n    pass\n\n\n"
                                               "def test_ok_c():\n    pass\n", encoding="utf-8")


def _run_pytest(root: Path, shard: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("TEST_SHARD", "PYTEST_ADDOPTS", "PYTHONPATH")}
    env["PYTHONPATH"] = str(REPO)  # the plugin is importable as tools.agentops.test_shard_plugin in the subprocess
    if shard is not None:
        env["TEST_SHARD"] = shard
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", PLUGIN, "-p", "no:cacheprovider"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("shard", ["1/1", "1/3", "2/5"])
def test_a_real_run_with_pc_only_files_listed_is_green_and_never_imports_them(tmp_path, shard):
    _project(tmp_path)
    run = _run_pytest(tmp_path, shard)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert "error" not in out.lower() and "boom" not in out
    assert "passed" in out


def test_one_of_one_runs_the_listed_non_raising_file_nowhere(tmp_path):
    _project(tmp_path)
    run = _run_pytest(tmp_path, "1/1")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "3 passed" in run.stdout  # test_ok only: the 2 tests of test_listed.py are not collected
    assert "listed" not in run.stdout


def test_the_parts_of_three_and_five_share_the_three_ok_tests(tmp_path):
    _project(tmp_path)
    assert "1 passed, 2 deselected" in _run_pytest(tmp_path, "1/3").stdout  # position 0 of the 3 sorted ids
    assert "1 passed, 2 deselected" in _run_pytest(tmp_path, "2/5").stdout  # position 1 of the 3 sorted ids


def test_unset_the_boom_file_is_a_collection_error(tmp_path):
    _project(tmp_path)
    run = _run_pytest(tmp_path, None)
    out = run.stdout + run.stderr
    assert run.returncode != 0 and "boom at import" in out  # the fixture bites when nothing drops the file


def test_load_and_drop_are_pure(tmp_path):
    assert load_pc_only(tmp_path) == set()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "pc_only.txt").write_text("# c\ntests/pc_a.py\n\n  tests/pc_b.py  \n", encoding="utf-8")
    names = load_pc_only(tmp_path)
    assert names == {"tests/pc_a.py", "tests/pc_b.py"}
    kept, dropped = drop_pc_only(_items(), names)
    assert [x.nodeid for x in dropped] == ["tests/pc_a.py::t0", "tests/pc_b.py::t0"] and len(kept) == 8


# -- (b) the list itself -----------------------------------------------------------------------------------------

def test_every_listed_pc_only_file_exists_and_is_listed_once():
    lines = (REPO / "tests" / "pc_only.txt").read_text(encoding="utf-8").splitlines()
    paths = [x for x in lines if x.strip() and not x.startswith("#")]
    assert paths and len(set(paths)) == len(paths)
    for path in paths:
        assert (REPO / path).is_file(), path
    assert load_pc_only(REPO) == set(paths)


# -- (c) the merge wiring ----------------------------------------------------------------------------------------

class PcWorld(World):
    """A World whose worker branch carries tests/pc_only.txt and the files it lists; a pc-only run is told from the
    whole-suite run by its `@args` file, and answers ``pc_result`` (or waits for the cancel when ``hang``)."""

    def __init__(self, tmp_path, monkeypatch, *, listing="tests/test_win.py\n", pc_result=None, hang=False, **kw):
        self.pc_only_runs: list[list[str]] = []
        self.pc_result = pc_result or SuiteRun(True, "5 passed in 1s", 0)
        self.hang, self.started, self.cancelled = hang, threading.Event(), 0
        fakes = kw.pop("fakes", None) or Fakes()
        if hang:
            real_poll = fakes.poll

            def poll(*a, **k):  # GitHub answers only once the PC-only run is under way
                assert self.started.wait(20)
                return real_poll(*a, **k)
            fakes.poll = poll  # before the World binds the fakes' callables
        super().__init__(tmp_path, monkeypatch, fakes=fakes, **kw)
        worker = tmp_path / "worker"
        _git(worker, "checkout", "-q", "feat")
        (worker / "tests").mkdir()
        (worker / "tests" / "test_win.py").write_text("def test_boom():\n    pass\n", encoding="utf-8")
        (worker / "tests" / "pc_only.txt").write_text(listing, encoding="utf-8")
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", "pc only")
        _git(worker, "push", "-q", "origin", "feat")

    def _pc_suite(self, run_cmd, *a, **kw):
        found = re.search(r'"@([^"]+)"', run_cmd)
        if not found:
            return super()._pc_suite(run_cmd, *a, **kw)
        self.pc_only_runs.append(Path(found.group(1)).read_text(encoding="utf-8").split())
        if self.hang:
            self.started.set()
            check = kw.get("cut_check")
            for _ in range(400):
                if check is not None and check():
                    self.cancelled += 1
                    return SuiteRun(False, "", 0, cut_for="x")
                threading.Event().wait(0.05)
        return self.pc_result

    def leftovers(self) -> list[str]:
        """The PC-only output files (output, args, plan, events) left in the suite output folder."""
        return sorted(x.name for x in (self.tmp / "suite_out").glob("*pconly*"))


def test_github_green_and_pc_only_green_is_green_and_records_the_count(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.pc_only_runs == [["tests/test_win.py"]]
    assert result.suite_place.startswith("suite on GitHub: 3 machines") and result.suite_pc_only == 5
    assert w.leftovers() == []


def test_github_green_and_pc_only_red_is_red_naming_the_id(tmp_path, monkeypatch):
    bad = SuiteRun(False, f"=== short test summary info ===\nFAILED {PC_ID} - boom\n1 failed, 2 passed in 1s\n", 0,
                   failed_tests=[PC_ID])
    w = PcWorld(tmp_path, monkeypatch, pc_result=bad)
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [PC_ID] and PC_ID in result.message
    assert w.rechecks == [] and w.pc_runs == []  # a PC-only id is not re-checked
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"
    assert w.leftovers() == []


def test_a_pc_only_id_that_passes_a_pc_recheck_is_still_red_for_good(tmp_path, monkeypatch):
    bad = SuiteRun(False, f"=== short test summary info ===\nFAILED {PC_ID}\n", 0, failed_tests=[PC_ID])
    w = PcWorld(tmp_path, monkeypatch, pc_result=bad, fakes=Fakes(red()))
    w.recheck_still = []  # every id handed to the PC re-check passes
    result = w.merge()
    assert w.rechecks == [[BAD]]  # GitHub's own id passes its re-check ...
    assert not result.ok and not result.pushed and result.reason == "red-suite"  # ... the PC-only red stands
    assert result.failed_tests == [PC_ID] and PC_ID in result.message
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_both_reds_name_both_ids(tmp_path, monkeypatch):
    bad = SuiteRun(False, f"=== short test summary info ===\nFAILED {PC_ID}\n1 failed\n", 0, failed_tests=[PC_ID])
    w = PcWorld(tmp_path, monkeypatch, pc_result=bad, fakes=Fakes(red()))
    w.recheck_still = [BAD]
    result = w.merge()
    assert not result.ok and sorted(result.failed_tests) == sorted([BAD, PC_ID])
    assert w.rechecks == [[BAD]]  # the re-check gets only GitHub's id
    assert w.leftovers() == []


def test_github_red_that_passes_the_recheck_plus_a_pc_only_crash_is_red(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, pc_result=SuiteRun(False, CRASH, 0), fakes=Fakes(red()))
    w.recheck_still = []
    result = w.merge()
    assert w.rechecks == [[BAD]]
    assert not result.ok and not result.pushed and result.reason == "red-suite"
    assert "pc-only run failed: OSError: disk gone" in result.message
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_github_green_and_a_pc_only_crash_is_red_naming_the_failure(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, pc_result=SuiteRun(False, CRASH, 0))
    result = w.merge()
    assert not result.ok and not result.pushed and result.reason == "red-suite"
    assert "pc-only run failed: OSError: disk gone" in result.message
    assert w.rechecks == [] and w.leftovers() == []


def test_a_stalled_pc_only_run_is_a_stuck_merge(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, pc_result=SuiteRun(False, "", 0, stuck="no output for 10 minutes"))
    result = w.merge()
    assert not result.ok and result.reason == "stuck" and not result.pushed
    assert "PC-only tests" in result.message and "no output for 10 minutes" in result.message
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_a_github_fallback_runs_the_whole_suite_on_the_pc_and_cancels_the_extra_run(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, hang=True, fakes=Fakes(LOST))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.pc_runs[0].count("@") == 0
    assert w.cancelled == 1 and result.suite_pc_only is None
    assert result.suite_place == "suite on the PC (GitHub lost)"
    assert w.leftovers() == []


@pytest.mark.parametrize("listing", ["", "# nothing here\n\n", "tests/test_gone.py\n"])
def test_an_empty_list_or_files_absent_from_the_tree_start_no_extra_run(tmp_path, monkeypatch, listing):
    w = PcWorld(tmp_path, monkeypatch, listing=listing)
    result = w.merge()
    assert result.ok and w.pc_only_runs == [] and w.pc_runs == [] and result.suite_pc_only is None


def test_no_pc_only_file_changes_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and w.pc_runs == [] and result.suite_pc_only is None


def test_the_setting_off_starts_no_extra_run(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, github=False)
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.pc_only_runs == [] and result.suite_pc_only is None


# -- (d) the size line and the berth of a cancelled run ----------------------------------------------------------

def test_a_pc_only_line_is_a_partial_run_of_its_file_count(tmp_path):
    assert merge_machine.run_size("pc-only tests: 3 files", tmp_path) == (3, False)


def test_a_cancelled_pc_only_run_waiting_for_a_berth_leaves_the_wait_and_holds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setattr(merge_machine, "configured_berths", lambda: 1)
    monkeypatch.setattr(merge_module, "machine_holds", lambda: False)
    monkeypatch.setattr(merge_machine, "run_size", lambda line, cwd: (600, False))  # heavy: it needs a berth
    real_run_heavy = merge_machine.run_heavy
    monkeypatch.setattr(merge_machine, "run_heavy", lambda *a, **k: real_run_heavy(*a, poll=0.01, **k))
    holder = machine_berths.acquire(tmp_path / "repoA", "other-merge", 4251, berths=1)  # the only berth is taken
    ran = []
    plan = PcOnly(["tests/test_win.py"], tmp_path / "out" / "m.pconly", lambda cancelled: ran.append(1))
    run = _PcOnlyRun(plan, tmp_path / "repoB", "feat", False, tmp_path / "merge.log")
    try:
        for _ in range(500):  # bounded: it must be seen waiting
            if machine_berths.waiting_rows():
                break
            threading.Event().wait(0.02)
        assert len(machine_berths.waiting_rows()) == 1
        run.stop(10)
        assert not run.thread.is_alive() and ran == []
        assert isinstance(run.error, merge_machine.WaitStopped) and run.result is None
        assert machine_berths.waiting_rows() == []  # it left the queue
        assert list((tmp_path / "out").glob("*pconly*")) == []
    finally:
        holder.release()
    again = machine_berths.acquire(tmp_path / "repoC", "next-merge", 4251, berths=1, poll=0.01)
    assert len(machine_berths.holders()) == 1  # only the new one: the cancelled run took no berth
    again.release()
