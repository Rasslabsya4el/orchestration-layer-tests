"""#231 unit 3 (D119): `python -m tools.agentops test-copy plan`, the run plan of a merge.

Tested here: row 3 of the issue (a test that reads an excluded file is listed as PC-only and the plan shows it apart):
a fake reads dir with three tests, one of which reads ``docs/research/x.md``, gives ``GitHub: 2 tests`` and
``PC-only: 1 test`` and names that test with its path. A test that lists a folder holding an excluded file is PC-only
(``docs/research/ (lists excluded: x.md)``); one that lists ``tests`` is not. The pure function ``pc_only_tests`` on
hand-built dicts, a missing reads dir (``no reads data: <path>``, exit 0), the 50-line cap on the PC-only block, and
the closing line ``tests without reads data are not counted``."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from tools.agentops import test_copy_plan as tp
from tools.agentops.test_copy_cmd import run_test_copy_command

NOT_COUNTED = "tests without reads data are not counted\n"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _reads(tmp_path: Path, files: dict, dirs: dict | None = None) -> Path:
    """A reads dir shaped as suite_reads_plugin writes it: one ``reads-<worker>-<pid>.json`` dump."""
    reads = tmp_path / "reads"
    reads.mkdir()
    dump = {"files": files, "dirs": dirs if dirs is not None else {test: [] for test in files}}
    (reads / "reads-main-1.json").write_text(json.dumps(dump), encoding="utf-8")
    return reads


def _plan(repo: Path, reads: Path, capsys) -> tuple[int, str]:
    rc = run_test_copy_command(argparse.Namespace(test_copy_command="plan", root=repo, reads_dir=reads))
    return rc, capsys.readouterr().out


def test_plan_shows_the_test_reading_an_excluded_file_apart(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/a.py": "A = 1\n", "src/b.py": "B = 1\n", "docs/research/x.md": "note\n"})
    reads = _reads(tmp_path, {
        "tests/test_a.py::test_one": ["src/a.py"],
        "tests/test_b.py::test_two": ["src/b.py"],
        "tests/test_c.py::test_three": ["docs/research/x.md", "src/a.py"],
    })
    rc, out = _plan(repo, reads, capsys)
    assert rc == 0
    assert out == ("GitHub: 2 tests\n"
                   "PC-only: 1 test\n"
                   "  tests/test_c.py::test_three: docs/research/x.md\n"
                   + NOT_COUNTED)


def test_listing_a_folder_with_an_excluded_file_is_pc_only_and_listing_tests_is_not(tmp_path, capsys):
    repo = _repo(tmp_path, {"tests/test_a.py": "def test_a():\n    pass\n", "src/a.py": "A = 1\n",
                            "docs/research/x.md": "note\n"})
    reads = _reads(
        tmp_path,
        files={"tests/test_docs_list.py::test_docs": [], "tests/test_tests_list.py::test_tests": []},
        dirs={"tests/test_docs_list.py::test_docs": ["docs/research"],
              "tests/test_tests_list.py::test_tests": ["tests"]},
    )
    rc, out = _plan(repo, reads, capsys)
    assert rc == 0
    assert out == ("GitHub: 1 test\n"
                   "PC-only: 1 test\n"
                   "  tests/test_docs_list.py::test_docs: docs/research/ (lists excluded: x.md)\n"
                   + NOT_COUNTED)


def test_missing_reads_dir_prints_no_data_and_exits_0(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/a.py": "A = 1\n"})
    missing = tmp_path / "no-such-reads"
    rc, out = _plan(repo, missing, capsys)
    assert rc == 0
    assert out == f"no reads data: {missing}\n"


def test_pure_function_lists_each_missing_path_sorted_and_skips_clean_tests():
    reads = {
        "tests/a.py::t1": ["src/a.py"],
        "tests/b.py::t2": ["docs/research/x.md", "src/b.py"],
        "tests/c.py::t3": [],
        "tests/d.py::t4": ["untracked/notes.txt", "src/d.py", "docs/research/x.md"],
    }
    snapshot = {"src/a.py", "src/b.py", "src/d.py"}
    assert tp.pc_only_tests(reads, snapshot) == {
        "tests/b.py::t2": ["docs/research/x.md"],
        "tests/d.py::t4": ["docs/research/x.md", "untracked/notes.txt"],
    }


def test_pure_function_folder_entry_names_the_first_excluded_file():
    tracked = {"docs/research/b.md", "docs/research/a.md", "docs/research/c.md", "tests/test_a.py", "src/a.py"}
    snapshot = {"tests/test_a.py", "src/a.py", "docs/research/c.md"}
    reads = {"tests/x.py::t1": [], "tests/x.py::t2": [], "tests/x.py::t3": []}
    dir_reads = {"tests/x.py::t1": ["docs/research"], "tests/x.py::t2": ["tests"], "tests/x.py::t3": ["src"]}
    assert tp.pc_only_tests(reads, snapshot, dir_reads, tracked) == {
        "tests/x.py::t1": ["docs/research/ (lists excluded: a.md)"],
    }


def test_plan_caps_the_pc_only_lines_at_50_and_counts_the_rest(tmp_path, capsys):
    repo = _repo(tmp_path, {"src/a.py": "A = 1\n"})
    reads = _reads(tmp_path, {f"tests/test_many.py::test_{i:02d}": ["docs/research/x.md"] for i in range(52)})
    rc, out = _plan(repo, reads, capsys)
    lines = out.splitlines()
    assert rc == 0
    assert lines[0] == "GitHub: 0 tests"
    assert lines[1] == "PC-only: 52 tests"
    assert len(lines) == 2 + 50 + 1 + 1
    assert lines[-2] == "... 2 more PC-only tests not shown"
    assert lines[-1] == "tests without reads data are not counted"
