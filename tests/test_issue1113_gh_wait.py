"""#1113 rule 3: a `gh` call that hit the primary rate limit waits until the `resetAt` it read: no retry storm.

Fakes only (a counting `subprocess.Popen`, an injected clock), no network, no `gh`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools.agentops import gh_call  # noqa: E402
from tools.agentops.gh_call import GhCalledProcessError, run_gh  # noqa: E402

LIMIT_TEXT = "gh: API rate limit exceeded for user ID 1. If you reach out to GitHub Support, please include the request ID"
T0 = 1_000_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Rig:
    """Counts spawned processes; `script` answers each call by its argv."""

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


def rate_limit_json(graphql_reset: float, core_reset: float) -> str:
    return json.dumps({"resources": {"graphql": {"reset": int(graphql_reset)}, "core": {"reset": int(core_reset)}}})


@pytest.fixture
def rig(monkeypatch):
    r = Rig()
    clock = Clock()
    monkeypatch.setattr(gh_call.subprocess, "Popen", r.popen)
    monkeypatch.setattr(gh_call, "_now", clock)
    monkeypatch.setattr(gh_call, "_blocked_until", {})
    r.clock = clock
    return r


def test_rate_limit_blocks_following_calls_until_reset(rig):
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (0, rate_limit_json(T0 + 120, T0 + 999), "")
    first = run_gh(["issue", "list"])
    assert first.returncode == 1
    assert rig.main_calls() == 1
    spawned_after_first = len(rig.spawned)

    for _ in range(5):
        blocked = run_gh(["issue", "view", "5"])
        assert blocked.returncode == 1
        assert blocked.transient is True
        assert blocked.stderr.startswith("gh: API rate limit exceeded, waiting until ")
        assert len(blocked.stderr.rsplit(" ", 1)[1].split(":")) == 3  # HH:MM:SS
    assert len(rig.spawned) == spawned_after_first  # 0 processes for the 5 blocked calls

    rig.clock.now = T0 + 121
    rig.main_answer = (0, "fine", "")
    run_gh(["issue", "list"])
    assert rig.main_calls() == 2  # one process after the reset


def test_rest_call_reads_core_reset_and_graphql_is_not_blocked_by_it(rig):
    rig.main_answer = (1, "", "HTTP 403: API rate limit exceeded")
    rig.reset_answer = (0, rate_limit_json(T0 + 10, T0 + 300), "")
    run_gh(["api", "repos/x/y/issues"])  # REST: the core resource
    spawned = len(rig.spawned)
    rig.main_answer = (0, "ok", "")
    rig.clock.now = T0 + 100  # past the graphql reset, before the core reset
    assert run_gh(["api", "repos/x/y/issues"]).returncode == 1  # core still blocked
    assert len(rig.spawned) == spawned
    assert run_gh(["issue", "list"]).returncode == 0  # graphql resource was never blocked
    assert len(rig.spawned) == spawned + 1


def test_failure_that_is_not_a_rate_limit_never_blocks(rig):
    rig.main_answer = (1, "", "gh: Not Found (HTTP 404)")
    for _ in range(3):
        run_gh(["issue", "view", "9"])
    assert rig.main_calls() == 3
    assert not any(a[1:3] == ["api", "rate_limit"] for a in rig.spawned)
    rig.main_answer = (1, "", "You have exceeded a secondary rate limit")
    run_gh(["issue", "view", "9"])
    run_gh(["issue", "view", "9"])
    assert rig.main_calls() == 5  # the secondary limit is left to the callers' own retry


def test_failed_reset_read_blocks_for_sixty_seconds_only(rig):
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (1, "", "gh: HTTP 502 Bad Gateway")
    run_gh(["issue", "list"])
    spawned = len(rig.spawned)
    rig.clock.now = T0 + 59
    assert run_gh(["issue", "list"]).transient is True
    assert len(rig.spawned) == spawned
    rig.clock.now = T0 + 61
    run_gh(["issue", "list"])
    assert len(rig.spawned) > spawned


def test_rate_limit_read_itself_is_never_blocked(rig):
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (0, rate_limit_json(T0 + 120, T0 + 120), "")
    run_gh(["issue", "list"])
    before = len(rig.spawned)
    result = run_gh(["api", "rate_limit"])
    assert len(rig.spawned) == before + 1
    assert "waiting until" not in result.stderr


def test_check_true_raises_transient_error_for_a_blocked_call(rig):
    rig.main_answer = (1, "", LIMIT_TEXT)
    rig.reset_answer = (0, rate_limit_json(T0 + 120, T0 + 120), "")
    run_gh(["issue", "list"])
    spawned = len(rig.spawned)
    with pytest.raises(GhCalledProcessError) as caught:
        run_gh(["issue", "list"], check=True)
    assert caught.value.transient is True
    assert caught.value.returncode == 1
    assert "waiting until" in caught.value.stderr
    assert len(rig.spawned) == spawned
