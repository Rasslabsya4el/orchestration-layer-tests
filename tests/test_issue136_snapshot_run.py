"""#136 part B unit 3: `monitor-snapshots start|stop|status|run` and `dispatch --ensure` starting the snapshots.

No real process is started: the launcher, the stopper and the loop are injected, and every lock lives under
`tmp_path`. The launcher receives the subcommand's argument list (`["monitor-snapshots", "run"]`).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tools.agentops import dispatch
from tools.agentops import monitor_snapshots as ms


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_lock(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started": _now_str()}), encoding="utf-8")


def _no_launch(args, *, out_path):
    raise AssertionError(f"no launch expected, got {args!r}")


# -- start (ensure) -----------------------------------------------------------------------------

def test_start_with_no_lock_launches_the_run_command_once(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"
    calls: list[tuple[list, Path]] = []

    def fake_launch(args, *, out_path):
        calls.append((list(args), out_path))
        return 4242

    rc = ms.ensure_snapshots(lock_path=lock, out_path=tmp_path / "out", launch=fake_launch)

    assert rc == 0
    assert calls == [(["monitor-snapshots", "run"], tmp_path / "out")]
    assert capsys.readouterr().out.strip() == "started pid 4242"


def test_start_with_a_live_lock_launches_nothing(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"
    _write_lock(lock, os.getpid())

    rc = ms.ensure_snapshots(lock_path=lock, out_path=tmp_path / "out", launch=_no_launch)

    assert rc == 0
    assert capsys.readouterr().out.strip() == f"live pid {os.getpid()}"


def test_start_whose_launch_raises_is_rc_1_with_one_stderr_line(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"

    def failing_launch(args, *, out_path):
        raise OSError("WMI Win32_Process.Create failed")

    rc = ms.ensure_snapshots(lock_path=lock, out_path=tmp_path / "out", launch=failing_launch)

    assert rc == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "monitor snapshots" in err[0] and "WMI" in err[0]


# -- run_ensure: the snapshot start rides along, its failure never changes the dispatcher's rc --

def test_run_ensure_with_a_live_dispatcher_still_calls_snapshots_ensure(tmp_path: Path, capsys):
    dlock = tmp_path / "dispatcher.lock"
    _write_lock(dlock, os.getpid())
    calls: list[int] = []

    rc = dispatch.run_ensure(lock_path=dlock, out_path=tmp_path / "d.out", launch=_no_launch,
                             root=tmp_path, snapshots_ensure=lambda: calls.append(1) or 0)

    assert rc == 0
    assert calls == [1], "a live dispatcher must not skip the snapshot start"
    assert "live pid" in capsys.readouterr().out


def test_run_ensure_rc_unchanged_when_snapshots_ensure_raises(tmp_path: Path, capsys):
    dlock = tmp_path / "dispatcher.lock"
    _write_lock(dlock, os.getpid())

    def boom() -> int:
        raise OSError("snapshot start blew up")

    rc_live = dispatch.run_ensure(lock_path=dlock, out_path=tmp_path / "d.out", launch=_no_launch,
                                  root=tmp_path, snapshots_ensure=boom)
    assert rc_live == 0
    assert "snapshot start blew up" in capsys.readouterr().err

    dead_lock = tmp_path / "dead" / "dispatcher.lock"  # no lock at all: the dispatcher part starts one
    rc_started = dispatch.run_ensure(lock_path=dead_lock, out_path=tmp_path / "d.out",
                                     launch=lambda **kw: 555, root=tmp_path, snapshots_ensure=boom)
    assert rc_started == 0
    assert capsys.readouterr().out.strip().endswith("started pid 555")

    def failing_dispatch(**kw) -> int:
        raise OSError("dispatcher launch failed")

    rc_failed = dispatch.run_ensure(lock_path=tmp_path / "other" / "dispatcher.lock",
                                    out_path=tmp_path / "d.out", launch=failing_dispatch,
                                    root=tmp_path, snapshots_ensure=boom)
    assert rc_failed == 1, "a dispatcher start failure keeps its own rc 1 whatever the snapshots did"


# -- stop ---------------------------------------------------------------------------------------

def test_stop_calls_the_stopper_with_the_lock_pid(tmp_path: Path):
    lock = tmp_path / "monitor_snapshots.lock"
    _write_lock(lock, os.getpid())
    stopped: list[list[int]] = []

    rc = ms.stop_snapshots(lock_path=lock, stopper=lambda pids: stopped.append(list(pids)) or [])

    assert rc == 0
    assert stopped == [[os.getpid()]]


def test_stop_with_no_live_holder_stops_nothing(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"  # absent: not live

    rc = ms.stop_snapshots(lock_path=lock, stopper=lambda pids: pytest.fail("nothing to stop"))

    assert rc == 0
    assert "not live" in capsys.readouterr().out


# -- run ----------------------------------------------------------------------------------------

def test_run_with_a_live_lock_exits_0_without_looping(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"
    _write_lock(lock, os.getpid())

    rc = ms.run_locked(lock_path=lock, out_root=tmp_path / "snaps",
                       loop=lambda *a, **kw: pytest.fail("a second run must not loop"))

    assert rc == 0
    assert capsys.readouterr().out.strip() == f"live pid {os.getpid()}"
    assert lock.exists(), "the live holder's lock is not ours to remove"


def test_run_writes_its_own_lock_while_looping_and_removes_it_on_exit(tmp_path: Path):
    lock = tmp_path / "monitor_snapshots.lock"
    seen: list[dict] = []

    def fake_loop(out_root, **kw):
        seen.append(json.loads(lock.read_text(encoding="utf-8")))

    rc = ms.run_locked(lock_path=lock, out_root=tmp_path / "snaps", loop=fake_loop)

    assert rc == 0
    assert seen and seen[0]["pid"] == os.getpid()
    assert not lock.exists()


# -- status -------------------------------------------------------------------------------------

def test_status_reports_not_live_and_the_newest_snapshot_folder(tmp_path: Path):
    out_root = tmp_path / "snaps"
    for name in ("20261017-100000", "20261017-120000", "not-a-snapshot"):
        (out_root / name).mkdir(parents=True)

    text = ms.status_text(lock_path=tmp_path / "monitor_snapshots.lock", out_root=out_root)

    assert "not live" in text
    assert "20261017-120000" in text
    assert "20261017-100000" not in text


# -- the launch factoring in dispatch.py ---------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.mark.skipif(sys.platform != "win32", reason="the WMI command is Windows-only")
def test_launch_detached_module_runs_the_subcommand_windowless(tmp_path: Path):
    calls: list[list] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _FakeCompletedProcess("rc=0 pid=4343")

    out_path = tmp_path / "snap.out"
    pid = dispatch.launch_detached_module(["monitor-snapshots", "run"], cwd=tmp_path,
                                          out_path=out_path, popen=fake_run, env={})

    assert pid == 4343
    encoded = calls[0][calls[0].index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "-m tools.agentops monitor-snapshots run >>" in script
    assert str(out_path) in script
    assert "ShowWindow = [uint16]0" in script


# -- review fixes: order, stdout shape, injected dispatcher launch --------------------------------

def test_dispatcher_launch_runs_before_the_snapshot_start(tmp_path: Path, capsys):
    """Review 1: the dispatcher part comes first; the snapshot start never delays it."""
    events: list[str] = []

    def fake_dispatch(*, out_path: Path) -> int:
        events.append("dispatcher")
        return 555

    rc = dispatch.run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "d.out",
                             launch=fake_dispatch, root=tmp_path,
                             snapshots_ensure=lambda: events.append("snapshots") or 0)

    assert rc == 0
    assert events == ["dispatcher", "snapshots"]


def test_live_dispatcher_prints_first_then_the_snapshot_start_runs(tmp_path: Path, capsys):
    dlock = tmp_path / "dispatcher.lock"
    _write_lock(dlock, os.getpid())
    events: list[str] = []

    def snapshots() -> int:
        events.append(f"snapshots after {capsys.readouterr().out.strip()!r}")
        return 0

    rc = dispatch.run_ensure(lock_path=dlock, out_path=tmp_path / "d.out", launch=_no_launch,
                             root=tmp_path, snapshots_ensure=snapshots)

    assert rc == 0
    assert events == [f"snapshots after 'live pid {os.getpid()}'"]


def test_a_failed_dispatcher_start_still_runs_the_snapshot_start_and_keeps_rc_1(tmp_path: Path):
    events: list[str] = []

    def failing_dispatch(*, out_path: Path) -> int:
        raise OSError("dispatcher launch failed")

    rc = dispatch.run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "d.out",
                             launch=failing_dispatch, root=tmp_path,
                             snapshots_ensure=lambda: events.append("snapshots") or 0)

    assert rc == 1
    assert events == ["snapshots"]


# -- stdout: the snapshot tick never prints the dispatcher's shape --------------------------------

def test_snapshot_tick_prints_nothing_when_already_live(tmp_path: Path, capsys):
    lock = tmp_path / "monitor_snapshots.lock"
    _write_lock(lock, os.getpid())

    rc = ms.ensure_snapshots(lock_path=lock, out_path=tmp_path / "out", launch=_no_launch,
                             prefix="monitor snapshots")

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_snapshot_tick_prints_the_prefixed_line_only_when_it_started(tmp_path: Path, capsys):
    rc = ms.ensure_snapshots(lock_path=tmp_path / "monitor_snapshots.lock", out_path=tmp_path / "out",
                             launch=lambda args, *, out_path: 4242, prefix="monitor snapshots")

    assert rc == 0
    assert capsys.readouterr().out.strip() == "monitor snapshots: started pid 4242"


def test_run_ensure_stdout_has_the_dispatcher_line_and_the_prefixed_snapshot_line(tmp_path: Path, capsys):
    rc = dispatch.run_ensure(
        lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "d.out",
        launch=lambda *, out_path: 555, root=tmp_path,
        snapshots_ensure=lambda: ms.ensure_snapshots(lock_path=tmp_path / "monitor_snapshots.lock",
                                                     out_path=tmp_path / "s.out",
                                                     launch=lambda args, *, out_path: 4242,
                                                     prefix="monitor snapshots"),
    )

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["started pid 555", "monitor snapshots: started pid 4242"]


# -- an injected dispatcher launch means no real snapshot start by default -----------------------

def test_run_ensure_with_an_injected_launch_and_no_snapshots_ensure_starts_no_snapshot(
        tmp_path: Path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ms, "ensure_snapshots", lambda **kw: calls.append(kw) or 0)

    rc = dispatch.run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "d.out",
                             launch=lambda *, out_path: 555, root=tmp_path)

    assert rc == 0
    assert calls == [], "the real snapshot start runs only with the real dispatcher launcher"
