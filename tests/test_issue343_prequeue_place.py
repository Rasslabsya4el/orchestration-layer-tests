"""#343 unit 6 (rules 1, 3): prequeue's full suite and red-on-base run where ``heavy_place.decide`` says: GitHub when the
settings' ``github_suite`` is on and a plan exists, the PC otherwise. Every run is faked here (the GitHub runner, the PC
runner, the settings reader, the clock), so no test pushes to a test copy or starts a suite.

Rows: github on + green -> rc 0 and the place line; github red -> rc 1 with the failed ids; a GitHub fallback -> the PC
run is called once, its line says the reason; github_suite off -> the PC run with ``github_suite off``; red-on-base the
same way; the child pytest of red-on-base runs in ``util.nested_env`` (no build variables)."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_select_build_map import SUITE, _git, _repo
from tools.agentops import core_settings, github_suite, prequeue, red_on_base
from tools.agentops.red_on_main_plugin import REPORT_ENV

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
ON = {**core_settings.DEFAULTS, "github_suite": True, "repo": "o/r", "test_copy_remote": "https://github.com/o/t.git"}
OFF = {**core_settings.DEFAULTS, "github_suite": False, "repo": "o/r"}
GREEN_IDS = ["tests/test_a.py::test_x"]


@pytest.fixture(autouse=True)
def _no_real_copy_scan(monkeypatch):
    """The test copy's scan reads the real settings; this file decides the place itself and never scans."""
    monkeypatch.setattr(prequeue, "_test_copy_on", lambda: False)


def _fixture(tmp_path: Path) -> Path:
    """A repo on ``main`` with an origin and a checked-out ``issue`` branch that changes one file (no test map)."""
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "branch", "-M", "main")
    (repo / ".gitignore").write_text("var/\n.claude/\n__pycache__/\n.pytest_cache/\n*.pyc\n.testmondata\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "-b", "issue")
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # changed\n")
    _git(repo, "commit", "-q", "-am", "issue change")
    return repo


def _pc(calls: list, ok: bool = True):
    """A fake PC runner: one green (or red) SuiteRun-like answer, the call recorded."""
    def run(*args, **kwargs):
        calls.append(("pc", args, kwargs))
        return SimpleNamespace(stuck=None, ok=ok, fail_fast=None, fail_fast_notes=[], output="", failed_tests=[],
                               verdicts={}, load_reading=None, failure_readings={})
    return run


def _gh(calls: list, result):
    def run(plan, **kwargs):
        calls.append(("github", plan, kwargs))
        return result
    return run


def _seams(settings, *, pc_calls, gh_calls=None, gh_result=None, pc_ok=True) -> prequeue.Seams:
    return prequeue.Seams(
        load_settings=lambda: settings,
        clock=lambda: NOW,
        github_run=_gh(gh_calls if gh_calls is not None else [], gh_result),
        pc_run=_pc(pc_calls, ok=pc_ok),
        pc_only_files=lambda root: [],
    )


def _run(repo: Path, tmp_path: Path, seams, capsys) -> tuple[int, list[str], list[str]]:
    log = tmp_path / "merge.log"
    rc = prequeue.run_prequeue(repo, suite_cmd=SUITE, log_path=log, output_dir=tmp_path / "out", suite_shared=(),
                               issues=[], seams=seams)
    out = capsys.readouterr().out.splitlines()
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return rc, out, lines


# -- the full suite on GitHub: green, red, fallback -------------------------------------------------------------------

def test_github_on_and_green_exits_0_and_names_the_place(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls, gh_calls = [], []
    seams = _seams(ON, pc_calls=pc_calls, gh_calls=gh_calls,
                   gh_result=github_suite.GithubSuiteResult(status="green", machines=2))
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert rc == 0
    assert "prequeue on github" in out
    assert "prequeue: green" in out
    assert pc_calls == []
    assert len(gh_calls) == 1 and gh_calls[0][2]["ids"] is None and gh_calls[0][2]["tests"] is None
    assert any(line.endswith("| prequeue on github") for line in log)


def test_github_on_and_red_exits_1_and_prints_the_failed_ids(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls = []
    seams = _seams(ON, pc_calls=pc_calls,
                   gh_result=github_suite.GithubSuiteResult(status="red", failed_ids=GREEN_IDS, machines=2))
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert rc == 1
    assert "prequeue on github" in out
    assert "  FAILED tests/test_a.py::test_x" in out
    assert pc_calls == []


def test_github_fallback_runs_the_pc_once_and_names_the_reason(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls = []
    seams = _seams(ON, pc_calls=pc_calls,
                   gh_result=github_suite.GithubSuiteResult(status="fallback", reason="refused",
                                                            brain_line="x merge #1: GitHub refused, suite on the PC"))
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert rc == 0
    assert len(pc_calls) == 1
    assert "prequeue on pc (refused)" in out
    assert any(line.endswith("| prequeue on pc (refused)") for line in log)
    assert "prequeue on github" not in out


def test_github_red_with_no_named_test_goes_to_the_pc(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls = []
    seams = _seams(ON, pc_calls=pc_calls, gh_result=github_suite.GithubSuiteResult(status="red", failed_ids=[]))
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert len(pc_calls) == 1
    assert "prequeue on pc (red, no test named)" in out


# -- github_suite off: the PC runs it, the line says why ---------------------------------------------------------------

def test_github_suite_off_runs_the_pc_with_the_reason(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls, gh_calls = [], []
    seams = _seams(OFF, pc_calls=pc_calls, gh_calls=gh_calls)
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert rc == 0
    assert len(pc_calls) == 1 and gh_calls == []
    assert "prequeue on pc (github_suite off)" in out
    assert any(line.endswith("| prequeue on pc (github_suite off)") for line in log)


def test_unreadable_settings_run_the_pc_with_the_reason(tmp_path, capsys):
    repo = _fixture(tmp_path)
    pc_calls = []
    seams = _seams(None, pc_calls=pc_calls)
    rc, out, log = _run(repo, tmp_path, seams, capsys)
    assert len(pc_calls) == 1
    assert "prequeue on pc (github settings unreadable)" in out


# -- red-on-base: the same routing, its ids on GitHub ------------------------------------------------------------------

RB_IDS = ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]


def _rb_seams(settings, *, gh_calls=None, gh_result=None, pc_calls=None, collected=RB_IDS):
    def pc_reds(cwd, tests, timeout):
        pc_calls.append(("pc", tests))
        return []

    return red_on_base.Seams(
        load_settings=lambda: settings,
        clock=lambda: NOW,
        collect_ids=lambda cwd, tests, timeout: list(collected),
        github_run=_gh(gh_calls if gh_calls is not None else [], gh_result),
        pc_reds=pc_reds,
        pc_only_files=lambda root: [],
        head=lambda cwd: "c" * 40,
    )


def test_red_on_base_github_green_lists_nothing_and_names_the_place(tmp_path, capsys):
    gh_calls, pc_calls = [], []
    seams = _rb_seams(ON, gh_calls=gh_calls, gh_result=github_suite.GithubSuiteResult(status="green"), pc_calls=pc_calls)
    rc = red_on_base.red_on_base(tmp_path, "tests/test_x.py", 343, tmp_path / "red.txt", seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert "red-on-base on github" in out and "no red tests on base" in out
    assert pc_calls == []
    assert len(gh_calls) == 1 and gh_calls[0][2]["ids"] == RB_IDS and gh_calls[0][2]["tests"] == 2


def test_red_on_base_github_red_lists_the_failed_ids(tmp_path, capsys):
    seams = _rb_seams(ON, gh_result=github_suite.GithubSuiteResult(status="red", failed_ids=[RB_IDS[0]]))
    target = tmp_path / "red.txt"
    rc = red_on_base.red_on_base(tmp_path, "tests/test_x.py", 343, target, seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert "red-on-base on github" in out
    assert RB_IDS[0] in target.read_text(encoding="utf-8")
    assert RB_IDS[1] not in target.read_text(encoding="utf-8")
    assert "ticket: #343, added 1, total 1" in out


def test_red_on_base_fallback_runs_the_pc_with_the_reason(tmp_path, capsys):
    pc_calls = []
    seams = _rb_seams(ON, gh_result=github_suite.GithubSuiteResult(status="fallback", reason="no-places"),
                      pc_calls=pc_calls)
    rc = red_on_base.red_on_base(tmp_path, "tests/test_x.py", None, tmp_path / "red.txt", seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(pc_calls) == 1
    assert "red-on-base on pc (no-places)" in out


def test_red_on_base_github_suite_off_runs_the_pc_with_the_reason(tmp_path, capsys):
    gh_calls, pc_calls = [], []
    seams = _rb_seams(OFF, gh_calls=gh_calls, pc_calls=pc_calls)
    red_on_base.red_on_base(tmp_path, "tests/test_x.py", None, tmp_path / "red.txt", seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert gh_calls == [] and len(pc_calls) == 1
    assert "red-on-base on pc (github_suite off)" in out


def test_red_on_base_with_no_collected_id_never_sends_an_empty_ids_run(tmp_path, capsys):
    gh_calls, pc_calls = [], []
    seams = _rb_seams(ON, gh_calls=gh_calls, gh_result=github_suite.GithubSuiteResult(status="green"),
                      pc_calls=pc_calls, collected=[])
    red_on_base.red_on_base(tmp_path, "tests/test_x.py", None, tmp_path / "red.txt", seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert gh_calls == []  # an empty ids list would make GitHub run the whole suite
    assert len(pc_calls) == 1
    assert "red-on-base on pc (no test collected)" in out


# -- the child pytest of red-on-base runs in nested_env (rule: no build variables inherited) ----------------------------

BUILD_VARS = ("PYTEST_ADDOPTS", "SUITE_READS_DIR", "TESTMON_DATAFILE")


def test_red_on_base_child_env_drops_the_build_variables(monkeypatch, tmp_path):
    for name in BUILD_VARS:
        monkeypatch.setenv(name, "from-a-build")
    report = tmp_path / "reds.txt"
    env = red_on_base._child_env(report)
    for name in ("PYTEST_ADDOPTS", "SUITE_READS_DIR", "TESTMON_DATAFILE"):
        assert name not in env
    assert env[REPORT_ENV] == str(report)
    assert env["PYTHONUTF8"] == "1"


# -- review fixes (u6 pass 2) -----------------------------------------------------------------------------------------

def _count_berths(monkeypatch) -> list:
    """Every ``LineCheck._take_berth`` call of a run, replaced by a fake that takes no real berth."""
    taken: list = []

    def take(self, tests_line, output_path):
        taken.append(tests_line)
        return True

    monkeypatch.setattr(prequeue.LineCheck, "_take_berth", take)
    return taken


def test_a_prequeue_routed_to_github_takes_no_pc_berth(tmp_path, capsys, monkeypatch):
    repo = _fixture(tmp_path)
    taken = _count_berths(monkeypatch)
    seams = _seams(ON, pc_calls=[], gh_result=github_suite.GithubSuiteResult(status="green", machines=2))
    rc, out, _log = _run(repo, tmp_path, seams, capsys)
    assert rc == 0 and "prequeue on github" in out
    assert taken == []


def test_a_prequeue_routed_to_the_pc_takes_one_berth(tmp_path, capsys, monkeypatch):
    repo = _fixture(tmp_path)
    taken = _count_berths(monkeypatch)
    rc, out, _log = _run(repo, tmp_path, _seams(OFF, pc_calls=[]), capsys)
    assert rc == 0 and "prequeue on pc (github_suite off)" in out
    assert len(taken) == 1


def test_a_github_fallback_takes_the_one_berth_before_the_pc_run(tmp_path, capsys, monkeypatch):
    repo = _fixture(tmp_path)
    taken = _count_berths(monkeypatch)
    seams = _seams(ON, pc_calls=[], gh_result=github_suite.GithubSuiteResult(status="fallback", reason="refused"))
    order: list[str] = []
    real_pc = seams.pc_run
    seams.pc_run = lambda *a, **k: (order.append(f"pc after {len(taken)} berth"), real_pc(*a, **k))[1]
    rc, out, _log = _run(repo, tmp_path, seams, capsys)
    assert rc == 0 and "prequeue on pc (refused)" in out
    assert len(taken) == 1 and order == ["pc after 1 berth"]


def test_red_on_base_collection_failure_falls_back_to_the_pc(tmp_path, capsys):
    pc_calls = []
    seams = _rb_seams(ON, gh_result=github_suite.GithubSuiteResult(status="green"), pc_calls=pc_calls)

    def broken(cwd, tests, timeout):
        raise red_on_base.SelectionDidNotRun("pytest exit 2: ImportError")

    seams.collect_ids = broken
    rc = red_on_base.red_on_base(tmp_path, "tests/test_x.py", None, tmp_path / "red.txt", seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(pc_calls) == 1
    assert "red-on-base on pc (collect failed)" in out
    assert not any("refused" in line for line in out)


def test_red_on_base_github_failed_ids_are_normalised_like_the_pc_ids(tmp_path, capsys):
    seams = _rb_seams(ON, gh_result=github_suite.GithubSuiteResult(status="red",
                                                                  failed_ids=["tests\\test_x.py::test_a "]))
    target = tmp_path / "red.txt"
    rc = red_on_base.red_on_base(tmp_path, "tests/test_x.py", 343, target, seams=seams)
    assert rc == 0
    assert "skip tests/test_x.py::test_a #" in target.read_text(encoding="utf-8")


def test_two_red_on_base_runs_use_different_refs(tmp_path, monkeypatch):
    refs = []
    for pid in (101, 202):
        gh_calls = []
        monkeypatch.setattr(red_on_base.os, "getpid", lambda pid=pid: pid)
        seams = _rb_seams(ON, gh_calls=gh_calls, gh_result=github_suite.GithubSuiteResult(status="green"))
        red_on_base.red_on_base(tmp_path, "tests/test_x.py", None, tmp_path / "red.txt", seams=seams)
        refs.append(gh_calls[0][2]["branch"])
    assert refs[0] != refs[1]
    assert all(ref.startswith("red-on-base-" + "c" * 8 + "-") for ref in refs)
