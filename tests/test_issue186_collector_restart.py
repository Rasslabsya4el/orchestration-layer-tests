"""tools/agentops/dispatch.py `ensure_collector` (issue 186): a live monitor collector older than the files it runs
is stopped through the stopper and replaced; a younger one, or a pid that is not this mod's `collect.py serve`,
is left alone. The dispatcher's `after` step (`mods.register`) calls `collector_tick` at most once per 60 s.
No real process is started or stopped: every test injects `table`, `stopper`, `popen`, `pid_alive`, `sleep`
and `wait_sleep`, and file mtimes are set with `os.utime` in `tmp_path`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.dispatch import collector_tick, ensure_collector
from tools.agentops.extensions import Registry
from tools.agentops.orphans import ProcRow

COLLECT_REL = Path("mods") / "monitor-mod" / "bin" / "collect.py"
LOCK_REL = Path("mods") / "monitor-mod" / "bin" / ".cache" / "collector.lock"
OLD_PID = 1111
NEW_PID = 2222


class _FakeProcess:
    """Stands in for `subprocess.run` (Windows: `.stdout` carries the WMI `rc=.. pid=..` line) and `Popen`."""

    def __init__(self, pid: int):
        self.stdout = f"rc=0 pid={pid}"
        self.stderr = ""
        self.returncode = 0
        self.pid = pid


def _recording_popen(calls: list, pid: int):
    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeProcess(pid)
    return fake


@pytest.fixture(autouse=True)
def _fresh_logged_pids():
    """The once-per-pid log sets are process state: every test starts with them empty."""
    dispatch_mod._NOT_COLLECTOR_LOGGED.clear()
    dispatch_mod._REFUSED_LOGGED.clear()


def _make_mod(root: Path, addon: str | None = None) -> Path:
    """A stand-in monitor mod (`mods/monitor-mod/bin/collect.py`) and the config naming it, plus an optional add-on."""
    monitor: dict = {"source": "monitor-mod", "collector_start_check_s": 0}
    if addon is not None:
        monitor["addon"] = addon
        (root / addon).parent.mkdir(parents=True, exist_ok=True)
        (root / addon).write_text("# stand-in add-on\n", encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": monitor}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")
    return collect


def _write_lock(root: Path, pid: int) -> None:
    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def _collector_command(collect: Path) -> str:
    return f'"C:\\Python\\python.exe" "{collect}" serve'


def _dispatch_lines(root: Path) -> list[str]:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def test_collector_born_before_a_touched_collect_py_is_stopped_once_and_replaced(tmp_path: Path):
    collect = _make_mod(tmp_path)
    _set_mtime(collect, 2000.0)  # the newest file: touched after the collector started
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_collector_command(collect))]
    stopped: list = []

    def stopper(pids):
        stopped.append(list(pids))
        return []

    def alive(pid: int) -> bool:
        return pid == NEW_PID or (pid == OLD_PID and not stopped)

    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={}, pid_alive=alive,
                              table=lambda: rows, stopper=stopper, sleep=lambda s: None,
                              wait_sleep=lambda s: None)

    assert result == "started"
    assert stopped == [[OLD_PID]]
    assert len(calls) == 1
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert str(OLD_PID) in lines[0] and str(NEW_PID) in lines[0] and "collect.py" in lines[0]


def test_collector_born_after_its_newest_file_is_left_alone(tmp_path: Path):
    collect = _make_mod(tmp_path)
    _set_mtime(collect, 2000.0)
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=3000.0, command_line=_collector_command(collect))]
    stopped: list = []
    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={},
                              pid_alive=lambda pid: pid == OLD_PID, table=lambda: rows,
                              stopper=lambda pids: stopped.append(list(pids)) or [],
                              sleep=lambda s: None, wait_sleep=lambda s: None)

    assert result == "live"
    assert stopped == []
    assert calls == []
    assert _dispatch_lines(tmp_path) == []


def test_live_pid_whose_command_line_is_not_the_mod_collector_is_never_stopped(tmp_path: Path):
    collect = _make_mod(tmp_path)
    _set_mtime(collect, 2000.0)
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0,
                    command_line='"C:\\Python\\python.exe" "C:\\other\\unrelated.py" serve')]
    stopped: list = []
    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={},
                              pid_alive=lambda pid: pid == OLD_PID, table=lambda: rows,
                              stopper=lambda pids: stopped.append(list(pids)) or [],
                              sleep=lambda s: None, wait_sleep=lambda s: None)

    assert result == "live"
    assert stopped == []
    assert calls == []
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert str(OLD_PID) in lines[0] and "not the monitor collector" in lines[0]


def test_lock_that_does_not_clear_within_ten_seconds_logs_once_and_starts_nothing(tmp_path: Path):
    collect = _make_mod(tmp_path)
    _set_mtime(collect, 2000.0)
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_collector_command(collect))]
    stopped: list = []
    waits: list = []
    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={},
                              pid_alive=lambda pid: pid == OLD_PID,  # never clears
                              table=lambda: rows,
                              stopper=lambda pids: stopped.append(list(pids)) or [],
                              sleep=lambda s: None, wait_sleep=waits.append)

    assert result == "live"
    assert stopped == [[OLD_PID]]
    assert calls == []                      # no second collector started this tick
    assert 10 <= sum(waits) < 11            # bounded: the 10 s wait, nothing more
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert str(OLD_PID) in lines[0]


def test_newest_file_may_be_the_add_on_and_the_log_names_it(tmp_path: Path):
    collect = _make_mod(tmp_path, addon="addons/monitor_addon.tsx")
    _set_mtime(collect, 1500.0)
    addon = tmp_path / "addons" / "monitor_addon.tsx"
    _set_mtime(addon, 2000.0)  # newer than the collector's start below, the collector file is older
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1800.0, command_line=_collector_command(collect))]
    stopped: list = []
    calls: list = []

    def alive(pid: int) -> bool:
        return pid == NEW_PID or (pid == OLD_PID and not stopped)

    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={}, pid_alive=alive,
                              table=lambda: rows, stopper=lambda pids: stopped.append(list(pids)) or [],
                              sleep=lambda s: None, wait_sleep=lambda s: None)

    assert result == "started"
    assert stopped == [[OLD_PID]]
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1 and "monitor_addon.tsx" in lines[0], lines


def _step_core() -> Registry:
    """A fresh registry holding only the monitor mod's own registration (the step `mods.register` adds), loaded the
    way a dispatcher loads its local modules: through the loader, never a direct import of the local module."""
    core = Registry()
    core.load(["tools.agentops.mods"])
    return core


def test_dispatcher_step_calls_ensure_on_first_tick_then_not_until_sixty_seconds_later():
    now = [0.0]
    calls: list = []
    stub = SimpleNamespace(clock=lambda: now[0], log=lambda line: None,
                           collector_ensure=lambda: calls.append(now[0]) or "live")
    core = _step_core()

    for at in (0.0, 30.0, 59.0):
        now[0] = at
        core.run_ticks(stub, None, phase="after")
    assert calls == [0.0]

    now[0] = 60.0
    core.run_ticks(stub, None, phase="after")
    assert calls == [0.0, 60.0]


def test_an_exception_in_the_collector_step_does_not_fail_the_tick():
    lines: list = []

    def boom() -> str:
        raise RuntimeError("collector check exploded")

    stub = SimpleNamespace(clock=lambda: 0.0, log=lines.append, collector_ensure=boom)
    core = _step_core()

    core.run_ticks(stub, None, phase="after")  # must not raise
    assert len(lines) == 1 and "collector check exploded" in lines[0], lines

    # the step itself also never raises, with no registry in front of it
    collector_tick(stub, None)


def test_a_stop_refused_by_the_safety_chain_starts_nothing_and_logs_once_per_process(tmp_path: Path):
    collect = _make_mod(tmp_path)
    _set_mtime(collect, 2000.0)
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_collector_command(collect))]
    calls: list = []
    refused = [SimpleNamespace(pid=OLD_PID, action="refused")]

    for _ in range(2):  # two ticks
        result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={},
                                  pid_alive=lambda pid: True, table=lambda: rows, stopper=lambda pids: refused,
                                  sleep=lambda s: None, wait_sleep=lambda s: None)
        assert result == "live"

    assert calls == []
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1 and "refused" in lines[0], lines


def test_a_tools_agentops_module_the_collector_imports_counts_as_its_file(tmp_path: Path):
    collect = _make_mod(tmp_path)
    collect.write_text("def f():\n    from tools.agentops import monitor_pipeline as mp\n", encoding="utf-8")
    _set_mtime(collect, 500.0)
    module = tmp_path / "tools" / "agentops" / "monitor_pipeline.py"
    module.parent.mkdir(parents=True)
    module.write_text("# stand-in\n", encoding="utf-8")
    _set_mtime(module, 2000.0)  # newer than the collector process
    _write_lock(tmp_path, OLD_PID)
    rows = [ProcRow(pid=OLD_PID, ppid=1, create_time=1000.0, command_line=_collector_command(collect))]
    stopped: list = []

    def stopper(pids):
        stopped.append(list(pids))
        return []

    calls: list = []
    result = ensure_collector(tmp_path, popen=_recording_popen(calls, NEW_PID), env={},
                              pid_alive=lambda pid: pid == NEW_PID or (pid == OLD_PID and not stopped),
                              table=lambda: rows, stopper=stopper, sleep=lambda s: None, wait_sleep=lambda s: None)

    assert result == "started" and stopped == [[OLD_PID]]
    assert "monitor_pipeline.py" in _dispatch_lines(tmp_path)[0]


def test_the_collector_step_does_nothing_outside_machine_mode_normal():
    calls: list = []
    stub = SimpleNamespace(clock=lambda: 0.0, log=lambda line: None, collector_ensure=lambda: calls.append(1),
                           _machine_mode_now="play")
    collector_tick(stub, None)
    assert calls == []
    stub._machine_mode_now = "normal"
    collector_tick(stub, None)
    assert calls == [1]
