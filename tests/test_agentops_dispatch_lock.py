"""tools/agentops/dispatch.py (issue #201): one dispatcher at a time, and `--ensure`. Every test
drives fakes -- no real `claude.exe`, `gh`, or a second live dispatcher process; a real short-lived
`python -c "pass"` subprocess stands in for "a process that is gone", the same pattern
`test_agentops_merge.py::test_dead_holder_is_taken_over_at_once` uses for the merge lock this
reuses `pid_alive` from."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from timing_bound import bound
from pathlib import Path

import pytest

from tools.agentops.dispatch import (
    DispatcherLockHeld,
    dispatcher_lock,
    dispatcher_status,
    launch_detached_dispatcher,
    run_dispatch,
    run_ensure,
)
from tools.agentops.util import now as _now_str
import tools.agentops.cli as cli_mod


# -- one dispatcher at a time --------------------------------------------------------------------

def test_a_second_dispatcher_beside_a_live_one_exits_fast_nonzero_starting_nothing(tmp_path: Path):
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}), encoding="utf-8")

    started = time.monotonic()
    rc = run_dispatch(once=True, lock_path=lock_path, args=None, dry_run=True)
    elapsed = time.monotonic() - started

    assert rc != 0
    # bound = max(floor, factor x a CPU spin timed here): load that slows the spin slows the call (#824)
    bound_s = bound(5.0, 33.0)
    assert elapsed < bound_s, f"took {elapsed:.1f}s, bound {bound_s:.1f}s"
    # 0 ORCH started, 0 sessions adopted: `run_dispatch` returns from the lock check alone, never
    # reaching `find_claude_exe`/`Dispatcher` (which would need a real `claude.exe`/`gh`).
    # the lock file is untouched -- still the first (live) holder's
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_dispatcher_lock_raises_dispatcher_lock_held_with_the_live_pid(tmp_path: Path):
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}), encoding="utf-8")
    with pytest.raises(DispatcherLockHeld) as excinfo:
        with dispatcher_lock(lock_path):
            pass
    assert excinfo.value.pid == os.getpid()


def test_a_lock_left_by_a_dead_process_is_taken_over_at_the_first_try(tmp_path: Path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    assert dead.wait(timeout=10) == 0

    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": dead_pid, "started": "irrelevant"}), encoding="utf-8")

    started = time.monotonic()
    with dispatcher_lock(lock_path):
        holder = json.loads(lock_path.read_text())
        assert holder["pid"] == os.getpid()
    elapsed = time.monotonic() - started

    bound_s = bound(1.0, 6.0)
    assert elapsed < bound_s, f"waited {elapsed:.1f}s for a dead holder, bound {bound_s:.1f}s"
    assert not lock_path.exists()  # released on the way out


def test_dispatcher_status_reads_a_live_and_a_dead_holder(tmp_path: Path):
    lock_path = tmp_path / "dispatcher.lock"
    assert dispatcher_status(lock_path) == (False, None)  # no lock file at all

    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}), encoding="utf-8")
    alive, pid = dispatcher_status(lock_path)
    assert alive is True
    assert pid == os.getpid()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    assert dead.wait(timeout=10) == 0
    lock_path.write_text(json.dumps({"pid": dead_pid, "started": "irrelevant"}), encoding="utf-8")
    assert dispatcher_status(lock_path) == (False, dead_pid)


# -- a reused pid is not a live holder (issue #235) ------------------------------------------------

@pytest.fixture
def live_child():
    """A live process this test itself owns and kills at the end -- stands in for "somebody
    else" now holding a pid the lock names."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _hours_before_now_str(hours: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))


@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row1_a_reused_pid_with_a_stale_started_is_not_live(tmp_path: Path, live_child):
    """Row 1: `live_child`'s pid is a live process of this test, but the lock's `started` claims
    it began 1 hour before that process was created -- exactly the reused-pid shape #235's own
    report measured. Before the fix (`pid_alive` alone) this reads as live and the lock as held;
    after it, not live, and the lock is taken over at the first try."""
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(
        json.dumps({"pid": live_child.pid, "started": _hours_before_now_str(1.0)}),
        encoding="utf-8",
    )

    assert dispatcher_status(lock_path) == (False, live_child.pid)

    started = time.monotonic()
    with dispatcher_lock(lock_path):
        holder = json.loads(lock_path.read_text())
        assert holder["pid"] == os.getpid()
    elapsed = time.monotonic() - started
    bound_s = bound(1.0, 6.0)
    assert elapsed < bound_s, f"waited {elapsed:.1f}s for a stale (reused-pid) holder, bound {bound_s:.1f}s"
    assert not lock_path.exists()


def test_row2_the_true_holder_with_its_own_started_is_live(tmp_path: Path):
    """Row 2: the lock written by the live process itself (its own pid and a `started` matching
    when it actually began) is the ordinary case -- still live, still held."""
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": _now_str()}), encoding="utf-8")

    assert dispatcher_status(lock_path) == (True, os.getpid())
    with pytest.raises(DispatcherLockHeld) as excinfo:
        with dispatcher_lock(lock_path):
            pass
    assert excinfo.value.pid == os.getpid()


@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row6_ensure_on_a_reused_pid_lock_starts_a_new_dispatcher_not_reports_live(
    tmp_path: Path, live_child, capsys,
):
    """Row 6: `dispatch --ensure` against row 1's lock starts a new dispatcher (the spawn
    injected, no real `claude.exe`) and must never answer `live pid <reused>`."""
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(
        json.dumps({"pid": live_child.pid, "started": _hours_before_now_str(1.0)}),
        encoding="utf-8",
    )

    def fake_launch(*, out_path: Path) -> int:
        return 555555

    rc = run_ensure(lock_path=lock_path, out_path=tmp_path / "dispatch.out", launch=fake_launch,
                    root=tmp_path)

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "started pid 555555"
    assert "live pid" not in out


def test_the_lock_is_released_after_a_normal_run(tmp_path: Path):
    lock_path = tmp_path / "dispatcher.lock"
    with dispatcher_lock(lock_path):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_the_lock_is_released_even_when_the_body_raises(tmp_path: Path):
    lock_path = tmp_path / "dispatcher.lock"
    with pytest.raises(RuntimeError):
        with dispatcher_lock(lock_path):
            raise RuntimeError("boom")
    assert not lock_path.exists()


# -- `--ensure` -------------------------------------------------------------------------------

def test_ensure_with_none_live_starts_one_and_exits_0(tmp_path: Path, capsys):
    lock_path = tmp_path / "dispatcher.lock"
    out_path = tmp_path / "dispatch.out"
    launched: list[dict] = []

    def fake_launch(*, out_path):
        launched.append({"out_path": out_path})
        return 4242

    rc = run_ensure(lock_path=lock_path, out_path=out_path, launch=fake_launch, root=tmp_path)

    assert rc == 0
    assert len(launched) == 1
    assert "started pid 4242" in capsys.readouterr().out


def test_ensure_with_one_live_starts_nothing_and_exits_0(tmp_path: Path, capsys):
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}), encoding="utf-8")
    launched: list[dict] = []

    def fake_launch(*, out_path):
        launched.append({"out_path": out_path})
        return 9999

    rc = run_ensure(lock_path=lock_path, out_path=tmp_path / "dispatch.out", launch=fake_launch,
                    root=tmp_path)

    assert rc == 0
    assert launched == []  # 0 started
    assert f"live pid {os.getpid()}" in capsys.readouterr().out


def test_launch_detached_dispatcher_appends_to_out_path_and_passes_no_max_hours(tmp_path: Path):
    """A fake `popen`/`run` stands in for the real spawn (never a real subprocess in a test); this
    only checks the command built, not a real detached launch. #248: on Windows the dispatcher is
    started through WMI (a `powershell` call, `_launch_via_wmi`), so the dispatcher's own command
    line (`... -m tools.agentops dispatch >> out_path 2>&1`) lives inside the `.ps1` text handed to
    that call, not as a literal `subprocess` command list; off Windows it is still a literal list."""
    out_path = tmp_path / "dispatch.out"
    out_path.write_text("existing\n", encoding="utf-8")

    calls: list[dict] = []

    class _FakeProc:
        pid = 5150

    class _FakeCompletedProcess:
        stdout = "rc=0 pid=5150"
        stderr = ""
        returncode = 0

    def fake_spawn(command, **kwargs):
        entry = {"command": command, "kwargs": kwargs}
        if sys.platform == "win32":
            encoded = command[command.index("-EncodedCommand") + 1]
            entry["ps1_text"] = base64.b64decode(encoded).decode("utf-16-le")
        calls.append(entry)
        if sys.platform == "win32":
            return _FakeCompletedProcess()
        return _FakeProc()

    pid = launch_detached_dispatcher(cwd=tmp_path, out_path=out_path, popen=fake_spawn, env={})

    assert pid == 5150
    assert len(calls) == 1
    command = calls[0]["command"]
    if sys.platform == "win32":
        assert command[0] == "powershell"
        assert "-NonInteractive" in command
        assert "-EncodedCommand" in command
        assert "-File" not in command
        ps1_text = calls[0]["ps1_text"]
        assert "-m tools.agentops dispatch" in ps1_text
        assert "--max-hours" not in ps1_text
        assert "--ensure" not in ps1_text
        assert str(out_path) in ps1_text  # stdout/stderr appended through cmd /c ... >> out 2>&1
    else:
        assert command[-1] == "dispatch"
        assert "--max-hours" not in command
        assert "--ensure" not in command
        # the file was opened in append mode, not truncated
        assert out_path.read_text(encoding="utf-8").startswith("existing\n")


# -- wired through `python -m tools.agentops dispatch --ensure` -------------------------------

def test_cli_dispatch_ensure_routes_to_run_ensure_not_run_dispatch(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "run_ensure", lambda: calls.append("ensure") or 0)
    monkeypatch.setattr(cli_mod, "run_dispatch", lambda **kw: calls.append("dispatch") or 0)

    rc = cli_mod.main(["dispatch", "--ensure"])

    assert rc == 0
    assert calls == ["ensure"]


def test_cli_dispatch_without_ensure_still_routes_to_run_dispatch(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "run_ensure", lambda: calls.append("ensure") or 0)
    monkeypatch.setattr(cli_mod, "run_dispatch", lambda **kw: calls.append("dispatch") or 0)

    rc = cli_mod.main(["dispatch", "--once"])

    assert rc == 0
    assert calls == ["dispatch"]


# -- #357: one loop per REPO, not per checkout -----------------------------------------------------

def test_a_worktree_and_its_main_checkout_share_one_lock_path(tmp_path: Path):
    from tools.agentops.worktree import main_checkout_root
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt1" / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
    assert main_checkout_root(wt).resolve() == main.resolve()
    assert main_checkout_root(main).resolve() == main.resolve()
    assert main_checkout_root(tmp_path / "nogit") == tmp_path / "nogit"


def test_the_module_lock_path_is_under_the_main_checkout():
    from tools.agentops import dispatch
    from tools.agentops.worktree import ROOT, main_checkout_root
    assert dispatch.LOCK_PATH == main_checkout_root(ROOT) / "var" / "agentops" / "dispatcher.lock"


def test_a_live_holder_message_names_its_pid_and_start(tmp_path: Path, capsys):
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": _now_str()}), encoding="utf-8")
    started_text = json.loads(lock_path.read_text())["started"]
    assert run_dispatch(once=True, lock_path=lock_path, args=None, dry_run=True) == 3
    out = capsys.readouterr().out
    assert str(os.getpid()) in out and started_text in out


def test_a_stale_lock_is_taken_with_a_log_line(tmp_path: Path, capsys):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead.pid
    dead.wait(timeout=10)
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": dead_pid, "started": "irrelevant"}), encoding="utf-8")
    with dispatcher_lock(lock_path):
        pass
    assert f"stale lock of pid {dead_pid} taken" in capsys.readouterr().err
