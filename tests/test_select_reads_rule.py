"""#195 unit 2: a data change selects the tests that read a changed file (the recorded reads map), plus the testmon
selection of the code change; a data file the map never saw read, or no reads map at all, is the full suite."""
import json

from tests.test_select_build_map import SUITE, _git, _repo
from tools.agentops.test_select import build_map, map_dir, select_for_diff

DOC = "docs/agents/orch.md"


def _map_with_reads(tmp_path, reads: dict[str, list[str]] | None):
    """A built map of the tmp repo; ``reads`` (test id -> files it opened) is written as the reads dir, None = no dir."""
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    if reads is not None:
        (mdir / "reads").mkdir()
        (mdir / "reads" / "reads-0.json").write_text(json.dumps({"files": reads, "dirs": {}}), encoding="utf-8")
    return repo, sha, mdir


def _commit(repo, files: dict[str, str]):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "c2")
    return _git(repo, "rev-parse", "HEAD")


def test_data_file_read_by_one_test_selects_that_test_only(tmp_path):
    repo, sha, mdir = _map_with_reads(tmp_path, {"tests/test_a.py::test_a": [DOC]})
    head = _commit(repo, {DOC: "touched\n"})
    sel = select_for_diff(repo, sha, head, mdir)
    assert not sel.full, sel.line
    assert sel.ids == ["tests/test_a.py::test_a"]
    assert sel.line == f"selected 1 of 4 tests (map {sha[:8]}); selected by reads: 1 tests for 1 data files"


def test_data_file_absent_from_the_reads_map_runs_the_full_suite(tmp_path):
    repo, sha, mdir = _map_with_reads(tmp_path, {"tests/test_a.py::test_a": [DOC]})
    head = _commit(repo, {DOC: "touched\n", "docs/agents/new.md": "new\n"})
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full
    assert sel.line == "full suite: data change"


def test_no_reads_dir_runs_the_full_suite_on_a_data_change(tmp_path):
    repo, sha, mdir = _map_with_reads(tmp_path, None)
    head = _commit(repo, {DOC: "touched\n"})
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full
    assert sel.line == "full suite: data change"


def test_data_file_plus_a_code_file_selects_the_union(tmp_path):
    repo, sha, mdir = _map_with_reads(tmp_path, {"tests/test_a.py::test_a": [DOC]})
    head = _commit(repo, {DOC: "touched\n", "pkg/b.py": "def b():\n    return 2  # touched\n"})
    sel = select_for_diff(repo, sha, head, mdir)
    assert not sel.full, sel.line
    assert sel.ids == ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    assert sel.line == f"selected 2 of 4 tests (map {sha[:8]}); selected by reads: 1 tests for 1 data files"
