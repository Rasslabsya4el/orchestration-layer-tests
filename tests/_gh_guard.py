"""Test support (#168, rule 1 of #148): no real `gh` in the #148 test files.

A #148 test file does `from tests._gh_guard import _no_real_gh  # noqa: F401`: importing the autouse fixture into the
module namespace activates it for every test of that file. It replaces `gh_call._spawn_gh`, the one place a `gh`
process starts (so `run_gh` and `_read_reset` both), with a function that records the call and raises.

The raise alone is not enough: `_read_reset` catches every Exception and falls back to a minute, so the teardown
asserts that nothing was recorded. A test that needs a reset injects it (`rate_limit_reset=`, or a monkeypatch of
`gh_call.rate_limit_reset`); a test of the real spawn over a fake `Popen` restores `REAL_SPAWN`.
"""

from __future__ import annotations

import pytest

from tools.agentops import gh_call

REAL_SPAWN = gh_call._spawn_gh  # captured at import, before any test patches it


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    calls: list[list[str]] = []

    def guard(args, **kwargs):
        calls.append(list(args))
        raise AssertionError(f"real gh in a #148 test: {list(args)}")

    monkeypatch.setattr(gh_call, "_spawn_gh", guard)
    yield calls
    assert not calls, f"real gh in a #148 test: {calls}"
