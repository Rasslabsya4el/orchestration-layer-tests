"""#319: `red-on-base` lists the tests red on a base; the plugin tools/agentops/red_on_main_plugin.py skips them.

Rows run on sample repos (folders with tests, outside this repo) through `python -m tools.agentops red-on-base`
and through pytest with the plugin loaded by the sample's conftest.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


pytestmark = pytest.mark.usefixtures("github_suite_off")  # #343 u6: no run here reaches GitHub

REPO = Path(__file__).resolve().parents[1]
CONFTEST = REPO / "tests" / "conftest.py"
PLUGIN = "tools.agentops.red_on_main_plugin"
SAMPLE_CONFTEST = f'pytest_plugins = ["{PLUGIN}"]\n'


SAMPLE_TESTS = textwrap.dedent('''
    def test_red_one():
        assert False

    def test_red_two():
        assert 1 == 2

    def test_red_three():
        raise RuntimeError("red on base")

    def test_green():
        assert True
''')
EXTRA_RED = "\n\ndef test_red_four():\n    assert False\n"
RED_IDS = [
    "tests/test_sample.py::test_red_one",
    "tests/test_sample.py::test_red_two",
    "tests/test_sample.py::test_red_three",
]

# a param id with '#', a fixture ERROR, a failed and an errored test in one pass, a module that raises at import
RICH_FILES = {
    "tests/test_hash.py": textwrap.dedent('''
        import pytest

        @pytest.mark.parametrize("value", ["a#b", "c"])
        def test_param(value):
            assert False
    '''),
    "tests/test_mixed.py": textwrap.dedent('''
        import pytest

        @pytest.fixture
        def boom():
            raise RuntimeError("fixture broke")

        def test_error_in_setup(boom):
            pass

        def test_failed_in_call():
            assert 1 == 2
    '''),
    "tests/test_bad.py": 'raise RuntimeError("cannot import")\n',
    "tests/test_green.py": "def test_green():\n    assert True\n",
}
# addopts -x would stop the run at the first red: the base run must not inherit it
RICH_INI = "[pytest]\naddopts = -x\n"
RICH_IDS = [
    "tests/test_hash.py::test_param[a#b]",
    "tests/test_hash.py::test_param[c]",
    "tests/test_mixed.py::test_error_in_setup",
    "tests/test_mixed.py::test_failed_in_call",
    "tests/test_bad.py",
]


def _sample(tmp_path: Path, name: str, files: dict[str, str] | None = None, ini: str | None = None) -> Path:
    root = tmp_path / name
    (root / "tests").mkdir(parents=True)
    for rel, text in (files or {"tests/test_sample.py": SAMPLE_TESTS}).items():
        (root / rel).write_text(text, encoding="utf-8")
    if ini is not None:
        (root / "pytest.ini").write_text(ini, encoding="utf-8")
    return root


def _list_file(root: Path) -> Path:
    return root / "tests" / "red_on_main.txt"


def _env() -> dict[str, str]:
    # the samples need no third-party pytest plugin: autoload off keeps each pytest start near 0.4 s (3 s with it),
    # and the plugin under test is loaded by name either way; a real run keeps autoload on
    # the merge suite and prequeue run pytest with PYTEST_ADDOPTS naming suite plugins: that must not reach the base run
    return dict(os.environ, PYTHONPATH=str(REPO), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                PYTEST_ADDOPTS="-p tools.agentops.suite_plan_plugin")


def _red(root: Path, ticket: int | None = None, tests: str = "tests", extra: list[str] | None = None):
    cmd = [sys.executable, "-m", "tools.agentops", "red-on-base",
           "--cwd", str(root), f"--tests={tests}", "--file", "tests/red_on_main.txt"]
    if ticket is not None:
        cmd += ["--ticket", str(ticket)]
    cmd += extra or []
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=_env(), timeout=300)


def _pytest_with_plugin(root: Path) -> str:
    """Run the sample's tests with its conftest (which loads the plugin); return the pytest summary line."""
    (root / "tests" / "conftest.py").write_text(SAMPLE_CONFTEST, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rs", "tests"],
        cwd=root, capture_output=True, text=True,
        env={k: v for k, v in _env().items() if k != "PYTEST_ADDOPTS"}, timeout=300,
    )
    return proc.stdout.strip().splitlines()[-1]


def _skip_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("skip ")]


def test_row1_writes_one_skip_line_per_red_with_the_ticket(tmp_path):
    root = _sample(tmp_path, "s1")
    proc = _red(root, ticket=900)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = _list_file(root).read_text(encoding="utf-8")
    assert sorted(_skip_lines(_list_file(root))) == sorted(
        f"skip {nodeid} # Tests red on main before #900" for nodeid in RED_IDS
    )
    assert "test_green" not in text
    assert text.startswith("#"), "a new file opens with a comment header"
    assert "ticket: #900, added 3, total 3" in proc.stdout


def test_row1b_without_ticket_and_empty_file_exits_3_and_writes_nothing(tmp_path):
    root = _sample(tmp_path, "s1b")
    _list_file(root).write_bytes(b"")
    proc = _red(root)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert _list_file(root).read_bytes() == b""
    assert 'open one ticket "Tests red on main before <next>" listing these ids:' in proc.stdout
    for nodeid in RED_IDS:
        assert nodeid in proc.stdout


def test_row2_second_run_keeps_the_ticket_and_appends_only_the_new_red(tmp_path):
    root = _sample(tmp_path, "s2")
    assert _red(root, ticket=900).returncode == 0
    before = _list_file(root).read_bytes()

    (root / "tests" / "test_sample.py").write_text(SAMPLE_TESTS + EXTRA_RED, encoding="utf-8")
    proc = _red(root)  # no --ticket: the file already names #900
    assert proc.returncode == 0, proc.stdout + proc.stderr
    after = _list_file(root).read_bytes()
    assert after.startswith(before), "existing lines stay byte-identical"
    assert after[len(before):] == b"skip tests/test_sample.py::test_red_four # Tests red on main before #900\n"
    assert "ticket: #900, added 1, total 4" in proc.stdout

    again = _red(root)  # nothing new is red: the file is not touched
    assert again.returncode == 0, again.stdout + again.stderr
    assert "ticket: #900, added 0, total 4" in again.stdout
    assert _list_file(root).read_bytes() == after


def test_row2b_a_different_ticket_exits_2_and_writes_nothing(tmp_path):
    root = _sample(tmp_path, "s2b")
    assert _red(root, ticket=900).returncode == 0
    before = _list_file(root).read_bytes()
    proc = _red(root, ticket=901)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert _list_file(root).read_bytes() == before


def test_row3_the_plugin_skips_the_three_listed_reds(tmp_path):
    root = _sample(tmp_path, "s3")
    assert _red(root, ticket=900).returncode == 0
    summary = _pytest_with_plugin(root)
    assert "1 passed, 3 skipped" in summary, summary
    assert "failed" not in summary and "error" not in summary, summary


def test_row4_the_repo_conftest_loads_the_plugin_and_keeps_no_hook_of_its_own():
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
    own_hooks = [node.name for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name.startswith("pytest_")]
    assert own_hooks == []
    plugins = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytest_plugins"
                                                for t in node.targets):
            plugins = ast.literal_eval(node.value)
    assert PLUGIN in plugins


def test_row5_one_parser_splits_on_the_first_space_hash_space_and_nowhere_else():
    from tools.agentops.red_on_main_plugin import parse_line

    assert parse_line("skip tests/t.py::test_p[a#b] # Tests red on main before #900") == (
        "tests/t.py::test_p[a#b]", "Tests red on main before #900")
    assert parse_line("skip tests\\t.py::test_x") == ("tests/t.py::test_x", "")
    assert parse_line("skip tests/t.py::test_x # reason # with hash") == (
        "tests/t.py::test_x", "reason # with hash")
    assert parse_line("# a comment") is None
    assert parse_line("") is None
    assert parse_line("xfail tests/t.py::test_x # y") is None


def test_row6_one_pass_lists_hash_params_fixture_errors_failures_and_import_errors_despite_addopts_x(tmp_path):
    root = _sample(tmp_path, "s6", files=RICH_FILES, ini=RICH_INI)
    # the selection uses Windows-style backslash paths
    proc = _red(root, ticket=900, tests=r"tests\test_hash.py tests\test_mixed.py tests\test_bad.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(_skip_lines(_list_file(root))) == sorted(
        f"skip {nodeid} # Tests red on main before #900" for nodeid in RICH_IDS
    )
    assert "ticket: #900, added 5, total 5" in proc.stdout
    # with the marks in place: the green test passes, the 4 listed ones are skipped, the import error is not collected
    summary = _pytest_with_plugin(root)
    assert "1 passed, 4 skipped" in summary, summary


def test_row7_a_pytest_usage_error_exits_1_and_writes_nothing(tmp_path):
    root = _sample(tmp_path, "s7")
    proc = _red(root, ticket=900, tests="--no-such-option-319")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "did not run" in proc.stdout
    assert not _list_file(root).exists()


def test_row8_a_selection_with_no_tests_is_no_red_tests_and_exits_0(tmp_path):
    root = _sample(tmp_path, "s8")
    proc = _red(root, ticket=900, tests="tests -k nothing_matches_this")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no red tests on base" in proc.stdout
    assert not _list_file(root).exists()


def test_row9_a_base_run_past_the_timeout_exits_1_and_writes_nothing(tmp_path):
    root = _sample(tmp_path, "s9", files={"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"})
    proc = _red(root, ticket=900, extra=["--timeout-s", "3"])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "did not run" in proc.stdout and "timed out" in proc.stdout
    assert not _list_file(root).exists()
