"""#232 unit 4d (D119): the test files that run on the PC only (`tests/pc_only.txt`).

Seams: the shard plugin's `pytest_collection_modifyitems` (the file drops its listed files before it shards), the
file itself, and `run_merge` with the fakes of test_issue232_wiring (a GitHub that answers, a PC suite that is a fake):
a merge asks the GitHub Windows job for the PC-only files (#247) and the PC runs nothing beside GitHub; a failed PC-only id
is re-checked on GitHub; a GitHub fallback runs the whole suite on the PC once; an empty list asks no Windows job."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import test_shard_plugin as shard
from tools.agentops.pc_only import drop_pc_only, load_pc_only
from tests.test_issue232_wiring import BAD, GREEN, Fakes, World, _git, red
from tests.test_issue247_routing import PcWorld

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
    local = (REPO / "tests" / "pc_only.local.txt")  # #305: the repo's own list; load_pc_only is the union of both
    local_paths = {x.strip() for x in local.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")} if local.is_file() else set()
    for path in local_paths:
        assert (REPO / path).is_file(), path
    assert load_pc_only(REPO) == set(paths) | local_paths


# -- (c) the merge wiring (#247: the PC-only files run on the GitHub Windows job, never on the PC) -----------------

def test_github_green_with_the_windows_job_is_green_and_the_pc_runs_nothing(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []
    assert w.calls[0]["windows"] is True and w.calls[0]["ids"] is None
    assert result.suite_place.startswith("suite on GitHub: 3 machines")


def test_a_failing_pc_only_id_makes_the_merge_red_after_its_github_recheck(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=Fakes(red(ids=(PC_ID,)), later=[red(ids=(PC_ID,))]))
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [PC_ID] and PC_ID in result.message
    assert w.pc_runs == [] and w.rechecks == []
    assert [c["windows"] for c in w.calls] == [True, True] and w.calls[1]["ids"] == [PC_ID]
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_both_reds_name_both_ids(tmp_path, monkeypatch):
    both = red(ids=(BAD, PC_ID))
    w = PcWorld(tmp_path, monkeypatch, fakes=Fakes(both, later=[both]))
    result = w.merge()
    assert not result.ok and sorted(result.failed_tests) == sorted([BAD, PC_ID])
    assert w.calls[1]["ids"] == [BAD, PC_ID] and w.pc_runs == [] and w.rechecks == []


def test_a_pc_only_id_that_passes_its_github_recheck_lands_green(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=Fakes(red(ids=(PC_ID,)), later=[GREEN]))
    result = w.merge()
    assert result.ok and result.pushed and result.rechecked == [PC_ID] and w.pc_runs == []


def test_a_github_fallback_runs_the_whole_suite_on_the_pc_once(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, fakes=Fakes(LOST))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.pc_runs[0].count("@") == 0
    assert result.suite_place == "suite on the PC (GitHub lost)"


@pytest.mark.parametrize("listing", ["", "# nothing here" + chr(10) * 2, "tests/test_gone.py" + chr(10)])
def test_an_empty_list_or_files_absent_from_the_tree_ask_no_windows_job(tmp_path, monkeypatch, listing):
    w = PcWorld(tmp_path, monkeypatch, listing=listing)
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.calls[0]["windows"] is False


def test_no_pc_only_file_changes_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.calls[0]["windows"] is False


def test_the_setting_off_asks_github_nothing(tmp_path, monkeypatch):
    w = PcWorld(tmp_path, monkeypatch, github=False)
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.calls == []
