"""#1129: a stretch of an autopilot is live from its start to its end, not only while one session is `working`.

Through `Dispatcher` with its fakes, as `tests/test_agentops_dispatch_brain_notice.py` does. The stretch is the
goal labelled `autopilot` that the dispatcher itself starts from: open, not `autopilot-stuck`, and not past its
`Autopilot terms` deadline (`tools/agentops/autopilot.py` `autopilot_goals` and `_tick_autopilot`). The end is
that goal dropping out of that set; between two sessions the label, not a session, is what says it is live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_agentops_dispatch_brain_notice import NoticeClaude, NoticeGh, _dispatcher
from tools.agentops.dispatch import Dispatcher, IssueStatus

GOAL = 90  # the goal the dispatcher starts a stretch on
MID = 80  # a parent between the goal and the ticket
WAIT = 1  # the held ticket that gets parked
PAST = "Autopilot terms\n```\ndeadline: 2000-01-01T00:00:00+00:00\n```"
FUTURE = "Autopilot terms\n```\ndeadline: 2999-01-01T00:00:00+00:00\n```"


class StretchGh(NoticeGh):
    """The notice's gh fake plus the one read the stretch check adds: the latest `Autopilot terms` body."""

    def __init__(self) -> None:
        super().__init__()
        self.terms: dict[int, str] = {}

    def latest_terms_comment(self, number: int) -> str | None:
        return self.terms.get(number)


def _tree(tmp_path: Path, *, goal_labels=("autopilot",), goal_open: bool = True,
          session_state: str | None = None, terms: str | None = None):
    """A ticket WAIT held by an ORCH session, under MID, under GOAL. `session_state` is the state of the
    goal's `autopilot #90` session (`None`: no such session); `terms` is the goal's terms comment."""

    claude = NoticeClaude()
    gh = StretchGh()
    gh.titles = {WAIT: "Fix the first thing"}
    gh.parents = {WAIT: MID, MID: GOAL}
    gh.issues[GOAL] = IssueStatus(number=GOAL, open=goal_open, labels=tuple(goal_labels), assignees=())
    gh.issues[MID] = IssueStatus(number=MID, open=True, labels=(), assignees=())
    if session_state is not None:
        claude.add_session("ap-goal", name=f"autopilot #{GOAL}", state=session_state, status="idle")
    if terms is not None:
        gh.terms[GOAL] = terms
    d = _dispatcher(tmp_path, claude, gh)
    d.log_lines = []
    d.log = d.log_lines.append
    d.tick()
    gh.claim(WAIT)
    d.tick()
    assert d.current is not None and WAIT in d.current.held
    gh.newest = {WAIT: "ORCH s: parked on the owner's answer"}
    gh.park(WAIT)
    return claude, gh, d


def _skips(d: Dispatcher) -> list[str]:
    return [line for line in d.log_lines if f"#{WAIT} skipped" in line]


# -- row 1: the goal is labelled `autopilot`, its last session is done/stopped or gone, the stretch
#    has not ended: the parked notice for a child two levels down is skipped and logged.

@pytest.mark.parametrize("session_state", ["done", "stopped", None])
def test_1_between_sessions_a_labelled_goal_skips_a_child_two_levels_down(tmp_path: Path, session_state):
    claude, gh, d = _tree(tmp_path, session_state=session_state)

    d.tick()

    assert claude.notices() == []
    skips = _skips(d)
    assert len(skips) == 1 and f"skipped, autopilot autopilot #{GOAL} is live" in skips[0]


def test_1_a_future_deadline_keeps_the_stretch_live_between_sessions(tmp_path: Path):
    claude, gh, d = _tree(tmp_path, session_state="done", terms=FUTURE)

    d.tick()

    assert claude.notices() == []
    assert len(_skips(d)) == 1


# -- row 2: the stretch has ended (label removed, goal closed, goal stuck, deadline passed): notice sent.

@pytest.mark.parametrize("ended", ["label removed", "goal closed", "goal stuck", "deadline passed"])
def test_2_an_ended_stretch_lets_the_parked_notice_through(tmp_path: Path, ended):
    kwargs = {
        "label removed": {"goal_labels": ()},
        "goal closed": {"goal_open": False},
        "goal stuck": {"goal_labels": ("autopilot", "autopilot-stuck")},
        "deadline passed": {"terms": PAST},
    }[ended]
    claude, gh, d = _tree(tmp_path, session_state="done", **kwargs)

    d.tick()

    assert len(claude.notices()) == 1
    assert claude.notices()[0].startswith(f"#{WAIT} Fix the first thing: parked:")
    assert _skips(d) == []


# -- row 3: a ticket outside any stretch's tree is sent as today.

def test_3_a_ticket_outside_any_stretch_tree_is_sent(tmp_path: Path):
    claude = NoticeClaude()
    gh = StretchGh()
    gh.titles = {WAIT: "Fix the first thing"}
    gh.parents = {WAIT: MID}  # MID has no parent; the labelled goal is elsewhere
    gh.issues[MID] = IssueStatus(number=MID, open=True, labels=(), assignees=())
    gh.issues[GOAL] = IssueStatus(number=GOAL, open=True, labels=("autopilot",), assignees=())
    d = _dispatcher(tmp_path, claude, gh)
    d.log_lines = []
    d.log = d.log_lines.append
    d.tick()
    gh.claim(WAIT)
    d.tick()
    gh.newest = {WAIT: "ORCH s: parked"}
    gh.park(WAIT)

    d.tick()

    assert len(claude.notices()) == 1
    assert _skips(d) == []
