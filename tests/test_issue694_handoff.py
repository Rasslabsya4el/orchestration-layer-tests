"""#694: a finishing session is told so once; an issue another live session claims leaves the old
session's `held` in the same tick and the old session is told once to stop all work on it."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import mark_finishing
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue
from tests.test_agentops_dispatch_finishing import _dispatcher
from tests.test_agentops_dispatch import IssueStatus
from tests._area_rule import _no_area_rule  # noqa: F401


def test_park_finishing_new_claim_merge_result(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    overlaps: list[set[int]] = []

    def tick():
        r = d.tick()
        live = [c.held for c in d.sessions]
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                overlaps.append(a & b)
        return r

    tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(1)
    tick()
    # park on merge: A lets #1 go (unassigned, ready again), then says finishing
    gh.issues[1] = IssueStatus(number=1, open=True, labels=("ready-for-agent",), assignees=())
    gh._candidates = [_issue(1, 1)]
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    tick()  # A finishing and told; B starts offered [1]
    assert len(claude.started) == 2
    b = next(iter({s["id"] for s in claude.list_sessions()} - {a}))
    tick()  # A's let-go is seen
    gh.claim(1)
    tick()  # B claims #1
    # the merge result arrives (issue re-readied by the daemon): A must not get it again
    gh.issues[1] = IssueStatus(number=1, open=True, labels=("ready-for-agent",), assignees=("bot",))
    tick()
    tick()

    to_a = [m for t, m in claude.waked if t == next(c.name or c.session for c in d.sessions if c.session == a)]
    assert len(to_a) == 2, to_a
    assert to_a[0].startswith("You are finishing") and f"finishing --session {a}" in to_a[0]
    assert to_a[1].startswith(f"#1 is now held by {b}: post nothing on it")
    cur_a = next(c for c in d.sessions if c.session == a)
    cur_b = next(c for c in d.sessions if c.session == b)
    assert 1 not in cur_a.held and 1 in cur_b.held
    assert all(not o for o in overlaps), overlaps
