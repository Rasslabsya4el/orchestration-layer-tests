"""tools/agentops/dispatch.py (issue #142, unit 3): `dispatch --ensure` also keeps the always-running
monitor collector alive (`ensure_collector`). Every test drives an injected `popen` or `pid_alive`:
no real collector and no real dispatcher is started. `env={}` hides `PYTEST_CURRENT_TEST` from the
WMI guard (#261), the same way `test_agentops_dispatch_lock.py` does for an injected `popen`."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops.dispatch import ensure_collector, run_ensure

COLLECT_REL = Path("mods") / "pob-slots" / "bin" / "collect.py"
LOCK_REL = Path("mods") / "pob-slots" / "bin" / ".cache" / "collector.lock"
FAKE_PID = 4242


class _FakeProcess:
    """Stands in for both `subprocess.run` (Windows: `.stdout` carries the WMI `rc=.. pid=..`
    line) and `subprocess.Popen` (elsewhere: `.pid`)."""

    def __init__(self, pid: int = FAKE_PID):
        self.stdout = f"rc=0 pid={pid}"
        self.stderr = ""
        self.returncode = 0
        self.pid = pid


def _make_collect(root: Path, source: str | None = "pob-slots") -> Path:
    """A stand-in collector under `mods/<source>` plus the config naming that source (`monitor.source`)."""
    if source is not None:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "orchestration.json").write_text(
            json.dumps({"monitor": {"source": source, "collector_start_check_s": 0}}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")
    return collect


def _write_lock(root: Path, text: str) -> None:
    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(text, encoding="utf-8")


def _recording_popen(calls: list, pid: int = os.getpid()):
    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeProcess(pid)
    return fake


def _command_text(command) -> str:
    """The command as one readable line: the decoded PowerShell script on Windows, the argv
    joined otherwise."""
    if "-EncodedCommand" in command:
        encoded = command[command.index("-EncodedCommand") + 1]
        return base64.b64decode(encoded).decode("utf-16-le")
    return " ".join(str(part) for part in command)


# -- ensure_collector ----------------------------------------------------------------------------

def test_no_collect_py_is_absent_and_starts_nothing(tmp_path: Path):
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "absent"
    assert calls == []


def test_no_lock_starts_one_collector_windowless(tmp_path: Path):
    _make_collect(tmp_path)
    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls), env={})

    assert result == "started"
    assert len(calls) == 1
    command, kwargs = calls[0]
    text = _command_text(command)
    # the collector's own command ends `collect.py serve` (the WMI wrapper appends `>> out 2>&1`)
    assert re.search(r'collect\.py"?\s+serve\s*(>>|$|")', text), text
    if sys.platform == "win32":
        assert command[0] == "powershell"
        assert kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
        assert "ShowWindow = [uint16]0" in text
        assert "cmd /c" in text
        assert str(tmp_path / "artifacts" / "runs" / "collector.out") in text
    else:
        assert kwargs.get("start_new_session") is True


def test_live_pid_in_lock_is_live_and_starts_nothing(tmp_path: Path):
    _make_collect(tmp_path)
    _write_lock(tmp_path, json.dumps({"pid": os.getpid()}))
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "live"
    assert calls == []


def test_dead_pid_in_lock_starts(tmp_path: Path):
    _make_collect(tmp_path)
    _write_lock(tmp_path, json.dumps({"pid": 999999999}))  # no such process
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "started"
    assert len(calls) == 1


def test_garbage_lock_starts(tmp_path: Path):
    _make_collect(tmp_path)
    _write_lock(tmp_path, "not json {")
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "started"
    assert len(calls) == 1


@pytest.mark.parametrize("lock_text", [
    "[1, 2]", '"text"', "null", '{"no_pid": 1}', '{"pid": "x"}',
])
def test_lock_of_another_shape_starts(tmp_path: Path, lock_text: str):
    _make_collect(tmp_path)
    _write_lock(tmp_path, lock_text)
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "started"
    assert len(calls) == 1


def test_popen_raising_is_failed_with_one_stderr_line(tmp_path: Path, capsys):
    _make_collect(tmp_path)

    def boom(command, **kwargs):
        raise OSError("boom")

    assert ensure_collector(tmp_path, popen=boom, env={}) == "failed"
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert err_lines == ["dispatch --ensure: collector start failed: boom"]


def test_two_calls_with_a_live_collector_start_one_process_total(tmp_path: Path):
    _make_collect(tmp_path)
    calls: list = []

    def fake_serve(command, **kwargs):
        # the collector writes its own liveness record when it starts
        calls.append((command, kwargs))
        _write_lock(tmp_path, json.dumps({"pid": FAKE_PID}))
        return _FakeProcess(FAKE_PID)

    first = ensure_collector(tmp_path, popen=fake_serve, env={},
                             pid_alive=lambda pid: pid == FAKE_PID)
    second = ensure_collector(tmp_path, popen=fake_serve, env={},
                              pid_alive=lambda pid: pid == FAKE_PID)
    assert (first, second) == ("started", "live")
    assert len(calls) == 1


# -- run_ensure: the collector step after the dispatcher part -------------------------------------

def _fake_launch_pid(pid: int = 111):
    return lambda out_path: pid


def test_run_ensure_starts_dispatcher_and_collector_and_prints_collector_line(tmp_path: Path, capsys):
    _make_collect(tmp_path)
    calls: list = []
    rc = run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "dispatch.out",
                    launch=_fake_launch_pid(), root=tmp_path,
                    popen=_recording_popen(calls), env={})
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == ["started pid 111", "dispatch --ensure: collector started"]
    assert len(calls) == 1


def test_run_ensure_with_live_dispatcher_still_ensures_the_collector(tmp_path: Path, capsys):
    _make_collect(tmp_path)
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}), encoding="utf-8")
    calls: list = []
    rc = run_ensure(lock_path=lock_path, out_path=tmp_path / "dispatch.out",
                    launch=_fake_launch_pid(), root=tmp_path,
                    popen=_recording_popen(calls), env={})
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0].startswith(f"live pid {os.getpid()}")
    assert out[-1] == "dispatch --ensure: collector started"
    assert len(calls) == 1


def test_run_ensure_collector_absent_prints_no_collector_line(tmp_path: Path, capsys):
    rc = run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "dispatch.out",
                    launch=_fake_launch_pid(), root=tmp_path,
                    popen=_recording_popen([]), env={})
    out = capsys.readouterr().out
    assert rc == 0
    assert "collector" not in out


def test_run_ensure_collector_failure_keeps_dispatcher_return_code(tmp_path: Path, capsys):
    _make_collect(tmp_path)

    def boom(command, **kwargs):
        raise OSError("boom")

    # dispatcher-only case: the same launch with an absent collector gives rc 0
    rc_dispatcher_only = run_ensure(lock_path=tmp_path / "a.lock", out_path=tmp_path / "a.out",
                                    launch=_fake_launch_pid(), root=tmp_path / "nowhere",
                                    popen=boom, env={})
    rc = run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "dispatch.out",
                    launch=_fake_launch_pid(), root=tmp_path, popen=boom, env={})
    captured = capsys.readouterr()
    assert rc == rc_dispatcher_only == 0
    assert "dispatch --ensure: collector start failed: boom" in captured.err
    assert "dispatch --ensure: collector failed" in captured.out.splitlines()


def test_no_monitor_source_in_the_config_means_absent(tmp_path):
    """A repo whose config names no monitor source has no collector to keep alive, whatever mods/ holds."""
    _make_collect(tmp_path, source=None)
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "absent"
    assert calls == []


def test_the_collector_folder_comes_from_the_config_source(tmp_path):
    """`monitor.source` picks the mod folder: another source name starts that folder's collector."""
    other = tmp_path / "mods" / "other-mod" / "bin"
    other.mkdir(parents=True)
    (other / "collect.py").write_text("# other", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": "other-mod", "collector_start_check_s": 0}}), encoding="utf-8")
    calls: list = []
    assert ensure_collector(tmp_path, popen=_recording_popen(calls), env={}) == "started"
    assert "other-mod" in _command_text(calls[0][0])
