"""#779 row 4: in machine mode play the dispatcher starts and wakes nothing; the first tick after
normal starts one; the keeper pass runs once per tick with the tick's mode. Fakes only: the mode file is
under tmp_path and keeper_pass is a recorder (no real process is ever suspended)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher, _issue
from tools.agentops import hold, machine_mode
from tests._area_rule import _no_area_rule  # noqa: F401


@pytest.fixture
def rig(tmp_path: Path, monkeypatch):
    mdir = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(mdir))
    calls: list[str] = []
    monkeypatch.setattr(hold, "keeper_pass", lambda mode, root=None, **kw: calls.append(mode) or {})
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1), _issue(2)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.log = lambda m: None
    return d, claude, calls


def test_row4_play_starts_and_wakes_nothing_then_normal_starts_one(rig):
    d, claude, calls = rig
    machine_mode.set_mode("play")
    for _ in range(20):
        d.tick()
    assert claude.started == [] and claude.waked == []
    assert calls == ["play"] * 20
    machine_mode.set_mode("normal")
    d.tick()
    assert len(claude.started) == 1
    assert calls[-1] == "normal" and len(calls) == 21


def test_unreadable_mode_starts_nothing_and_does_not_crash(rig):
    d, claude, calls = rig
    (machine_mode.machine_dir()).mkdir(parents=True)
    (machine_mode.machine_dir() / machine_mode.MODE_FILE).write_text("{not json", encoding="utf-8")
    for _ in range(3):
        d.tick()
    assert claude.started == [] and calls == []


def test_keeper_pass_error_is_logged_not_raised(rig, monkeypatch):
    d, claude, calls = rig
    logs: list[str] = []
    d.log = logs.append

    def boom(mode, root=None, **kw):
        raise OSError("no psutil")

    monkeypatch.setattr(hold, "keeper_pass", boom)
    d.tick()
    assert any("keeper pass failed" in m for m in logs)
    assert len(claude.started) == 1
