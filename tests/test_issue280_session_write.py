"""#280 unit 3 (session-retry): the `orch_session.json` write retries its `replace` on `PermissionError`
up to `STATE_REPLACE_RETRIES` (5) times, `STATE_REPLACE_WAIT_S` (0.05 s) apart; a write that still fails
logs ONE line naming the step (`write` or `replace`) and the error.

Platform-independent: `Path.replace` and `Path.write_text` are replaced by fakes, and the sleep is
injected, so nothing really waits. The real held-file cases (a reader holding the file on Windows)
are in tests/test_issue280_session_held.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops import dispatch
from tools.agentops.dispatch import Dispatcher

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tests._area_rule import _no_area_rule  # noqa: F401

REAL_REPLACE = Path.replace
REAL_WRITE_TEXT = Path.write_text


def _dispatcher(tmp_path: Path, lines: list[str]) -> Dispatcher:
    return Dispatcher(claude=FakeClaude(), gh=FakeGh(), events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      finishing_dir=tmp_path / "finishing", clock=FakeClock(), log=lines.append)


def _sleeps(calls: list[float]):
    return lambda seconds: calls.append(seconds)


def test_constants_are_five_retries_fifty_ms_apart():
    assert dispatch.STATE_REPLACE_RETRIES == 5
    assert dispatch.STATE_REPLACE_WAIT_S == pytest.approx(0.05)


def test_replace_refused_twice_then_succeeds_no_line(tmp_path, monkeypatch):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    (tmp_path / "orch_session.json").write_text(json.dumps({"old": True}), encoding="utf-8")
    attempts = []

    def flaky_replace(self, target):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "Access is denied")
        return REAL_REPLACE(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    waits: list[float] = []
    d._write_state(sleep=_sleeps(waits))
    assert len(attempts) == 3
    assert waits == [pytest.approx(0.05), pytest.approx(0.05)]
    assert json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8"))["sessions"] == []
    assert lines == []


def test_replace_refused_through_all_retries_logs_one_replace_line(tmp_path, monkeypatch):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    (tmp_path / "orch_session.json").write_text(json.dumps({"old": True}), encoding="utf-8")
    attempts = []

    def always_refused(self, target):
        attempts.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_refused)
    waits: list[float] = []
    d._write_state(sleep=_sleeps(waits))
    # one try, then the 5 retries, each after a 50 ms wait
    assert len(attempts) == dispatch.STATE_REPLACE_RETRIES + 1
    assert len(waits) == dispatch.STATE_REPLACE_RETRIES
    assert all(w == pytest.approx(0.05) for w in waits)
    assert len(lines) == 1
    assert "could not write orch_session.json" in lines[0]
    assert "step replace" in lines[0]
    assert "Access is denied" in lines[0]
    # the old file stays until a later tick's write succeeds
    assert json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8")) == {"old": True}


def test_other_replace_error_is_not_retried(tmp_path, monkeypatch):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    attempts = []

    def odd_error(self, target):
        attempts.append(1)
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(Path, "replace", odd_error)
    waits: list[float] = []
    d._write_state(sleep=_sleeps(waits))
    assert len(attempts) == 1
    assert waits == []
    assert len(lines) == 1
    assert "step replace" in lines[0] and "Invalid argument" in lines[0]


def test_tmp_write_failure_is_step_write_without_retry(tmp_path, monkeypatch):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    replaces = []

    def failing_write(self, data, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise PermissionError(13, "Permission denied")
        return REAL_WRITE_TEXT(self, data, *args, **kwargs)

    def counting_replace(self, target):
        replaces.append(1)
        return REAL_REPLACE(self, target)

    monkeypatch.setattr(Path, "write_text", failing_write)
    monkeypatch.setattr(Path, "replace", counting_replace)
    waits: list[float] = []
    d._write_state(sleep=_sleeps(waits))
    assert replaces == []
    assert waits == []
    assert len(lines) == 1
    assert "could not write orch_session.json" in lines[0]
    assert "step write" in lines[0]
    assert "Permission denied" in lines[0]


def test_default_sleep_is_used_when_none_injected(tmp_path, monkeypatch):
    """Without an injected sleep the call uses the real one, so a patched `time.sleep` is the one that runs."""
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    waits: list[float] = []
    monkeypatch.setattr(dispatch.time, "sleep", lambda s: waits.append(s))

    def always_refused(self, target):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", always_refused)
    d._write_state()
    assert len(waits) == dispatch.STATE_REPLACE_RETRIES
    assert len(lines) == 1
