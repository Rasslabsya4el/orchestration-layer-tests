"""#611: claude.exe is looked up at each use, never held; a failed lookup is a failed tick (one line
naming what was searched), and ticks failing for 10 minutes in a row are reported once."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClock, FakeGh, _dispatcher, _issue
from tools.agentops import dispatch as dp
from tools.agentops.dispatch import ClaudeCli


def _make_exe(local: Path, version: str) -> Path:
    exe = (local / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
           / version / "e1f0154146bb" / "claude.exe")
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    return exe


def _env(monkeypatch, local: Path) -> None:
    monkeypatch.delenv("CLAUDE_EXE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(local / "no-roaming"))
    monkeypatch.setattr("shutil.which", lambda name: None)


def _fake_run(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if not Path(cmd[0]).is_file():
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout="backgrounded · abcd1234", stderr="")

    monkeypatch.setattr(dp.subprocess, "run", run)
    return calls


def test_second_start_uses_the_new_version_folder(tmp_path, monkeypatch):
    local = tmp_path / "local"
    old = _make_exe(local, "2.1.286")
    _env(monkeypatch, local)
    calls = _fake_run(monkeypatch)
    cli = ClaudeCli()
    cli.start([1])
    assert calls[0][0] == str(old)
    # the desktop app update: the old version folder goes, a new one appears
    import shutil
    shutil.rmtree(old.parent.parent)
    new = _make_exe(local, "2.1.289")
    cli.start([1])
    cli.list_sessions()
    cli.stop("abcd1234")
    assert [c[0] for c in calls[1:]] == [str(new)] * 3  # 0 FileNotFoundError


def test_fixed_exe_is_kept(tmp_path):
    assert ClaudeCli(tmp_path / "claude.exe").exe == tmp_path / "claude.exe"


def test_missing_exe_is_a_failed_tick_naming_the_searched_paths(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path / "local")
    calls = _fake_run(monkeypatch)
    gh = FakeGh(candidates=[_issue(5)])
    d = _dispatcher(tmp_path, claude=ClaudeCli(), gh=gh)
    lines: list[str] = []
    d.log = lines.append
    d.run(ticks=2, sleep=lambda s: None)  # no crash
    failed = [ln for ln in lines if ln.startswith("tick failed")]
    assert len(failed) == 2  # one line per tick
    assert "searched" in failed[0] and "claude-code" in failed[0] and "CLAUDE_EXE" in failed[0]
    assert calls == []  # nothing was launched


def test_failed_ticks_for_ten_minutes_are_reported_once(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path / "local")
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=ClaudeCli(), gh=FakeGh(candidates=[_issue(5)]), clock=clock)
    d.log = lambda m: None
    written: list[dict] = []
    d.core_issue_writer = lambda **kw: written.append(kw)

    def step(seconds):
        clock.advance(seconds)
        d.run(ticks=1, sleep=lambda s: None)

    step(0)
    step(599)
    assert written == []
    step(1)
    assert len(written) == 1 and "searched" in written[0]["error"]
    step(300)
    step(300)
    assert len(written) == 1  # not repeated
    # a success resets the run
    monkeypatch.setattr(d, "tick", lambda: None)
    step(1)
    monkeypatch.undo()
    _env(monkeypatch, tmp_path / "local")
    step(0)
    step(600)
    assert len(written) == 2
