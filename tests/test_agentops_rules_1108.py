"""#1108 unit 1: `python -m tools.agentops rules` and `rules --issues N,M,...`.

A fixture git repo in tmp_path (no heavy tool, no network): rule files, a code file and a plain doc, and a few commits whose
messages name issues. The seams under test are the lines the CLI prints.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.agentops.cli import main


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return proc.stdout


def _write(repo: Path, rel: str, lines: int) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")


def _append(repo: Path, rel: str, lines: int) -> None:
    path = repo / rel
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(f"more {i}\n" for i in range(lines)))


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    # C1 "#1": five rule files (10+4+2+3+5 = 24 lines), a doc outside the rule set, a code file.
    _write(root, "CLAUDE.md", 10)
    _write(root, "docs/agents/a.md", 4)
    _write(root, "docs/agents/sub/b.md", 2)
    _write(root, ".claude/agents/w.md", 3)
    _write(root, ".claude/skills/s/SKILL.md", 5)
    _write(root, "docs/other.md", 7)
    _write(root, "tools/code.py", 1)
    _commit(root, "#1 seed the rule files")
    # C2 "#12": a rule file (+2) and a code file (+10) in one commit; only the +2 counts for #12.
    _append(root, "docs/agents/a.md", 2)
    _append(root, "tools/code.py", 10)
    _commit(root, "#12 rule line and code line")
    # C3 "#123": +4 on a rule file; it must not be counted for #12.
    _append(root, "docs/agents/sub/b.md", 4)
    _commit(root, "#123 sub rule file")
    # C4 "#12": deletes a rule file (-3).
    _git(root, "rm", "-q", ".claude/agents/w.md")
    _commit(root, "#12 drop the worker template")
    return root


def test_rules_without_flag_prints_count_and_lines_at_head(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--root", str(repo)]) == 0
    # HEAD holds 4 rule files: CLAUDE.md 10, docs/agents/a.md 6, docs/agents/sub/b.md 6, .claude/skills/s/SKILL.md 5.
    assert capsys.readouterr().out.strip() == "4 files, 27 lines"


def test_rules_issues_prints_one_row_per_issue_and_sum(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--root", str(repo), "--issues", "1,12,123,999"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()
    assert rows == [
        "#1 +24/-0",
        "#12 +2/-3",
        "#123 +4/-0",
        "#999 +0/-0",
        "sum +30/-3",
    ]


def test_rules_issues_counts_only_the_rule_file_part_of_a_mixed_commit(repo: Path,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--root", str(repo), "--issues", "12"]) == 0
    # C2 also adds 10 code lines; they are not counted.
    assert capsys.readouterr().out.strip().splitlines()[0] == "#12 +2/-3"


def test_rules_issues_does_not_count_a_longer_number_for_a_shorter_one(repo: Path,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--root", str(repo), "--issues", "12,123"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()
    assert rows[0] == "#12 +2/-3"   # C3 (#123) is not counted for #12
    assert rows[1] == "#123 +4/-0"


def test_rules_issues_prefers_origin_main_when_it_exists(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before_c4 = _git(repo, "rev-parse", "HEAD~1").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", before_c4)
    assert main(["rules", "--root", str(repo), "--issues", "12"]) == 0
    # origin/main stops before C4, so #12 is the +2 of C2 only
    assert capsys.readouterr().out.strip().splitlines()[0] == "#12 +2/-0"
