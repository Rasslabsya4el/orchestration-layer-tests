"""#1142: ORCH stays silent to the brain chat on a ticket under an open autopilot stretch.

The seam is `run_autopilot_stretch_command` (`python -m tools.agentops autopilot-stretch --issue N`): it
answers with the name of the stretch live on the issue or one of its ancestors (the same walk as the
dispatcher's `_autopilot_live_for`, #1129), or nothing. Fake `gh` and fake `claude`, no network, no heavy tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude
from tools.agentops.dispatch import IssueStatus, run_autopilot_stretch_command

TICKET = 1
MID = 80
GOAL = 90


class StretchFakeGh:
    """The reads the stretch walk makes: issue_status, parent_issue, latest_terms_comment, and
    the ORCH-side reads the dispatcher's constructor never makes. `fail` names the number whose
    read raises, the way a failed `gh` does."""

    def __init__(self) -> None:
        self.issues: dict[int, IssueStatus] = {}
        self.parents: dict[int, int] = {}
        self.terms: dict[int, str] = {}
        self.fail: set[int] = set()

    def issue_status(self, number: int) -> IssueStatus:
        if number in self.fail:
            raise RuntimeError(f"gh issue view {number} failed: HTTP 504")
        return self.issues[number]

    def parent_issue(self, number: int) -> int | None:
        if number in self.fail:
            raise RuntimeError(f"gh api issues/{number}/parent failed: HTTP 504")
        return self.parents.get(number)

    def latest_terms_comment(self, number: int) -> str | None:
        return self.terms.get(number)


def _tree(*, goal_labels=("autopilot",), goal_open: bool = True) -> StretchFakeGh:
    """TICKET under MID under GOAL; the goal carries `goal_labels` and is open or closed."""
    gh = StretchFakeGh()
    gh.issues[TICKET] = IssueStatus(number=TICKET, open=True, labels=("ready-for-agent",), assignees=())
    gh.issues[MID] = IssueStatus(number=MID, open=True, labels=(), assignees=())
    gh.issues[GOAL] = IssueStatus(number=GOAL, open=goal_open, labels=tuple(goal_labels), assignees=())
    gh.parents = {TICKET: MID, MID: GOAL}
    return gh


def _run(gh: StretchFakeGh, tmp_path: Path, issue: int = TICKET) -> tuple[int, str]:
    return run_autopilot_stretch_command(issue, claude=FakeClaude(), gh=gh,
                                         events_file=tmp_path / "events.jsonl")


def test_1_open_stretch_on_an_ancestor_prints_its_name(tmp_path: Path):
    rc, text = _run(_tree(), tmp_path)

    assert rc == 0
    assert text == f"autopilot #{GOAL}"


def test_1_no_labelled_ancestor_prints_nothing(tmp_path: Path):
    gh = _tree(goal_labels=())

    rc, text = _run(gh, tmp_path)

    assert rc == 0
    assert text == ""


def test_1_goal_labelled_autopilot_stuck_prints_nothing(tmp_path: Path):
    gh = _tree(goal_labels=("autopilot", "autopilot-stuck"))

    rc, text = _run(gh, tmp_path)

    assert rc == 0
    assert text == ""


def test_1_closed_goal_prints_nothing(tmp_path: Path):
    rc, text = _run(_tree(goal_open=False), tmp_path)

    assert rc == 0
    assert text == ""


@pytest.mark.parametrize("failing", [TICKET, MID, GOAL])
def test_1_failed_github_read_is_a_nonzero_exit_with_no_name(tmp_path: Path, failing: int):
    """The fallback: a failed read prints no stretch name (ORCH then sends its message as today) and
    exits non-zero with the reason, so the caller can tell a read failure from 'no stretch'."""
    gh = _tree()
    gh.fail.add(failing)

    rc, text = _run(gh, tmp_path)

    assert rc != 0
    assert f"#{TICKET}" in text and "not read" in text
    assert f"autopilot #{GOAL}" not in text


def test_1_cli_prints_the_name_on_stdout_and_nothing_on_failure(tmp_path: Path, monkeypatch, capsys):
    """The subcommand's own wiring: the name goes to stdout, a failed read goes to stderr with rc 1."""
    import tools.agentops.cli as cli

    monkeypatch.setattr(cli, "run_autopilot_stretch_command", lambda issue: (0, f"autopilot #{GOAL}"))
    assert cli.main(["autopilot-stretch", "--issue", str(TICKET)]) == 0
    out = capsys.readouterr()
    assert out.out == f"autopilot #{GOAL}\n" and out.err == ""

    monkeypatch.setattr(cli, "run_autopilot_stretch_command", lambda issue: (1, "not read"))
    assert cli.main(["autopilot-stretch", "--issue", str(TICKET)]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "not read" in out.err
