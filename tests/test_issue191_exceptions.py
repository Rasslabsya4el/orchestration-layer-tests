"""#191 part 2 (`logic-audit` exception lists): a dead entry of an allow list, a list that only grew, and the no-source row.

Each test builds a throwaway git repo in ``tmp_path`` with dated commits and reads it through ``exception_rows`` only.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

from tools.agentops import logic_audit_exceptions
from tools.agentops.logic_audit_exceptions import exception_rows
from tools.agentops.logic_audit_rows import NO_SOURCE
from tools.agentops.util import no_window_flags

DAY1 = "2026-10-01 12:00:00 +0000"
DAY2 = "2026-10-05 12:00:00 +0000"
#: 18:00 UTC on the since-day: after its local midnight in every time zone from UTC-12 to UTC+14.
BOUNDARY_DAY = "2026-10-03 18:00:00 +0000"


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", *args],
        env=env, check=True, capture_output=True, creationflags=no_window_flags(), timeout=60,
    )


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")


def _commit(repo: Path, message: str, when: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when)


def test_dead_entries_and_list_that_only_grew(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(
        json.dumps({"allow_files": ["a.txt", "b.txt"], "other": 1}, indent=2), encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    (repo / "config" / "g.json").write_text(
        json.dumps({"allow_files": ["a.txt", "b.txt", "ghost.txt", "gone.bin"], "other": 1}, indent=2),
        encoding="utf-8")
    _commit(repo, "day 2", DAY2)

    rows = exception_rows(repo, since=date(2026, 10, 3))

    dead = sorted((r for r in rows if r.kind == "dead exception"), key=lambda r: r.finding)
    grew = [r for r in rows if r.kind == "list only grew"]
    assert len(rows) == 3
    assert [r.finding for r in dead] == [
        "entry ghost.txt of config/g.json::allow_files matches nothing",
        "entry gone.bin of config/g.json::allow_files matches nothing",
    ]
    json_lines = (repo / "config" / "g.json").read_text(encoding="utf-8").splitlines()
    ghost_line = next(i for i, line in enumerate(json_lines, 1) if '"ghost.txt"' in line)
    gone_line = next(i for i, line in enumerate(json_lines, 1) if '"gone.bin"' in line)
    assert dead[0].place_a == f"config/g.json:{ghost_line}"
    assert dead[1].place_a == f"config/g.json:{gone_line}"
    assert dead[0].place_b == "" and dead[1].place_b == ""
    assert dead[0].verdict == "owner decides"
    assert len(grew) == 1
    assert grew[0].count == 2
    assert "2 -> 4" in grew[0].finding
    assert "ghost.txt" in grew[0].finding and "gone.bin" in grew[0].finding


def test_guard_list_entry_that_matches_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "tools").mkdir()
    (repo / "tools" / "x_guard.py").write_text(
        '"""guard fixture."""\n\nALLOW_X = ("keepme", "dead_entry_zz")\n', encoding="utf-8")
    (repo / "notes.txt").write_text("the keepme word lives here\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    rows = exception_rows(repo, since=date(2026, 10, 3))

    assert len(rows) == 1
    assert rows[0].kind == "dead exception"
    assert rows[0].finding == "entry dead_entry_zz of tools/x_guard.py::ALLOW_X matches nothing"
    assert rows[0].place_a == "tools/x_guard.py:3"


def test_commit_on_the_since_day_is_after_since(tmp_path: Path) -> None:
    """The base is the last commit before the since-day's midnight: the day's own commit is not the base."""
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["a.txt", "b.txt"]}), encoding="utf-8")
    for name in ("a.txt", "b.txt", "c.txt"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["a.txt", "b.txt", "c.txt"]}), encoding="utf-8")
    _commit(repo, "boundary day", BOUNDARY_DAY)

    rows = exception_rows(repo, since=date(2026, 10, 3))

    assert len(rows) == 1
    assert rows[0].kind == "list only grew"
    assert rows[0].count == 1
    assert "2 -> 3" in rows[0].finding


def test_entry_match_is_case_exact(tmp_path: Path) -> None:
    """fnmatch would fold case on Windows: ``A.TXT`` must not match ``a.txt``."""
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["A.TXT"]}), encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    rows = exception_rows(repo, since=date(2026, 10, 3))

    assert [r.finding for r in rows] == ["entry A.TXT of config/g.json::allow_files matches nothing"]


def test_git_grep_timeout_never_makes_an_entry_dead(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["ghost.txt"]}), encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    control = exception_rows(repo, since=date(2026, 10, 3))
    assert [r.finding for r in control] == ["entry ghost.txt of config/g.json::allow_files matches nothing"]

    real_run = subprocess.run

    def run_grep_times_out(argv, **kwargs):
        if "grep" in argv:
            raise subprocess.TimeoutExpired(argv, 120)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(logic_audit_exceptions.subprocess, "run", run_grep_times_out)
    rows = exception_rows(repo, since=date(2026, 10, 3))

    assert [r for r in rows if r.kind == "dead exception"] == []


def test_grown_list_that_also_lost_an_entry_is_not_reported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["a.txt", "b.txt"]}), encoding="utf-8")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    (repo / "config" / "g.json").write_text(json.dumps({"allow_files": ["a.txt", "c.txt", "d.txt"]}), encoding="utf-8")
    _commit(repo, "day 2", DAY2)

    assert exception_rows(repo, since=date(2026, 10, 3)) == []


def test_new_list_with_one_entry_is_not_reported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "config").mkdir()
    (repo / "config" / "g.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    (repo / "config" / "g.json").write_text(json.dumps({"other": 1, "allow_new": ["a.txt"]}), encoding="utf-8")
    _commit(repo, "day 2", DAY2)

    assert exception_rows(repo, since=date(2026, 10, 3)) == []


def test_no_source_when_repo_has_neither_config_nor_guard(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init(repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "day 1", DAY1)

    rows = exception_rows(repo, since=date(2026, 10, 3))

    assert len(rows) == 1
    assert rows[0].kind == NO_SOURCE
    assert rows[0].finding.startswith("exceptions:")


def test_no_source_when_there_is_no_git_repo(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "config").mkdir()
    (folder / "config" / "g.json").write_text('{"allow_files": ["a.txt"]}', encoding="utf-8")

    rows = exception_rows(folder, since=date(2026, 10, 3))

    assert len(rows) == 1
    assert rows[0].kind == NO_SOURCE
