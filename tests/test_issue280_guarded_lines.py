"""#280 unit 2 (guard-lines): every `dispatch.out` line of the collector path goes through `_dispatch_line_or_stderr`,
so a line the file refuses never changes the result of `ensure_collector`.

Row 3: `_append_dispatch_line` raising `PermissionError` at each of the six places (the "is not the monitor
collector" line, "stop raised", "stop refused", "did not clear its lock", the 2 GB cap-end line, and the
"collector died after start" line): the result is the one a writable file gives (measured, not guessed), never
`"failed"`, and the line goes to stderr.

No real process is started, stopped or capped: every test injects `table`, `stopper`, `popen`, `pid_alive`, `cap`,
`sleep` and `wait_sleep`; the cap is wired through `_caps_apply` (set to True, as the #276 and #278 tests do).

    python -m pytest tests/test_issue280_guarded_lines.py -q
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.dispatch import ensure_collector
from tools.agentops.orphans import ProcRow

COLLECT_REL = Path("mods") / "monitor-mod" / "bin" / "collect.py"
LOCK_REL = Path("mods") / "monitor-mod" / "bin" / ".cache" / "collector.lock"
OLD_PID = 1111
NEW_PID = 2222


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


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    dispatch_mod._NOT_COLLECTOR_LOGGED.clear()
    dispatch_mod._REFUSED_LOGGED.clear()
    dispatch_mod._RECAP_HANDLED.clear()
    monkeypatch.setattr(dispatch_mod, "_caps_apply", lambda: True)  # the cap is wired on Windows only


def _make_mod(root: Path) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    monitor = {"source": "monitor-mod", "collector_start_check_s": 0}
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": monitor}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")
    os.utime(collect, (2000.0, 2000.0))  # the newest input: touched after a collector born at 1000.0
    return collect


def _write_lock(root: Path, pid: int) -> None:
    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def _command(collect: Path) -> str:
    return f'"C:\\Python\\python.exe" "{collect}" serve'


def _collector_out(root: Path, text: str) -> None:
    path = root / "artifacts" / "runs" / "collector.out"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dispatch_lines(root: Path) -> list[str]:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _kwargs(calls: list, **extra) -> dict:
    base = {"popen": _popen(calls), "env": {}, "sleep": lambda s: None, "wait_sleep": lambda s: None,
            "cap": lambda pid, limit: None}
    base.update(extra)
    return base


# Each place: the situation the existing tests build (#186, #276, #278), the result a writable file gives,
# and the text of the line the place writes.

def _not_the_collector(root: Path) -> dict:
    collect = _make_mod(root)
    _write_lock(root, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0,
                    command_line='"C:\\Python\\python.exe" "C:\\other\\unrelated.py" serve')]
    return _kwargs([], pid_alive=lambda pid: pid == OLD_PID, table=lambda: rows,
                   stopper=lambda pids: [])


def _stop_raised(root: Path) -> dict:
    collect = _make_mod(root)
    _write_lock(root, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_command(collect))]

    def stopper(pids):
        raise RuntimeError("stop exploded")

    return _kwargs([], pid_alive=lambda pid: pid == OLD_PID, table=lambda: rows, stopper=stopper)


def _stop_refused(root: Path) -> dict:
    collect = _make_mod(root)
    _write_lock(root, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_command(collect))]
    refused = [SimpleNamespace(pid=OLD_PID, action="refused")]
    return _kwargs([], pid_alive=lambda pid: True, table=lambda: rows, stopper=lambda pids: refused)


def _lock_not_cleared(root: Path) -> dict:
    collect = _make_mod(root)
    _write_lock(root, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_command(collect))]
    return _kwargs([], pid_alive=lambda pid: pid == OLD_PID,  # never clears
                   table=lambda: rows, stopper=lambda pids: [])


def _cap_ended(root: Path) -> dict:
    _make_mod(root)
    _write_lock(root, OLD_PID)  # a dead lock: the old collector is gone
    _collector_out(root, "collect serve: MemoryError: ended at the memory cap\n"
                         "Traceback (most recent call last):\nMemoryError\n")
    return _kwargs([], pid_alive=lambda pid: pid == NEW_PID)


def _died_after_start(root: Path) -> dict:
    _make_mod(root)
    _write_lock(root, OLD_PID)
    _collector_out(root, "Traceback (most recent call last):\nValueError: nope\n")
    return _kwargs([], pid_alive=lambda pid: False)


# place -> (situation, the result with a writable file, the text that must reach stderr when the file refuses)
PLACES = {
    "not_the_monitor_collector": (_not_the_collector, "live", "is not the monitor collector"),
    "stop_raised": (_stop_raised, "live", "stop raised"),
    "stop_refused": (_stop_refused, "live", "stop refused by the safety chain"),
    "lock_not_cleared": (_lock_not_cleared, "live", "did not clear its lock"),
    "cap_ended": (_cap_ended, "started", "ended at the 2 GB cap; restarted"),
    "died_after_start": (_died_after_start, "died", "collector died after start"),
}


def _run(root: Path, setup) -> str:
    """One `ensure_collector` call on a fresh tree: the once-per-pid sets are process state, cleared per run."""
    dispatch_mod._NOT_COLLECTOR_LOGGED.clear()
    dispatch_mod._REFUSED_LOGGED.clear()
    dispatch_mod._RECAP_HANDLED.clear()
    calls: list = []
    kwargs = setup(root)
    kwargs["popen"] = _popen(calls)
    return ensure_collector(root, **kwargs)


@pytest.mark.parametrize("place", list(PLACES))
def test_a_refused_line_keeps_the_writable_result_and_goes_to_stderr(tmp_path: Path, monkeypatch, capsys, place: str):
    setup, expected, text = PLACES[place]

    writable = tmp_path / "writable"
    baseline = _run(writable, setup)
    assert baseline == expected, baseline
    assert any(text in line for line in _dispatch_lines(writable)), _dispatch_lines(writable)
    capsys.readouterr()  # the baseline run writes nothing to stderr; drop anything it did

    def refuse(base, line):
        raise PermissionError(13, "Permission denied", str(base / "artifacts" / "runs" / "dispatch.out"))

    monkeypatch.setattr(dispatch_mod, "_append_dispatch_line", refuse)
    refused = tmp_path / "refused"
    result = _run(refused, setup)

    assert result == baseline, result
    assert result != "failed"
    assert text in capsys.readouterr().err
    assert _dispatch_lines(refused) == []
