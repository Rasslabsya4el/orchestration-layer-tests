"""logic-audit part 3, "until" conditions (issue #191, acceptance row 3). A throwaway git repo per test (identity by
``-c``); the issue lookup is a stub, so no network and no ``gh`` call."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.logic_audit_rows import NO_SOURCE
from tools.agentops.logic_audit_until import until_rows
from tools.agentops.util import no_window_flags

GIT_ARGS = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *GIT_ARGS, *args], check=True, capture_output=True,
                   creationflags=no_window_flags())


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_closed_ticket_is_a_finding_open_ticket_is_not_and_open_ended_is_listed(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "a.py": "# until #7 the old path stays\n",
        "b.py": "# until #8 keep this\n",
        "c.py": "# TODO fix later\n",
    })
    later = datetime.now(timezone.utc) + timedelta(hours=3)

    rows = until_rows(root, issue_state=lambda n: {7: "closed", 8: "open"}.get(n), now=later)

    met = [r for r in rows if r.kind == "until met"]
    assert len(met) == 1
    assert met[0].place_a == "a.py:1"
    assert met[0].verdict == "owner decides"
    assert "#7" in met[0].finding and "closed" in met[0].finding
    assert not any(r.place_a.startswith("b.py") for r in rows)

    open_ended = [r for r in rows if r.kind == "until open-ended"]
    assert len(open_ended) == 1
    assert open_ended[0].place_a == "c.py:1"
    assert open_ended[0].finding.startswith("TODO without a ticket, ")
    assert "3 h" in open_ended[0].finding  # the age is a human duration, not a bare count of seconds
    assert re.search(r"\d+(?:\.\d)? (?:s|min|h)\b", open_ended[0].finding)
    assert open_ended[0].verdict == "listed"
    assert open_ended[0].count == 1
    assert len(rows) == 2


def test_open_or_unknown_ticket_gives_no_until_met_row(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a.py": "# until #7 the old path stays\n"})

    rows = until_rows(root, issue_state=lambda n: None)

    assert [r for r in rows if r.kind == "until met"] == []


def test_file_under_tests_is_skipped(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "a.py": "# TODO outside tests\n",
        "tests/test_x.py": "# TODO inside a test\n",
    })

    rows = until_rows(root, issue_state=lambda n: None)

    assert [r.place_a.split(":")[0] for r in rows] == ["a.py"]


def test_no_git_repo_gives_one_no_source_row(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.py").write_text("# TODO outside any repo\n", encoding="utf-8")

    rows = until_rows(root, issue_state=lambda n: None)

    assert len(rows) == 1
    assert rows[0].kind == NO_SOURCE
    assert "until" in rows[0].finding
