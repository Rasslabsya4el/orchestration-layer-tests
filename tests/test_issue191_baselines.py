"""#191 part 1: `logic-audit` lists every baseline added in a window and the older code it would refuse today.

A throwaway git repo (no shell, no real history): day 1 = 2026-10-01, day 2 = 2026-10-02.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

import tools.agentops.logic_audit_baselines as audit
from tools.agentops.logic_audit_baselines import baseline_rows
from tools.agentops.logic_audit_rows import NO_SOURCE
from tools.agentops.util import no_window_flags

DAY1 = "2026-10-01T12:00:00"
DAY2 = "2026-10-02T12:00:00"
GIT_TIMEOUT_S = 60


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=tester", "-c", "user.email=tester@example.com",
         "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True, env=env, creationflags=no_window_flags(), timeout=GIT_TIMEOUT_S,
    )


def _commit(root: Path, files: dict[str, str], message: str, when: str) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message, env=env)


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")


def test_adr_added_on_day2_lists_day1_code_that_breaks_it(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/a.py": 'os.system("x")\n'}, "day 1: old code", DAY1)
    _commit(tmp_path, {"docs/adr/0001-no-os-system.md": "Never call `os.system`\n"}, "day 2: ADR", DAY2)

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    mine = [r for r in rows if r.place_a.startswith("docs/adr/0001-no-os-system.md")]
    assert len(mine) == 1
    assert mine[0].place_b == "src/a.py:1"
    assert mine[0].kind == "baseline"
    assert mine[0].verdict == "reader checks"
    assert mine[0].count == 1
    assert "older code holds os.system" in mine[0].finding


def test_rule_line_added_in_claude_md_lists_older_file_holding_its_token(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/old.py": "import subprocess\nsubprocess.run(['ls'])\n"}, "day 1: old code", DAY1)
    _commit(tmp_path, {"CLAUDE.md": "# Rules\n\nNever call `subprocess.run` here.\n"}, "day 2: rule", DAY2)

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    assert len(rows) == 1
    assert rows[0].kind == "baseline"
    assert rows[0].place_a == "CLAUDE.md:3"
    assert rows[0].place_b == "src/old.py:2"
    assert "older code holds subprocess.run" in rows[0].finding


def test_new_module_named_by_a_rule_line_is_a_baseline(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/old.py": "import json\njson.dumps({})\n"}, "day 1: old code", DAY1)
    _commit(
        tmp_path,
        {
            "tools/helper_x.py": '"""Output goes through `json.dumps`."""\n',
            "CLAUDE.md": "Use `helper_x` for the output.\n",
        },
        "day 2: module and rule",
        DAY2,
    )

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    module_rows = [r for r in rows if "tools/helper_x.py" in r.finding]
    assert len(module_rows) == 1
    assert module_rows[0].place_a == "CLAUDE.md:1"
    assert module_rows[0].place_b == "src/old.py:2"
    assert "older code holds json.dumps" in module_rows[0].finding


def test_module_named_by_stem_with_py_suffix_is_a_baseline(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/old.py": "import json\njson.dumps({})\n"}, "day 1: old code", DAY1)
    _commit(
        tmp_path,
        {
            "tools/helper_y.py": '"""Output goes through `json.dumps`."""\n',
            "CLAUDE.md": "Use `helper_y.py` for the output.\n",
        },
        "day 2: module and rule",
        DAY2,
    )

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    module_rows = [r for r in rows if "tools/helper_y.py" in r.finding]
    assert len(module_rows) == 1
    assert module_rows[0].place_a == "CLAUDE.md:1"
    assert module_rows[0].place_b == "src/old.py:2"


def test_module_named_by_dotted_name_is_a_baseline(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/old.py": "import json\njson.dumps({})\n"}, "day 1: old code", DAY1)
    _commit(
        tmp_path,
        {
            "tools/agentops/helper_z.py": '"""Output goes through `json.dumps`."""\n',
            "CLAUDE.md": "Use `tools.agentops.helper_z` for the output.\n",
        },
        "day 2: module and rule",
        DAY2,
    )

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    module_rows = [r for r in rows if "tools/agentops/helper_z.py" in r.finding]
    assert len(module_rows) == 1
    assert module_rows[0].place_a == "CLAUDE.md:1"
    assert module_rows[0].place_b == "src/old.py:2"


def test_token_found_in_nine_files_is_generic_and_yields_no_row(tmp_path: Path):
    _init(tmp_path)
    old = {f"src/f{i}.py": "shared_call()\n" for i in range(9)}
    old["src/one.py"] = "unique_call()\n"
    _commit(tmp_path, old, "day 1: old code", DAY1)
    _commit(
        tmp_path,
        {"docs/adr/0001-calls.md": "Never call `shared_call()` or `unique_call()`.\n"},
        "day 2: ADR",
        DAY2,
    )

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    assert [r.place_b for r in rows] == ["src/one.py:1"]
    assert "unique_call()" in rows[0].finding


def test_a_baseline_keeps_at_most_five_candidate_files(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {f"src/g{i}.py": "hook_call()\n" for i in range(6)}, "day 1: old code", DAY1)
    _commit(tmp_path, {"docs/adr/0001-hooks.md": "Never `hook_call()`.\n"}, "day 2: ADR", DAY2)

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    assert [r.place_b for r in rows] == [f"src/g{i}.py:1" for i in range(5)]


def test_cap_row_appears_when_rows_exceed_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(audit, "MAX_ROWS", 3)
    _init(tmp_path)
    _commit(tmp_path, {f"src/c{i}.py": "cap_call()\n" for i in range(4)}, "day 1: old code", DAY1)
    _commit(tmp_path, {"docs/adr/0001-cap.md": "Never `cap_call()`.\n"}, "day 2: ADR", DAY2)

    rows = baseline_rows(tmp_path, since=date(2026, 10, 2))

    assert len(rows) == 3
    assert [r.kind for r in rows[:2]] == ["baseline", "baseline"]
    assert rows[-1].kind == "baseline"
    assert rows[-1].finding == "... 2 more baseline rows not shown"


def test_window_without_commit_is_a_no_source_row(tmp_path: Path):
    _init(tmp_path)
    _commit(tmp_path, {"src/a.py": 'os.system("x")\n'}, "day 1: old code", DAY1)
    _commit(tmp_path, {"docs/adr/0001-no-os-system.md": "Never call `os.system`\n"}, "day 2: ADR", DAY2)

    rows = baseline_rows(tmp_path, since=date(2026, 10, 5))

    assert len(rows) == 1
    assert rows[0].kind == NO_SOURCE


def test_folder_that_is_not_a_git_repo_is_a_no_source_row(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()

    rows = baseline_rows(plain, since=date(2026, 10, 1))

    assert len(rows) == 1
    assert rows[0].kind == NO_SOURCE
