"""#1108 unit 2: `python -m tools.agentops recount --since DATE`.

No network and no real `gh`: every `gh` call goes through an injected fake that records its arguments and answers
from a fixed JSON list. The seams under test are the candidates the lister returns and the lines the CLI prints.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date

import pytest

from tools.agentops import gh_call, recount
from tools.agentops.cli import main


class _FakeGh:
    """Stands in for `run_gh`: records each call, answers with one fixed reply."""

    def __init__(self, reply: object = None, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.reply = reply
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        stdout = json.dumps(self.reply) if self.reply is not None else ""
        return subprocess.CompletedProcess(["gh", *args], self.returncode, stdout, self.stderr)


def _comment(created: str, body: str) -> dict:
    return {"body": body, "createdAt": created, "author": {"login": "brain"}}


def _issue(number: int, closed: str, body: str = "", comments: list | None = None, title: str | None = None) -> dict:
    return {"number": number, "title": title or f"issue {number}", "closedAt": closed, "body": body,
            "comments": comments or []}


BLOCK = "## Recount\n- incident: the brain posts a rule without a count\n- how counted: `grep -c` over the logs\n- window: weekly\n- count: 3"
RESULT = "## Recount result: 3 -> 0, kept"


def _numbers(candidates) -> list[int]:
    return [c.number for c in candidates]


def test_block_in_body_is_listed() -> None:
    fake = _FakeGh([_issue(10, "2026-10-15T09:00:00Z", body=BLOCK)])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [10]


def test_block_in_a_later_comment_is_listed() -> None:
    issue = _issue(11, "2026-10-14T09:00:00Z", body="no block here",
                   comments=[_comment("2026-10-15T10:00:00Z", BLOCK)])
    fake = _FakeGh([issue])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [11]


def test_block_with_a_later_result_is_excluded() -> None:
    issue = _issue(12, "2026-10-14T09:00:00Z", body=BLOCK,
                   comments=[_comment("2026-10-16T10:00:00Z", RESULT)])
    fake = _FakeGh([issue])
    assert recount.find_candidates(date(2026, 10, 1), runner=fake) == []


def test_block_with_a_result_before_the_block_is_still_listed() -> None:
    issue = _issue(13, "2026-10-14T09:00:00Z", body="no block here",
                   comments=[_comment("2026-10-14T10:00:00Z", RESULT),
                             _comment("2026-10-15T10:00:00Z", BLOCK)])
    fake = _FakeGh([issue])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [13]


def test_a_first_waits_result_lists_the_issue_again_once() -> None:
    waits = _comment("2026-10-15T11:00:00Z", "## Recount result: 0 -> 0, waits")
    one = _issue(16, "2026-10-14T09:00:00Z", body=BLOCK, comments=[waits])
    two = _issue(17, "2026-10-14T09:00:00Z", body=BLOCK,
                 comments=[waits, _comment("2026-10-16T11:00:00Z", "## Recount result: 0 -> 0, waits")])
    fake = _FakeGh([one, two])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [16]


def test_no_block_is_excluded() -> None:
    issue = _issue(14, "2026-10-14T09:00:00Z", body="## For the agent\nplain text",
                   comments=[_comment("2026-10-15T10:00:00Z", "## Recount result: 1 -> 1, waits")])
    fake = _FakeGh([issue])
    assert recount.find_candidates(date(2026, 10, 1), runner=fake) == []


def test_result_heading_is_not_a_block() -> None:
    issue = _issue(15, "2026-10-14T09:00:00Z", body="",
                   comments=[_comment("2026-10-15T10:00:00Z", RESULT)])
    fake = _FakeGh([issue])
    assert recount.find_candidates(date(2026, 10, 1), runner=fake) == []


def test_candidates_come_oldest_first_by_closed_date() -> None:
    fake = _FakeGh([
        _issue(21, "2026-10-15T20:00:00Z", body=BLOCK),
        _issue(22, "2026-10-12T08:00:00Z", body=BLOCK),
        _issue(23, "2026-10-13T08:00:00Z", body=BLOCK),
    ])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [22, 23, 21]


def test_issue_closed_before_the_date_is_excluded() -> None:
    fake = _FakeGh([
        _issue(31, "2026-09-30T23:59:00Z", body=BLOCK),
        _issue(32, "2026-10-01T00:00:00Z", body=BLOCK),
    ])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [32]


def test_block_shows_its_first_three_lines_only() -> None:
    fake = _FakeGh([_issue(41, "2026-10-15T09:00:00Z", body=BLOCK)])
    (candidate,) = recount.find_candidates(date(2026, 10, 1), runner=fake)
    assert candidate.lines == [
        "## Recount",
        "- incident: the brain posts a rule without a count",
        "- how counted: `grep -c` over the logs",
    ]


def test_block_lines_skip_blank_lines_and_stop_at_next_heading() -> None:
    body = "## Recount\n\n- incident: one\n## Other section\n- not part of the block"
    fake = _FakeGh([_issue(42, "2026-10-15T09:00:00Z", body=body)])
    (candidate,) = recount.find_candidates(date(2026, 10, 1), runner=fake)
    assert candidate.lines == ["## Recount", "- incident: one"]


def test_one_list_call_and_no_per_candidate_call() -> None:
    fake = _FakeGh([
        _issue(51, "2026-10-14T09:00:00Z", body=BLOCK),
        _issue(52, "2026-10-14T10:00:00Z", body=BLOCK),
        _issue(53, "2026-10-14T11:00:00Z", body=BLOCK, comments=[_comment("2026-10-15T10:00:00Z", RESULT)]),
    ])
    assert _numbers(recount.find_candidates(date(2026, 10, 1), runner=fake)) == [51, 52]
    assert len(fake.calls) == 1
    args = fake.calls[0]
    assert args[:2] == ["issue", "list"]
    assert "closed:>=2026-10-01" in args
    assert "--state" in args and args[args.index("--state") + 1] == "closed"
    assert "number,title,closedAt,body,comments" in args


def test_default_runner_is_gh_call_run_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    assert recount.run_gh is gh_call.run_gh


def test_cli_prints_the_lines_of_each_candidate(monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    fake = _FakeGh([
        _issue(61, "2026-10-15T20:00:00Z", body=BLOCK, title="second one"),
        _issue(62, "2026-10-12T08:00:00Z", body=BLOCK, title="first one"),
    ])
    monkeypatch.setattr(recount, "run_gh", fake)
    assert main(["recount", "--since", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert out.index("#62 first one") < out.index("#61 second one")
    assert "closed 2026-10-12" in out and "closed 2026-10-15" in out
    assert "    ## Recount" in out
    assert "    - how counted: `grep -c` over the logs" in out
    assert "- window: weekly" not in out
    assert len(fake.calls) == 1


def test_cli_with_no_candidates_says_so(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(recount, "run_gh", _FakeGh([_issue(71, "2026-10-15T09:00:00Z", body="none")]))
    assert main(["recount", "--since", "2026-10-01"]) == 0
    assert capsys.readouterr().out.strip() == "no closed issue with a Recount block to recount since 2026-10-01"


def test_cli_gh_failure_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(recount, "run_gh", _FakeGh(returncode=1, stderr="HTTP 503: Service Unavailable"))
    assert main(["recount", "--since", "2026-10-01"]) == 2
    assert "gh" in capsys.readouterr().out


def test_cli_bad_date_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["recount", "--since", "15.10.2026"]) == 2
    assert "--since" in capsys.readouterr().out
