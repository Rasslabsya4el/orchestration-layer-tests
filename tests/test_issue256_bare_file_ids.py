"""Issue #256: an id sent as a bare test file path selects every test of that file.

The merge's selection (tools/agentops/test_select.py, ``Selection.ids``) sends the node ids of mapped tests plus the
bare path of every added or changed test file. Run 39332923246 (merge-suite/i251, 2026-10-19) sent 10 such paths among 97
ids; the shard plugin compared them to node ids only, found them "not collected" and raised ``pytest.UsageError`` in
every xdist worker, which xdist reports as ``INTERNALERROR ... assert not crashitem`` and "no tests ran". Fake items in
the pattern of tests/test_issue247_ids_collected.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_issue247_ids_collected import IDS, PC_ONLY, PLAIN, SHARD, WIN, ids, plain, run, win

OTHER = "tests/test_other.py"


def other(count: int) -> list:
    from types import SimpleNamespace
    return [SimpleNamespace(nodeid=f"{OTHER}::test_{k:03d}") for k in range(count)]


def test_a_bare_file_path_keeps_every_test_of_that_file(tmp_path, monkeypatch):
    items = plain(6) + other(4)
    kept, config = run(monkeypatch, tmp_path, {SHARD: "1/1"}, items, [OTHER, ids(plain(6))[0]])
    assert sorted(ids(kept)) == sorted(ids(other(4)) + [ids(plain(6))[0]])
    assert len(config.deselected) == 5


def test_a_bare_file_path_alone_is_the_whole_file_not_zero_tests(tmp_path, monkeypatch):
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "1/1"}, plain(6) + other(4), [OTHER])
    assert ids(kept) == ids(other(4))


def test_a_bare_file_path_is_split_between_the_parts_by_its_tests(tmp_path, monkeypatch):
    first, _ = run(monkeypatch, tmp_path, {SHARD: "1/2"}, plain(6) + other(4), [OTHER])
    second, _ = run(monkeypatch, tmp_path, {SHARD: "2/2"}, plain(6) + other(4), [OTHER])
    assert sorted(ids(first) + ids(second)) == sorted(ids(other(4))) and len(first) == len(second) == 2


def test_a_bare_file_path_with_no_collected_test_is_still_a_usage_error(tmp_path, monkeypatch):
    with pytest.raises(pytest.UsageError, match=r"1 of 2") as caught:
        run(monkeypatch, tmp_path, {SHARD: "1/1"}, plain(6), [ids(plain(6))[0], "tests/test_gone.py"])
    assert "tests/test_gone.py" in str(caught.value)


def test_a_bare_pc_only_path_is_not_required_on_a_linux_part_and_whole_on_the_windows_run(tmp_path, monkeypatch):
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "1/1"}, plain(6), [ids(plain(6))[0], WIN])
    assert ids(kept) == [ids(plain(6))[0]]
    kept, _ = run(monkeypatch, tmp_path, {PC_ONLY: "1"}, plain(6) + win(), [WIN])
    assert ids(kept) == ids(win())


def test_a_node_id_is_not_taken_for_a_path_prefix(tmp_path, monkeypatch):
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "1/1"}, plain(3), [f"{PLAIN}::test_001"])
    assert ids(kept) == [f"{PLAIN}::test_001"]


def test_a_test_run_under_the_workflows_env_sees_no_shard_variables(tmp_path):
    """The workflow's pytest step sets TEST_SHARD and TEST_IDS_FILE for the whole process, so the tests of the plugin
    itself (their subprocess pytest runs and their own hook calls) run with them set. Run 39332923246's selection held
    tests/test_issue232_shard.py as a bare path: with the variables leaking, 3 of its tests failed on GitHub."""
    repo = Path(__file__).resolve().parents[1]
    ids = tmp_path / "ids.txt"
    ids.write_text("tests/test_issue232_shard.py\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "TEST_PC_ONLY")}
    env.update(TEST_SHARD="1/1", TEST_IDS_FILE=str(ids), PYTHONPATH=str(repo))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_issue232_shard.py", "-q", "-p", "tools.agentops.test_shard_plugin",
         "-p", "no:cacheprovider", "-p", "no:faulthandler"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stdout[-1500:]
