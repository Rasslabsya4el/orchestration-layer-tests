"""#278: a LIVE monitor collector whose process memory limit is not COLLECTOR_MEMORY_CAP_BYTES gets the 2 GB cap on the
next `ensure_collector` check (rules 1-6); a failed cap relaunches it through the restart path (rule 3), each pid is
handled once per dispatcher process (rule 4), a line that cannot be written goes to stderr (rule 5), and nothing is
read off Windows (rule 6).

No real process is started, stopped or capped: every test injects `table`, `stopper`, `popen`, `pid_alive`, `cap`,
`limit_of`, `sleep` and `wait_sleep`; file mtimes are set with `os.utime` in `tmp_path`.

    python -m pytest tests/test_issue278_collector_recap.py -q
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.dispatch import ensure_collector
from tools.agentops.orphans import ProcRow

COLLECT_REL = Path("mods") / "monitor-mod" / "bin" / "collect.py"
LOCK_REL = Path("mods") / "monitor-mod" / "bin" / ".cache" / "collector.lock"
OLD_PID = 1111
NEW_PID = 2222
CAP_BYTES = 2 * 1024 ** 3
LINE_CAPPED = "had no memory cap; capped at 2 GB"
UNREADABLE = "capped, limit not readable: the job name is taken"


class _FakeProcess:
    def __init__(self, pid: int):
        self.stdout = f"rc=0 pid={pid}"
        self.stderr = ""
        self.returncode = 0
        self.pid = pid


def _popen(calls: list):
    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeProcess(NEW_PID)
    return fake


class _Host:
    """A fake process table: the live collector OLD_PID until the stopper stops it; NEW_PID is the relaunch."""

    def __init__(self, rows: list):
        self.rows = rows
        self.stopped: list = []
        self.calls: list = []

    def stopper(self, pids):
        self.stopped.append(list(pids))
        return []

    def alive(self, pid: int) -> bool:
        return pid == NEW_PID or (pid == OLD_PID and not self.stopped)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    dispatch_mod._NOT_COLLECTOR_LOGGED.clear()
    dispatch_mod._REFUSED_LOGGED.clear()
    dispatch_mod._RECAP_HANDLED.clear()
    monkeypatch.setattr(dispatch_mod, "_caps_apply", lambda: True)  # the cap is wired on Windows only


def _live_collector(root: Path) -> _Host:
    """A monitor mod with its collector file, a live lock on OLD_PID, and that collector born after its newest input."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": "monitor-mod", "collector_start_check_s": 0}}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")
    os.utime(collect, (2000.0, 2000.0))
    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": OLD_PID}), encoding="utf-8")
    command = f'"C:\\Python\\python.exe" "{collect}" serve'
    return _Host([ProcRow(pid=OLD_PID, ppid=1, create_time=3000.0, command_line=command)])


def _run(root: Path, host: _Host, *, cap, limit_of) -> str:
    return ensure_collector(root, popen=_popen(host.calls), env={}, pid_alive=host.alive,
                            table=lambda: host.rows, stopper=host.stopper, sleep=lambda s: None,
                            wait_sleep=lambda s: None, cap=cap, limit_of=limit_of)


def _dispatch_lines(root: Path) -> list[str]:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_row1_live_collector_without_cap_is_capped_once_and_the_next_check_does_nothing(tmp_path: Path):
    host = _live_collector(tmp_path)
    limit: dict = {"value": None}
    seen: list = []

    def cap(pid, limit_bytes):
        seen.append((pid, limit_bytes))
        return None

    assert _run(tmp_path, host, cap=cap, limit_of=lambda pid: limit["value"]) == "live"
    assert seen == [(OLD_PID, CAP_BYTES)]
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert f"collector pid {OLD_PID} {LINE_CAPPED}" in lines[0]
    assert host.stopped == [] and host.calls == []

    limit["value"] = CAP_BYTES  # the second check reads the 2 GB cap
    assert _run(tmp_path, host, cap=cap, limit_of=lambda pid: limit["value"]) == "live"
    assert seen == [(OLD_PID, CAP_BYTES)]
    assert len(_dispatch_lines(tmp_path)) == 1


def test_row2_a_collector_already_at_2_gib_is_not_touched(tmp_path: Path):
    host = _live_collector(tmp_path)

    def cap(pid, limit_bytes):
        pytest.fail("the cap was called for a collector already at 2 GB")

    assert _run(tmp_path, host, cap=cap, limit_of=lambda pid: CAP_BYTES) == "live"
    assert _dispatch_lines(tmp_path) == []
    assert host.stopped == [] and host.calls == []


def test_row3_a_failed_cap_stops_and_relaunches_the_collector_with_one_line(tmp_path: Path):
    host = _live_collector(tmp_path)
    outcomes = iter(["jobcap: OpenProcess failed", None])  # the recap fails, the relaunch's cap succeeds
    cap_calls: list = []

    def cap(pid, limit_bytes):
        cap_calls.append((pid, limit_bytes))
        return next(outcomes)

    assert _run(tmp_path, host, cap=cap, limit_of=lambda pid: None) == "started"
    assert host.stopped == [[OLD_PID]]
    assert len(host.calls) == 1
    assert cap_calls == [(OLD_PID, CAP_BYTES), (NEW_PID, CAP_BYTES)]
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert str(OLD_PID) in lines[0] and str(NEW_PID) in lines[0] and "jobcap: OpenProcess failed" in lines[0]


def test_row4_an_unreadable_limit_is_capped_once_over_three_checks_of_the_same_pid(tmp_path: Path):
    host = _live_collector(tmp_path)
    cap_calls: list = []

    def cap(pid, limit_bytes):
        cap_calls.append(pid)
        return UNREADABLE

    for _ in range(3):
        assert _run(tmp_path, host, cap=cap, limit_of=lambda pid: None) == "live"
    assert cap_calls == [OLD_PID]
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert LINE_CAPPED in lines[0]
    assert host.stopped == []


def test_row5_an_unwritable_line_goes_to_stderr_and_the_capped_collector_stays_live(tmp_path: Path, monkeypatch,
                                                                                  capsys):
    def refuse(base, text):
        raise PermissionError(13, "Permission denied", str(base / "artifacts" / "runs" / "dispatch.out"))

    monkeypatch.setattr(dispatch_mod, "_append_dispatch_line", refuse)
    host = _live_collector(tmp_path)

    assert _run(tmp_path, host, cap=lambda pid, limit: None, limit_of=lambda pid: None) == "live"
    assert LINE_CAPPED in capsys.readouterr().err
    assert host.stopped == [] and host.calls == []


def test_row5_an_unwritable_relaunch_line_goes_to_stderr_and_the_relaunch_is_started(tmp_path: Path, monkeypatch,
                                                                                    capsys):
    def refuse(base, text):
        raise PermissionError(13, "Permission denied", str(base / "artifacts" / "runs" / "dispatch.out"))

    monkeypatch.setattr(dispatch_mod, "_append_dispatch_line", refuse)
    host = _live_collector(tmp_path)
    outcomes = iter(["jobcap: OpenProcess failed", None])

    assert _run(tmp_path, host, cap=lambda pid, limit: next(outcomes), limit_of=lambda pid: None) == "started"
    err = capsys.readouterr().err
    assert str(OLD_PID) in err and str(NEW_PID) in err and "jobcap: OpenProcess failed" in err
    assert host.stopped == [[OLD_PID]]


def test_row6_off_windows_nothing_is_read_or_capped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dispatch_mod, "_caps_apply", lambda: False)
    host = _live_collector(tmp_path)

    def never(*args):
        pytest.fail("off Windows the limit was read or the cap was called")

    assert _run(tmp_path, host, cap=never, limit_of=never) == "live"
    assert _dispatch_lines(tmp_path) == []
    assert host.stopped == [] and host.calls == []
