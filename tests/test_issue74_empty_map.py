"""#74 unit 1: a map that holds 0 tests is no map: ``_select`` returns the full suite with reason ``map holds 0 tests``.

Seam: ``select_for_diff`` on a map built from the small repo of ``tests/test_select_build_map.py``."""
import sqlite3

from tests.test_select_build_map import SUITE, _git, _repo
from tools.agentops.test_select import EMPTY_MAP_REASON, build_map, map_dir, read_meta, select_for_diff, write_meta


def _built_repo(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    return repo, sha, mdir


def _change_a_module(repo):
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # changed\n")
    _git(repo, "commit", "-q", "-am", "c2")
    return _git(repo, "rev-parse", "HEAD")


def test_meta_total_zero_with_a_datafile_runs_the_full_suite(tmp_path):
    repo, sha, mdir = _built_repo(tmp_path)
    write_meta(mdir, {**read_meta(mdir), "total": 0})
    assert (mdir / "testmondata").is_file()
    head = _change_a_module(repo)
    sel = select_for_diff(repo, sha, head, mdir)
    assert EMPTY_MAP_REASON == "map holds 0 tests"
    assert sel.full is True
    assert sel.reason == "map holds 0 tests"
    assert sel.line == f"full suite: map holds 0 tests (map {sha[:8]})"  # #74 unit 2: the line names the map


def test_datafile_with_no_test_runs_the_full_suite_even_when_meta_total_is_positive(tmp_path):
    repo, sha, mdir = _built_repo(tmp_path)
    (mdir / "testmondata").unlink()
    con = sqlite3.connect(str(mdir / "testmondata"))
    con.executescript("create table test_execution (id integer primary key, test_name text);"
                      "create table test_execution_file_fp (test_execution_id integer, fingerprint_id integer);"
                      "create table file_fp (id integer primary key, filename text);")
    con.commit()
    con.close()
    head = _change_a_module(repo)
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full is True
    assert sel.reason == "map holds 0 tests"


def test_real_map_with_tests_keeps_its_selection(tmp_path):
    repo, sha, mdir = _built_repo(tmp_path)
    assert read_meta(mdir)["total"] == 4
    head = _change_a_module(repo)
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full is False
    assert sel.reason == ""
    assert sel.ids == ["tests/test_a.py::test_a"]
    assert sel.line == f"selected 1 of 4 tests (map {sha[:8]})"
