"""Issue 89 unit 1: a home push that a target refuses as `refused: ORCH live` or `refused: merge running` leaves a
pending mark (the home head sha and the time) in that target; `taken` and `failed` leave none.

Rows: (a) a live ORCH refuses: the mark holds the home's HEAD; (b) a busy merge lock refuses: the mark is written;
(c) a `taken` push: 0 marks; (d) a `failed:` push: 0 marks; (e) write/read/clear round trip, `read_mark` None on
malformed JSON; (f) a mark write that raises keeps the refused line; (g) a home head that cannot be read writes no mark
and keeps the refused line.

Fakes: the home is a real git repo (its HEAD is the mark's sha); a target is a folder whose
`artifacts/runs/orch_session.json` lists a session (a live ORCH); `run_core_update` is faked for (b) to (d)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tools.agentops import core_pending, core_targets
from tools.agentops.core_update import CoreUpdateBusy

REPO = "owner/t"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=True)
    return result.stdout.strip()


def _home(tmp_path: Path) -> Path:
    """A home checkout with one commit; its HEAD is what a refused push must mark."""
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q", "-b", "main")
    _git(home, "-c", "user.email=agentops-test@example.invalid", "-c", "user.name=agentops test",
         "commit", "-q", "--allow-empty", "-m", "home")
    return home


def _target(tmp_path: Path, *, live: bool = False) -> Path:
    folder = tmp_path / "target"
    folder.mkdir()
    if live:
        session = folder / "artifacts" / "runs" / "orch_session.json"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"sessions": [{"pid": 1}]}), encoding="utf-8")
    return folder


def _targets(folder: Path) -> list[dict]:
    return [{"folder": str(folder), "repo": REPO}]


def _fake_update(monkeypatch, behaviour) -> None:
    """Replace `run_core_update`: `behaviour` is an exception to raise, or a result to return."""
    def update(folder, gh_runner=None, lock_wait_s=None, home_count=None, monitor_runner=None):
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    monkeypatch.setattr(core_targets, "run_core_update", update)


# -- (a) a live ORCH refuses: the mark holds the home's HEAD ----------------------------------------------------------

def test_row_a_orch_live_refusal_writes_the_mark_with_the_home_head(tmp_path):
    home = _home(tmp_path)
    folder = _target(tmp_path, live=True)

    lines = core_targets.run_core_targets(_targets(folder), home_root=home)

    assert lines == [f"{REPO}: refused: ORCH live"], lines
    mark = core_pending.read_mark(folder)
    assert mark is not None and mark["sha"] == _git(home, "rev-parse", "HEAD"), mark
    assert (folder / core_pending.MARK_REL).is_file()


# -- (b) a busy merge lock refuses: the mark is written ---------------------------------------------------------------

def test_row_b_merge_running_refusal_writes_the_mark(tmp_path, monkeypatch):
    home = _home(tmp_path)
    folder = _target(tmp_path)
    _fake_update(monkeypatch, CoreUpdateBusy("merge lock held"))

    lines = core_targets.run_core_targets(_targets(folder), home_root=home)

    assert lines == [f"{REPO}: refused: merge running"], lines
    mark = core_pending.read_mark(folder)
    assert mark is not None and mark["sha"] == _git(home, "rev-parse", "HEAD"), mark


# -- (c) a taken push writes no mark ----------------------------------------------------------------------------------

def test_row_c_taken_push_writes_no_mark(tmp_path, monkeypatch):
    home = _home(tmp_path)
    folder = _target(tmp_path)
    _fake_update(monkeypatch, SimpleNamespace(home_head="c" * 40, waited=False))

    lines = core_targets.run_core_targets(_targets(folder), home_root=home)

    assert lines == [f"{REPO}: taken {'c' * 8}"], lines
    assert core_pending.read_mark(folder) is None
    assert not (folder / core_pending.MARK_REL).exists()


# -- (d) a failed push writes no mark ---------------------------------------------------------------------------------

def test_row_d_failed_push_writes_no_mark(tmp_path, monkeypatch):
    home = _home(tmp_path)
    folder = _target(tmp_path)
    _fake_update(monkeypatch, RuntimeError("boom"))

    lines = core_targets.run_core_targets(_targets(folder), home_root=home)

    assert len(lines) == 1 and lines[0].startswith(f"{REPO}: failed: boom"), lines
    assert core_pending.read_mark(folder) is None
    assert not (folder / core_pending.MARK_REL).exists()


# -- (e) the mark's own round trip ------------------------------------------------------------------------------------

def test_row_e_write_read_clear_round_trip(tmp_path):
    folder = tmp_path / "t"
    assert core_pending.read_mark(folder) is None

    clock = lambda: datetime(2026, 10, 16, 21, 30, 5)  # noqa: E731
    path = core_pending.write_mark(folder, "d" * 40, clock=clock)
    assert path == folder / core_pending.MARK_REL
    assert core_pending.read_mark(folder) == {"sha": "d" * 40, "at": "2026-10-16T21:30:05"}

    core_pending.write_mark(folder, "e" * 40, clock=clock)  # overwrites
    assert core_pending.read_mark(folder)["sha"] == "e" * 40

    core_pending.clear_mark(folder)
    assert core_pending.read_mark(folder) is None
    core_pending.clear_mark(folder)  # no error when absent


def test_row_e_malformed_mark_reads_as_none(tmp_path):
    folder = tmp_path / "t"
    path = folder / core_pending.MARK_REL
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert core_pending.read_mark(folder) is None


# -- (f) a mark write that raises never changes the line ---------------------------------------------------------------

def test_row_f_a_mark_write_failure_keeps_the_refused_line(tmp_path, monkeypatch):
    home = _home(tmp_path)
    folder = _target(tmp_path, live=True)

    def broken_write(folder, sha, *, clock=None):
        raise OSError("disk full")

    monkeypatch.setattr(core_pending, "write_mark", broken_write)

    lines = core_targets.run_core_targets(_targets(folder), home_root=home)

    assert lines == [f"{REPO}: refused: ORCH live"], lines


# -- (g) a home head that cannot be read writes no mark and keeps the refused line -----------------------------------

def test_row_g_unreadable_home_head_writes_no_mark(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    folder = _target(tmp_path, live=True)

    lines = core_targets.run_core_targets(_targets(folder), home_root=not_a_repo)

    assert lines == [f"{REPO}: refused: ORCH live"], lines
    assert core_pending.read_mark(folder) is None
