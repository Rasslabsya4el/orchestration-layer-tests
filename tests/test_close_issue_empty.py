"""#165 close-refuse: `python -m tools.agentops close` refuses an empty closing text, and a closing text that names no
acceptance row when the issue body has an acceptance block. Fake gh only (no live call).

Each test maps to an acceptance row of #165: rows 1-2 empty and whitespace-only text, rows 3-4 an acceptance block
with a text that names no row and one that names row 1, row 5 a body with no acceptance block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.close_issue import run_close  # noqa: E402
from tests.test_close_issue import REPO, AFTER, FakeGh, _closing, comment  # noqa: E402

ACCEPTANCE_BODY = (
    "Map #22. Some facts.\n\n"
    "### Acceptance rows\n"
    "1. Test: empty comment file -> rc 1\n"
    "2. Test: a file of spaces and newlines -> as row 1\n"
)


class BodyGh(FakeGh):
    """The fake gh of test_close_issue, with the issue body the close step now reads."""

    def __init__(self, comments: list[dict], body: str):
        super().__init__(comments)
        self.body = body

    def _graphql(self, query: str, variables: dict) -> dict:
        data = super()._graphql(query, variables)
        if "repository" in data["data"]:
            data["data"]["repository"]["issue"]["body"] = self.body
        return data


# row 1: an empty closing comment file refuses, and gh is never asked to close

def test_row1_empty_closing_text_refuses_and_leaves_the_issue_open(tmp_path, capsys):
    gh = BodyGh([], body="")
    rc = run_close(165, _closing(tmp_path, ""), repo=REPO, runner=gh)
    assert rc == 1
    assert "refused: empty closing text" in capsys.readouterr().out
    assert gh.closes() == []


# row 2: a file of spaces, tabs and newlines is as empty as a file with no text

def test_row2_whitespace_only_closing_text_refuses_like_row1(tmp_path, capsys):
    gh = BodyGh([], body="")
    rc = run_close(165, _closing(tmp_path, "   \n\t\n  \n"), repo=REPO, runner=gh)
    assert rc == 1
    assert "refused: empty closing text" in capsys.readouterr().out
    assert gh.closes() == []


# row 3: an acceptance block in the body, and a closing text that names no row, refuses

def test_row3_closing_text_naming_no_acceptance_row_refuses(tmp_path, capsys):
    gh = BodyGh([], body=ACCEPTANCE_BODY)
    rc = run_close(165, _closing(tmp_path, "done\n"), repo=REPO, runner=gh)
    assert rc == 1
    assert "refused: closing text names no acceptance row" in capsys.readouterr().out
    assert gh.closes() == []


# row 4: the same body, a closing text that names row 1, closes with that text (today's call)

def test_row4_closing_text_naming_a_row_closes_with_that_text(tmp_path, capsys):
    text = "- Row 1: pytest -> 3 passed\n"
    gh = BodyGh([], body=ACCEPTANCE_BODY)
    rc = run_close(165, _closing(tmp_path, text), repo=REPO, runner=gh)
    assert rc == 0
    assert "refused" not in capsys.readouterr().out
    closes = gh.closes()
    assert len(closes) == 1
    assert f"--comment={text}" in closes[0]


# row 5: a body with no acceptance block does not ask for a row: a plain closing text closes

def test_row5_body_without_acceptance_block_closes_with_any_text(tmp_path, capsys):
    gh = BodyGh([], body="Plain body with no check block.\n")
    rc = run_close(165, _closing(tmp_path, "not a task\n"), repo=REPO, runner=gh)
    assert rc == 0
    assert "refused" not in capsys.readouterr().out
    assert len(gh.closes()) == 1


# the unanswered-comment refusal of #58 still applies on an acceptance body

def test_unanswered_comment_still_refuses_on_an_acceptance_body(tmp_path, capsys):
    gh = BodyGh([comment(7001, AFTER, "a question")], body=ACCEPTANCE_BODY)
    rc = run_close(165, _closing(tmp_path, "- Row 1: pytest -> 3 passed\n"), repo=REPO, runner=gh)
    assert rc == 1
    assert "comment 7001" in capsys.readouterr().out
    assert gh.closes() == []
