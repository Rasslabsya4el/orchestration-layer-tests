"""#250: the prequeue's check is a Priority one when its issue's labels rank it (acceptance rows 1-4), and its
issue numbers come from ``--issue`` or from the branch name (row 3).

Seams: ``run_prequeue`` on a tmp repo (as tests/test_prequeue.py), the ``LineCheck`` it builds (spied), the berth
waiter the check writes (spied on ``machine_berths._wait_and_take``), and the CLI parser."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from tests.test_prequeue import GREEN_A, RED_A, _change_a, _fixture, _log
from tests.test_select_build_map import SUITE, _git
from tools.agentops import machine_berths as ms
from tools.agentops import merge as merge_module
from tools.agentops import merge_machine
from tools.agentops import prequeue
from tools.agentops.prequeue import add_subparser, issues_of_branch, run_prequeue

FLOW_FIX_MARKS = {"flow-fix": "2026-10-19T10:00:00Z"}


@pytest.fixture(autouse=True)
def machine_dir_in_tmp(tmp_path, monkeypatch):
    """The berth log sits in this test's own machine dir; one berth, so a heavy check takes it at once."""
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    monkeypatch.setattr(merge_machine, "configured_berths", lambda: 1)


def _run(repo, tmp_path, capsys, **kwargs) -> tuple[int, list[str]]:
    rc = run_prequeue(repo, suite_cmd=SUITE, log_path=tmp_path / "merge.log", output_dir=tmp_path / "out",
                      suite_shared=(), **kwargs)
    return rc, capsys.readouterr().out.splitlines()


def _spy_line_check(monkeypatch) -> list:
    """The ``priority`` keyword each LineCheck the prequeue builds is constructed with (None when not passed)."""
    seen: list = []
    real = prequeue.LineCheck

    class Spy(real):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("priority"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(prequeue, "LineCheck", Spy)
    return seen


def _spy_waiters(monkeypatch) -> list:
    """Every waiter record the berth wait writes (a copy, taken before the wait starts)."""
    seen: list = []
    real = ms._wait_and_take

    def spy(waiter, *args, **kwargs):
        seen.append(dict(waiter))
        return real(waiter, *args, **kwargs)

    monkeypatch.setattr(ms, "_wait_and_take", spy)
    return seen


def _reader(marks_by_issue: dict):
    """A label reader stub: the marks of each issue, or the exception to raise for it; records the issues read."""
    seen: list[int] = []

    def read(issue: int):
        seen.append(issue)
        value = marks_by_issue[issue]
        if isinstance(value, Exception):
            raise value
        return value

    return read, seen


def _ordinary_line(count: int) -> list[dict]:
    """``count`` ordinary waiters that waited long ago, as the machine line holds them."""
    return [{"id": f"old-{i}", "repo": "other", "run": "x", "kind": "pc", "tests": 4255, "priority": False,
             "since": 1.0 + i, "pid": 1} for i in range(count)]


def test_row1_flow_fix_label_makes_the_prequeue_priority_and_its_waiter_heads_the_seven_ordinary(
        tmp_path, monkeypatch, capsys):
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    constructed = _spy_line_check(monkeypatch)
    waiters = _spy_waiters(monkeypatch)
    read, seen = _reader({247: FLOW_FIX_MARKS})
    rc, _lines = _run(repo, tmp_path, capsys, issues=[247], reader=read)
    assert rc == 0
    assert seen == [247]
    assert constructed == [True]
    assert waiters and waiters[0]["priority"] is True
    ours = waiters[0]
    head = ms._order([(Path(f"{r['id']}.json"), r) for r in [*_ordinary_line(7), ours]],
                     now=time.time(), max_wait_s=3600.0)
    assert head[0][1]["id"] == ours["id"]


def test_row2_no_priority_class_makes_the_prequeue_ordinary_and_it_queues_behind_the_seven(
        tmp_path, monkeypatch, capsys):
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    constructed = _spy_line_check(monkeypatch)
    waiters = _spy_waiters(monkeypatch)
    read, seen = _reader({247: {}})
    rc, _lines = _run(repo, tmp_path, capsys, issues=[247], reader=read)
    assert rc == 0
    assert seen == [247]
    assert constructed == [False]
    assert waiters and waiters[0]["priority"] is False
    ours = waiters[0]
    line = ms._order([(Path(f"{r['id']}.json"), r) for r in [*_ordinary_line(7), ours]],
                     now=time.time(), max_wait_s=3600.0)
    assert [r["id"] for _, r in line].index(ours["id"]) == 7


def test_row3_branch_u248_2_without_issue_flag_reads_issue_248(tmp_path, monkeypatch, capsys):
    repo, _base = _fixture(tmp_path, with_map=False)
    _git(repo, "branch", "-m", "issue", "u248-2")
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    constructed = _spy_line_check(monkeypatch)
    read, seen = _reader({248: {}})
    rc, _lines = _run(repo, tmp_path, capsys, reader=read)
    assert rc == 0
    assert seen == [248]
    assert constructed == [False]


@pytest.mark.parametrize("change, expected_rc", [(GREEN_A, 0), (RED_A, 1)])
def test_row4_label_reader_raising_is_ordinary_prints_the_line_and_keeps_the_runs_exit_code(
        tmp_path, monkeypatch, capsys, change, expected_rc):
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, change)
    _log(tmp_path / "merge.log", "4 min")
    constructed = _spy_line_check(monkeypatch)
    read, seen = _reader({247: RuntimeError("gh down")})
    rc, lines = _run(repo, tmp_path, capsys, issues=[247], reader=read)
    assert seen == [247]
    assert rc == expected_rc
    assert constructed == [False]
    assert "prequeue: priority not read: #247 (RuntimeError: gh down)" in lines


@pytest.mark.parametrize("branch, expected", [
    ("i247", [247]),
    ("u248-2", [248]),
    ("issue", []),
    ("main", []),
])
def test_the_branch_name_gives_its_issue_number(branch, expected):
    assert issues_of_branch(branch) == expected


def test_issue_flag_repeats_and_is_parsed_into_the_numbers():
    parser = argparse.ArgumentParser()
    add_subparser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["prequeue", "--cwd", "x", "--issue", "247", "--issue", "248"])
    assert args.issue == [247, 248]
    assert parser.parse_args(["prequeue", "--cwd", "x"]).issue == []


def test_a_foreign_issue_flag_is_dropped_like_the_merge_does_and_never_crashes(monkeypatch):
    from tools.agentops import prequeue
    seen = {}
    monkeypatch.setattr(prequeue.m, "resolve_suite_cmd", lambda _x: "suite")
    monkeypatch.setattr(prequeue, "run_prequeue", lambda cwd, **kw: seen.update(kw) or 0)
    parser = argparse.ArgumentParser()
    add_subparser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["prequeue", "--cwd", "x", "--issue", "owner/repo#5", "--issue", "247"])
    assert prequeue.run_prequeue_command(args) == 0
    assert seen["issues"] == [247]
    args = parser.parse_args(["prequeue", "--cwd", "x", "--issue", "owner/repo#5"])
    prequeue.run_prequeue_command(args)
    assert seen["issues"] is None
