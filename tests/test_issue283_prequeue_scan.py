"""#283: ``prequeue`` runs the test copy's scan (``test_copy_check.scan``) on the snapshot the branch would put on main,
when the repo's settings have ``github_suite`` true. A hit refuses the branch before it is queued: one line
``prequeue: refused, test copy hits: <file:line kind, ...>`` and exit 1. Off ``github_suite`` nothing changes.

Seams: ``run_prequeue`` on a tmp repo (as tests/test_prequeue.py does); the settings through ``core_settings.load``
(patched); the machine pattern file through ``AGENTOPS_MACHINE_DIR`` (a tmp dir). The pattern word is made up here,
never written into a file of this repo.
"""
from __future__ import annotations

import json

import pytest

from tests.test_prequeue import GREEN_A, _change_a, _fixture, _run
from tests.test_select_build_map import _git
from tools.agentops import core_settings
from tools.agentops import merge as merge_module
from tools.agentops import test_copy_check

PRODUCT_WORD = "zorblax"  # a made-up word: the machine pattern file of this test only
NEW_TEST = "tests/test_newcase.py"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    path.mkdir()
    (path / test_copy_check.MACHINE_FILE).write_text(json.dumps({"product": PRODUCT_WORD}), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def _select(monkeypatch):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)


def _settings(monkeypatch, github_suite: bool) -> None:
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"github_suite": github_suite})


def _branch_with_product_test(repo) -> None:
    """The issue branch: its change plus a new test file that holds the product word."""
    _change_a(repo, GREEN_A)
    (repo / NEW_TEST).write_text(f"NAME = {PRODUCT_WORD!r}\n", encoding="utf-8")
    _git(repo, "add", NEW_TEST)
    _git(repo, "commit", "-q", "-m", "new test with the product word")


def test_github_suite_on_refuses_a_branch_whose_new_test_holds_a_product_string(tmp_path, monkeypatch, capsys, machine):
    _settings(monkeypatch, github_suite=True)
    repo, _base = _fixture(tmp_path, with_map=True)
    _branch_with_product_test(repo)
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 1
    assert lines == [f"prequeue: refused, test copy hits: {NEW_TEST}:1 product"]


def test_github_suite_on_the_same_branch_without_the_string_exits_0(tmp_path, monkeypatch, capsys, machine):
    _settings(monkeypatch, github_suite=True)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, GREEN_A)
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert not any(line.startswith("prequeue: refused") for line in lines)


def test_github_suite_off_the_branch_with_the_string_is_not_refused(tmp_path, monkeypatch, capsys, machine):
    _settings(monkeypatch, github_suite=False)
    repo, _base = _fixture(tmp_path, with_map=True)
    _branch_with_product_test(repo)
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert not any("test copy hits" in line for line in lines)
