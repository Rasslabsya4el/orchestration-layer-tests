"""#58 unit 1: the close check of `python -m tools.agentops close`, with a fake gh (no live gh).

Each test maps to an acceptance row of #58 or to one behaviour the spec names: an unanswered comment refuses the
close, an answered one closes, machine lines and older comments never refuse it, and --dry-run never closes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops import core_settings  # noqa: E402
from tools.agentops.close_issue import run_close  # noqa: E402

REPO = "owner/orchestration-layer"
CREATED = "2026-10-10T09:00:00Z"
EDITED = "2026-10-15T12:00:00Z"
BEFORE = "2026-10-11T08:00:00Z"
AFTER = "2026-10-16T15:09:07Z"


def comment(database_id: int, created: str, body: str, login: str = "brain-bot") -> dict:
    return {"databaseId": database_id, "createdAt": created, "body": body, "author": {"login": login}}


def _result(args: list[str], rc: int, out: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh", *args], rc, out, "")


class FakeGh:
    """A fake runner: answers the issue's graphql read (and its comment pages) and `issue close`. Any other call,
    `repo view` included, fails the test."""

    def __init__(self, comments: list[dict], *, edited: str | None = EDITED, follow: dict | None = None):
        # follow: {cursor: (nodes, next_cursor)} for the comment pages after the first one
        self.comments = comments
        self.edited = edited
        self.follow = follow or {}
        self.first_next = "c1" if follow else None
        self.calls: list[list[str]] = []
        self.graphql_variables: list[dict] = []

    @staticmethod
    def _page(nodes: list[dict], next_cursor: str | None) -> dict:
        return {"pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}, "nodes": nodes}

    def _graphql(self, query: str, variables: dict) -> dict:
        if "node(id:" in query:
            nodes, next_cursor = self.follow[variables["cursor"]]
            return {"data": {"node": {"comments": self._page(nodes, next_cursor)}}}
        issue = {"id": "I_kwDOFAKE58", "state": "OPEN", "createdAt": CREATED, "lastEditedAt": self.edited,
                 "comments": self._page(self.comments, self.first_next)}
        return {"data": {"repository": {"issue": issue}}}

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:3] == ["api", "graphql", "--input"]:
            request = json.loads(stdin or "{}")
            self.graphql_variables.append(request["variables"])
            return _result(args, 0, json.dumps(self._graphql(request["query"], request["variables"])))
        if args[:2] == ["issue", "close"]:
            return _result(args, 0, "")
        raise AssertionError(f"unexpected gh call: {args}")

    def closes(self) -> list[list[str]]:
        return [args for args in self.calls if args[:2] == ["issue", "close"]]


def _closing(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "closing.md"
    path.write_text(text, encoding="utf-8")
    return path


# row 1: one unanswered brain comment after the last text edit refuses the close

def test_row1_unanswered_comment_after_the_edit_refuses_the_close(tmp_path, capsys):
    gh = FakeGh([comment(1001, AFTER, "Add the third row to the table\nand the date line")])
    rc = run_close(58, _closing(tmp_path, "Closing: the work is merged.\n"), repo=REPO, runner=gh)
    out = capsys.readouterr().out
    assert rc == 1
    assert "comment 1001 | 2026-10-16 | brain-bot | Add the third row to the table" in out
    assert out.rstrip().splitlines()[-1] == (
        "not closed: 1 comment(s) after the last edit of the issue text are not answered in the closing comment")
    assert gh.closes() == []


# row 2: the same comment answered in the closing comment closes the issue, once, with the comment file

def test_row2_answered_comment_closes_with_the_comment_file(tmp_path, capsys):
    text = "Closing: merged.\n- comment 1001: done in row 3\n"
    gh = FakeGh([comment(1001, AFTER, "Add the third row to the table")])
    rc = run_close(58, _closing(tmp_path, text), repo=REPO, runner=gh)
    assert rc == 0
    assert capsys.readouterr().out == ""
    closes = gh.closes()
    assert len(closes) == 1
    assert closes[0][:3] == ["issue", "close", "58"]
    assert closes[0][closes[0].index("--repo") + 1] == REPO
    assert f"--comment={text}" in closes[0]
    assert "--reason" not in closes[0]


@pytest.mark.parametrize("answer", [
    "comment 1001: done in row 3",
    "- comment 1001: moved to #12",
    "* comment 1001: not a task",
    "  COMMENT 1001: Done In Row 3",
])
def test_row2_each_answer_form_is_accepted(tmp_path, answer):
    gh = FakeGh([comment(1001, AFTER, "a request")])
    assert run_close(58, _closing(tmp_path, answer + "\n"), repo=REPO, runner=gh) == 0
    assert len(gh.closes()) == 1


@pytest.mark.parametrize("answer", [
    "comment 1002: done in row 3",   # a wrong id
    "comment 100: done in row 3",    # an id that is a prefix of the right one
    "comment 1001: maybe later",     # not one of the three answers
])
def test_row2_wrong_id_or_answer_is_not_accepted(tmp_path, answer, capsys):
    gh = FakeGh([comment(1001, AFTER, "a request")])
    assert run_close(58, _closing(tmp_path, answer + "\n"), repo=REPO, runner=gh) == 1
    assert "comment 1001 | 2026-10-16" in capsys.readouterr().out
    assert gh.closes() == []


# row 3: machine lines after the edit and a plain comment before it: closed, nothing printed by the check

def test_row3_machine_lines_and_an_older_comment_close_silently(tmp_path, capsys):
    gh = FakeGh([
        comment(2001, AFTER, "<!-- merge result -->\nmerged 4ed5deef"),
        comment(2002, AFTER, "Checkpoint 2 (ORCH 3302c1a1, #58). done: unit 1; next: close"),
        comment(2003, BEFORE, "A plain remark written before the last edit"),
    ])
    rc = run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh)
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert len(gh.closes()) == 1


# extra rows

def test_null_last_edit_uses_the_creation_time(tmp_path, capsys):
    gh = FakeGh([comment(3001, "2026-10-09T10:00:00Z", "before the creation"),
                 comment(3002, "2026-10-11T10:00:00Z", "after the creation")], edited=None)
    assert run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh) == 1
    out = capsys.readouterr().out
    assert "comment 3002 | 2026-10-11 | brain-bot | after the creation" in out
    assert "comment 3001" not in out


def test_dry_run_never_closes_and_refuses_the_same_way(tmp_path, capsys):
    gh = FakeGh([comment(1001, AFTER, "a question")])
    assert run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh, dry_run=True) == 1
    assert gh.closes() == []


def test_dry_run_with_every_comment_answered_says_it_would_close(tmp_path, capsys):
    gh = FakeGh([comment(1001, AFTER, "a question")])
    rc = run_close(58, _closing(tmp_path, "comment 1001: done in row 3\n"), repo=REPO, runner=gh, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out and "#58" in out
    assert gh.closes() == []


def test_owner_answer_after_the_edit_must_be_answered(tmp_path, capsys):
    gh = FakeGh([comment(4001, AFTER, "<!-- owner-answer -->\nyes, keep the three rows")])
    assert run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh) == 1
    assert "comment 4001 | 2026-10-16 | brain-bot | <!-- owner-answer -->" in capsys.readouterr().out
    assert gh.closes() == []


def test_not_planned_passes_the_reason(tmp_path):
    gh = FakeGh([])
    assert run_close(58, _closing(tmp_path, "Closing: dropped.\n"), repo=REPO, runner=gh, not_planned=True) == 0
    args = gh.closes()[0]
    assert args[args.index("--reason") + 1] == "not planned"


def test_the_tickets_repo_from_the_settings_is_the_default_repo(tmp_path, monkeypatch):
    # #323: with no --repo given, the closer reads and closes in the tickets repo of the settings, not in the repo
    # gh detects for the working directory (no `repo view` call any more)
    tickets = "owner/tickets"
    monkeypatch.setattr(core_settings, "tickets_repo", lambda *a, **k: tickets)
    gh = FakeGh([])
    assert run_close(58, _closing(tmp_path, "Closing.\n"), runner=gh) == 0
    assert not any(args[:2] == ["repo", "view"] for args in gh.calls)
    assert gh.graphql_variables and all(
        v["owner"] == "owner" and v["name"] == "tickets" for v in gh.graphql_variables)
    closes = gh.closes()
    assert len(closes) == 1
    assert closes[0][closes[0].index("--repo") + 1] == tickets


def test_comments_past_the_first_page_are_read(tmp_path, capsys):
    gh = FakeGh([comment(5001, BEFORE, "an old remark")],
                follow={"c1": ([comment(5002, AFTER, "a late question")], None)})
    assert run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh) == 1
    assert "comment 5002 | 2026-10-16 | brain-bot | a late question" in capsys.readouterr().out
    assert gh.closes() == []


def test_the_first_line_is_cut_to_120_characters(tmp_path, capsys):
    long_line = "x" * 300
    gh = FakeGh([comment(6001, AFTER, long_line)])
    run_close(58, _closing(tmp_path, "Closing.\n"), repo=REPO, runner=gh)
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("comment 6001"))
    assert line.split(" | ", 3)[3] == "x" * 120


def test_a_repo_without_a_slash_is_an_error_not_a_traceback(tmp_path):
    f = tmp_path / "c.md"
    f.write_text("done", encoding="utf-8")
    from tools.agentops.close_issue import CloseIssueError, read_issue
    with pytest.raises(CloseIssueError):
        read_issue(lambda *a, **k: None, "foo", 1)
