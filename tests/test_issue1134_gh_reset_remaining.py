"""#1134: the rate-limit gate blocks a resource only while its budget is empty (`remaining == 0`).

A call that fails with "API rate limit exceeded" reads the reset once. When that read shows budget left (the window
already rolled between the failed call and the read), the gate must not block: the next call starts `gh`.
Fakes only (a counting `subprocess.Popen`, an injected clock), no network, no `gh`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools.agentops import gh_call  # noqa: E402
from tools.agentops.gh_call import run_gh  # noqa: E402

LIMIT_TEXT = "gh: API rate limit exceeded for user ID 1. If you reach out to GitHub Support, please include the request ID"
T0 = 1_000_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Rig:
    """Counts spawned processes; answers the `api rate_limit` read and every other call separately."""

    def __init__(self) -> None:
        self.spawned: list[list[str]] = []
        self.main_answer: tuple[int, str, str] = (0, "ok", "")
        self.reset_answer: tuple[int, str, str] = (0, "", "")

    def popen(self, argv, **kwargs):
        self.spawned.append(list(argv))
        rig = self

        class _P:
            args = list(argv)
            pid = 1
            stdout = stderr = stdin = None

            def __init__(self) -> None:
                if argv[1:3] == ["api", "rate_limit"]:
                    self.returncode, self._out, self._err = rig.reset_answer
                else:
                    self.returncode, self._out, self._err = rig.main_answer

            def communicate(self, input=None, timeout=None):
                return self._out, self._err

            def kill(self):
                pass

        return _P()

    def main_calls(self) -> int:
        return sum(1 for a in self.spawned if a[1:3] != ["api", "rate_limit"])


def rate_limit_json(graphql_remaining: int, graphql_reset: float) -> str:
    """The shape of `gh api rate_limit`: per resource, `remaining` and `reset` (epoch seconds)."""
    return json.dumps({"resources": {
        "graphql": {"limit": 5000, "remaining": graphql_remaining, "reset": int(graphql_reset)},
        "core": {"limit": 5000, "remaining": graphql_remaining, "reset": int(graphql_reset)},
    }})


@pytest.fixture
def rig(monkeypatch):
    r = Rig()
    clock = Clock()
    monkeypatch.setattr(gh_call.subprocess, "Popen", r.popen)
    monkeypatch.setattr(gh_call, "_now", clock)
    monkeypatch.setattr(gh_call, "_blocked_until", {})
    r.clock = clock
    return r


def test_window_already_rolled_with_budget_left_does_not_block(rig):
    """(1) The failed call's window rolled before the read: remaining 4,845, reset an hour away. Not blocked."""
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (0, rate_limit_json(4845, T0 + 3600), "")
    assert run_gh(["issue", "list"]).returncode == 1
    assert rig.main_calls() == 1

    rig.main_answer = (0, "ok", "")
    result = run_gh(["issue", "view", "5"])
    assert result.returncode == 0
    assert rig.main_calls() == 2  # the next call started gh


def test_empty_budget_still_blocks_until_reset(rig):
    """(2) The same read with remaining 0: the next call is blocked until the reset, without starting gh."""
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (0, rate_limit_json(0, T0 + 3600), "")
    run_gh(["issue", "list"])
    spawned = len(rig.spawned)

    blocked = run_gh(["issue", "view", "5"])
    assert blocked.returncode == 1
    assert blocked.transient is True
    assert blocked.stderr.startswith("gh: API rate limit exceeded, waiting until ")
    assert len(rig.spawned) == spawned  # 0 processes while blocked

    rig.clock.now = T0 + 3601
    rig.main_answer = (0, "ok", "")
    assert run_gh(["issue", "view", "5"]).returncode == 0
    assert len(rig.spawned) == spawned + 1


def test_failed_reset_read_keeps_sixty_second_fallback(rig):
    """(3) A failed read keeps the 60 s block: blocked at +59 s, through at +61 s."""
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (1, "", "gh: HTTP 502 Bad Gateway")
    run_gh(["issue", "list"])
    spawned = len(rig.spawned)

    rig.clock.now = T0 + 59
    assert run_gh(["issue", "list"]).transient is True
    assert len(rig.spawned) == spawned

    rig.clock.now = T0 + 61
    rig.main_answer = (0, "ok", "")
    run_gh(["issue", "list"])
    assert len(rig.spawned) == spawned + 1
