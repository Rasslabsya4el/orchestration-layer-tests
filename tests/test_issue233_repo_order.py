"""#233 unit 1: the owner's repo order, one file in machine_dir() plus a change log (rows of the unit's spec)."""

import json
import re

import pytest

from tools.agentops import repo_order as ro


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


def _log_rows(mdir):
    path = mdir / "repo_order.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_get_of_a_missing_file_is_empty(mdir):
    assert not (mdir / "repo_order.json").exists()
    assert ro.get() == []


def test_set_then_get_returns_the_same_order(mdir):
    ro.set_order(["B", "A", "C"])
    assert ro.get() == ["B", "A", "C"]
    assert json.loads((mdir / "repo_order.json").read_text(encoding="utf-8")) == ["B", "A", "C"]


def test_an_unreadable_file_is_empty(mdir):
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "repo_order.json").write_text("not json at all", encoding="utf-8")
    assert ro.get() == []
    assert ro.rank("A") == (0, 0)


def test_rank_of_first_second_unlisted_and_empty_order(mdir):
    ro.set_order(["A", "B"])
    assert ro.rank("A") == (0, 2)
    assert ro.rank("B") == (1, 2)
    assert ro.rank("C") == (2, 2)  # absent from a non-empty list: after every listed one
    ro.set_order([])
    assert ro.rank("A") == (0, 0)  # empty order: no ranking, for every repo
    assert ro.rank("B") == (0, 0)


def test_rank_of_a_missing_file_is_no_ranking(mdir):
    assert ro.rank("A") == (0, 0)


def test_one_log_line_per_real_change_and_none_for_an_unchanged_set(mdir):
    ro.set_order(["A", "B"])
    rows = _log_rows(mdir)
    assert len(rows) == 1
    assert rows[0]["old"] == []
    assert rows[0]["new"] == ["A", "B"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rows[0]["time"])

    ro.set_order(["A", "B"])  # unchanged: no line, no rewrite
    assert len(_log_rows(mdir)) == 1

    ro.set_order(["B", "A"])  # a real change: one more line, old then new
    rows = _log_rows(mdir)
    assert len(rows) == 2
    assert rows[1]["old"] == ["A", "B"]
    assert rows[1]["new"] == ["B", "A"]
    assert ro.get() == ["B", "A"]


def test_cli_set_then_show(mdir, capsys):
    from tools.agentops import cli

    assert cli.main(["repo-order", "set", "B", "A"]) == 0
    capsys.readouterr()
    assert cli.main(["repo-order", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == ["B", "A"]
    assert ro.get() == ["B", "A"]


def test_cli_empty_set_and_show(mdir, capsys):
    from tools.agentops import cli

    assert cli.main(["repo-order", "set", "B", "A"]) == 0
    assert cli.main(["repo-order", "set"]) == 0
    capsys.readouterr()
    assert cli.main(["repo-order", "show"]) == 0
    assert capsys.readouterr().out.strip() == "(empty: today's order)"
    assert ro.get() == []


def test_rank_ignores_the_case_of_a_folder_name(mdir):
    ro.set_order(["Alpha-Repo", "BetaRepo"])
    assert ro.rank("alpha-repo") == (0, 2)
    assert ro.rank("BETAREPO") == (1, 2)
