"""#782: a session that ended with no checkpoint after the machine booted later than the session
started was killed by the restart: no comment, no core issue, back in the pool. Fakes only."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude
from tests.test_dispatch_dead_sessions_restart import _held_gh
from tests.test_agentops_dispatch import _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401


def _run(tmp_path: Path, boot, started_before: float):
    now = time.time()
    entry = {"session": "aaaa1111", "session_uuid": None, "offered": [], "held": [7], "preexisting": [],
             "claimed_now": [7], "finishing": False, "started_at_wall": now - started_before}
    (tmp_path / "orch_session.json").write_text(
        json.dumps({"updated": "2026-10-11T00:00:00", "session": None, "sessions": [entry]}), encoding="utf-8")
    gh = _held_gh({"aaaa1111": [7]})
    gh.last[7] = "worked on it, no checkpoint"
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    cores: list[dict] = []
    d.core_issue_writer = lambda **kw: cores.append(kw)
    logs: list[str] = []
    d.log = logs.append
    d.boot_time = boot
    d.tick()
    return d, gh, claude, cores, logs


def test_row1_started_before_boot_is_a_machine_restart(tmp_path: Path):
    d, gh, claude, cores, logs = _run(tmp_path, lambda: time.time() - 600, started_before=3600)
    assert cores == []
    assert gh.comments == []
    assert 7 in gh.removed_assignees
    assert [offered for offered, _ in claude.started] == [[7]]
    assert len([m for m in logs if "ended (machine restart at " in m]) == 1


def test_row2_started_after_boot_is_a_no_checkpoint_defect(tmp_path: Path):
    d, gh, claude, cores, logs = _run(tmp_path, lambda: time.time() - 7200, started_before=3600)
    assert len(cores) == 1 and cores[0]["kind"] == "broke"
    assert [n for n, _ in gh.comments] == [7]
    assert not [m for m in logs if "machine restart" in m]


def test_row3_unreadable_boot_time_behaves_as_row2(tmp_path: Path):
    def boom():
        raise OSError("no boot time")

    for boot in (lambda: None, boom):
        sub = tmp_path / ("none" if boot() is None else "raise") if boot is not boom else tmp_path / "raise"
        sub.mkdir()
        d, gh, claude, cores, logs = _run(sub, boot, started_before=3600)
        assert len(cores) == 1 and cores[0]["kind"] == "broke"
        assert [n for n, _ in gh.comments] == [7]
