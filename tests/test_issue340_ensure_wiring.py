"""#340 unit 3: the monitor server is kept up by `dispatch --ensure` (`run_ensure`'s `server_ensure` step), and the
logon-task verb `monitor-boot` is gone (tools/agentops/monitor_boot.py deleted: one mechanism, not two).

Seams under test (the spec's own): `run_ensure(...)` with an injected `server_ensure` and injected `launch` / `popen`
(nothing real starts); `cli.main([...])` for the verb; the file system for `monitor_boot.py`.

    python -m pytest -q -p no:faulthandler tests/test_issue340_ensure_wiring.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import cli, monitor_ensure
from tools.agentops.dispatch import run_ensure

ROOT = Path(__file__).resolve().parents[1]


def _launch(*, out_path):
    """A fake dispatcher start: returns a pid, writes nothing."""
    return 555


def _failing_launch(*, out_path):
    raise OSError("start refused (fake)")


def _no_popen(*args, **kwargs):
    raise AssertionError("a real collector start was attempted")


def _run(tmp_path, capsys, *, launch=_launch, **kwargs):
    rc = run_ensure(lock_path=tmp_path / "dispatcher.lock", out_path=tmp_path / "dispatch.out",
                    launch=launch, root=tmp_path, popen=_no_popen, env={}, **kwargs)
    captured = capsys.readouterr()
    return rc, captured.out.splitlines(), captured.err


def test_started_server_prints_its_line_and_the_dispatcher_code(tmp_path, capsys):
    rc, out, _err = _run(tmp_path, capsys, server_ensure=lambda: "started")
    assert rc == 0
    assert "dispatch --ensure: monitor server started" in out


def test_absent_server_prints_no_monitor_line(tmp_path, capsys):
    rc, out, _err = _run(tmp_path, capsys, server_ensure=lambda: "absent")
    assert rc == 0
    assert not any("monitor server" in line for line in out)


def test_live_server_prints_its_line(tmp_path, capsys):
    rc, out, _err = _run(tmp_path, capsys, server_ensure=lambda: "live")
    assert rc == 0
    assert "dispatch --ensure: monitor server live" in out


def test_raising_server_check_is_one_stderr_line_and_keeps_the_code(tmp_path, capsys):
    rc_ok, _out, _err = _run(tmp_path, capsys, server_ensure=lambda: "started")

    def boom():
        raise RuntimeError("boom")

    rc, out, err = _run(tmp_path, capsys, server_ensure=boom)
    assert rc == rc_ok == 0
    assert not any("monitor server" in line for line in out)
    lines = [line for line in err.splitlines() if "monitor server check failed" in line]
    assert lines == ["dispatch --ensure: monitor server check failed: boom"]


def test_raising_server_check_keeps_a_failed_dispatcher_code(tmp_path, capsys):
    def boom():
        raise RuntimeError("boom")

    rc, _out, err = _run(tmp_path, capsys, launch=_failing_launch, server_ensure=boom)
    assert rc == 1, "the dispatcher's own failure code stays whatever the server check did"
    assert "monitor server check failed: boom" in err


def test_injected_launch_without_server_ensure_calls_no_real_server(tmp_path, capsys, monkeypatch):
    calls: list[object] = []

    def must_not_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("ensure_server reached from a test with an injected launch")

    monkeypatch.setattr(monitor_ensure, "ensure_server", must_not_run)
    rc, out, err = _run(tmp_path, capsys)
    assert rc == 0
    assert calls == []
    assert not any("monitor server" in line for line in out)
    assert "monitor server check failed" not in err


def test_monitor_boot_verb_no_longer_parses(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["monitor-boot", "status"])
    assert exc.value.code == 2
    assert "monitor-boot" in capsys.readouterr().err


def test_monitor_boot_verb_is_not_in_the_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "monitor-boot" not in capsys.readouterr().out


def test_monitor_boot_module_is_gone():
    assert not (ROOT / "tools" / "agentops" / "monitor_boot.py").exists()
