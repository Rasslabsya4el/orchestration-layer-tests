"""#156 rule 1: a merge process loads every repo module its path after the lock wait needs BEFORE it waits.

Case: a batch waiter is queued behind a live holder; while it waits, ``core_update.py`` and ``core_targets.py`` on disk
are replaced by versions where ``core_targets`` imports a name only the new ``core_update`` has (what #148 did); then the
holder posts the waiter's landed result. The waiter must return it (rc 0, ``merged: ... (batch of N with ...)``), not
die on an ImportError at its first import after the wait. Seams: ``merge.run_merge_command`` (rows 1) and
``detach.run_and_record`` (row 3), both with the real lock protocol in a throw-away copy of the package.

Also: the ``after-landing step failed:`` line stays ONE line for a multi-line exception message."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import asdict
from pathlib import Path

from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401
from tests.test_issue156_after_landing import PREFIX, _args, _fake_world, _landed
from tools.agentops import detach, merge as merge_module, target_check
from tools.agentops.merge import MergeResult
from tools.agentops.util import now

REPO = Path(__file__).resolve().parents[1]
NEW_NAME = "NAME_ONLY_THE_NEW_CORE_UPDATE_HAS"

_WAITER = textwrap.dedent("""
    import argparse, sys
    sys.path.insert(0, {pkg!r})  # run_and_record starts the child in the repo root: the copy must come first
    from pathlib import Path
    import tools.agentops.core_update  # a CLI entry has loaded it before any wait
    from tools.agentops import merge
    ns = argparse.Namespace(branch="b", cwd=Path({home!r}), suite={suite!r}, lock=Path({lock!r}), timeout=120,
                            queue_checked=True, issue=[])
    sys.exit(merge.run_merge_command(ns))
""")


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


class _World:
    """A copy of the package (so its files can change on disk), a home repo with origin, a lock held by this test."""

    def __init__(self, tmp: Path):
        self.pkg = tmp / "pkg"
        shutil.copytree(REPO / "tools" / "agentops", self.pkg / "tools" / "agentops",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "scripts", self.pkg / "scripts", ignore=shutil.ignore_patterns("__pycache__"))  # #163: core_table's token_meter
        (self.pkg / "config").mkdir()
        shutil.copy(REPO / "config" / "orchestration.json", self.pkg / "config" / "orchestration.json")  # #163: dispatch reads the settings at import
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(tmp / "origin.git"))
        self.home = tmp / "home"
        _git(tmp, "clone", "-q", str(tmp / "origin.git"), str(self.home))
        (self.home / "f.txt").write_text("x", encoding="utf-8")
        _git(self.home, "add", "-A")
        _git(self.home, "commit", "-q", "-m", "c")
        _git(self.home, "push", "-q", "origin", "main")
        self.head = _git(self.home, "rev-parse", "origin/main")
        # row 1b: a home (update_rule home) with one target, and a branch one commit ahead of main
        self.target = tmp / "target"
        self.target.mkdir()
        (self.home / "config").mkdir()
        (self.home / "config" / "orchestration.json").write_text(
            json.dumps({"update_rule": "home", "targets": [{"repo": "acme/t", "folder": str(self.target)}]}),
            encoding="utf-8")
        _git(self.home, "add", "-A")
        _git(self.home, "commit", "-q", "-m", "home config")
        _git(self.home, "push", "-q", "origin", "main")
        self.head = _git(self.home, "rev-parse", "origin/main")
        _git(self.home, "checkout", "-q", "-b", "b")
        (self.home / "g.txt").write_text("y", encoding="utf-8")
        _git(self.home, "add", "-A")
        _git(self.home, "commit", "-q", "-m", "branch work")
        _git(self.home, "checkout", "-q", "main")
        self.lock = tmp / "merge.lock"
        self.lock.write_text(json.dumps({"pid": os.getpid(), "started": now(), "token": "leader"}), encoding="utf-8")
        self.script = _WAITER.format(pkg=str(self.pkg), home=str(self.home), lock=str(self.lock),
                                    suite=f'"{sys.executable}" --version')
        self.env = {**os.environ, "PYTHONPATH": str(self.pkg)}

    def argv(self) -> list[str]:
        return [sys.executable, "-c", self.script, "--cwd", str(self.home)]

    def wait_until_queued(self, deadline_s: float = 120.0) -> Path:
        """The waiter's ticket is in the line and its wait loop runs: what it loads before the wait is loaded by now."""
        queue = self.lock.with_name(self.lock.name + ".queue")
        end = time.time() + deadline_s
        while time.time() < end:
            tickets = list(queue.iterdir()) if queue.exists() else []
            if tickets:
                time.sleep(1.0)
                return tickets[0]
            time.sleep(0.05)
        raise AssertionError("the waiter never queued")

    def replace_core_modules(self) -> None:
        """``core_update.py`` and ``core_targets.py`` on disk change: ``core_targets`` imports a name only the new
        ``core_update`` has (what #148 did)."""
        agentops = self.pkg / "tools" / "agentops"
        with open(agentops / "core_update.py", "a", encoding="utf-8") as fh:
            fh.write(f"\n{NEW_NAME} = 1\n")
        targets = agentops / "core_targets.py"
        text = targets.read_text(encoding="utf-8")
        targets.write_text(text.replace("from .core_update import ", f"from .core_update import {NEW_NAME}, ", 1),
                           encoding="utf-8")

    def release_the_waiter_with_changed_code(self, deadline_s: float = 120.0) -> None:
        """Row 1: the files change, then the leader posts the waiter's landed result."""
        ticket = self.wait_until_queued(deadline_s)
        self.replace_core_modules()
        posted = MergeResult(ok=True, reason="pushed", message="b -> main (batch of 2 with a)", pushed=True,
                             head_before=self.head, head_after=self.head, batched=True)
        batch = self.lock.with_name(self.lock.name + ".batch")
        batch.mkdir(parents=True, exist_ok=True)
        tmp = batch / "tmp-leader"
        tmp.write_text(json.dumps(asdict(posted)), encoding="utf-8")
        os.replace(tmp, batch / f"{ticket.name}.result")

    def release_the_lock_with_changed_code(self, deadline_s: float = 120.0) -> None:
        """Row 1b: the files change, then the holder lets go with no result for the waiter, which takes the lock."""
        self.wait_until_queued(deadline_s)
        self.replace_core_modules()
        self.lock.unlink()


def test_row1_a_waiter_whose_core_modules_changed_on_disk_during_the_wait_returns_landed(tmp_path: Path):
    world = _World(tmp_path)
    proc = subprocess.Popen(world.argv(), cwd=world.pkg, env=world.env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        world.release_the_waiter_with_changed_code()
        out, _ = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "Traceback" not in out and PREFIX not in out, out  # nothing failed after the wait, not even a caught step
    assert proc.returncode == 0, out
    assert "merged: b -> main (batch of 2 with a)" in out, out


def test_row1b_a_waiter_that_takes_the_lock_after_the_swap_runs_its_own_attempt_without_an_import_error(tmp_path: Path):
    world = _World(tmp_path)
    proc = subprocess.Popen(world.argv(), cwd=world.pkg, env=world.env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        world.release_the_lock_with_changed_code()
        out, _ = proc.communicate(timeout=100)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "ImportError" not in out and "Traceback" not in out, out
    assert "merged: b -> main" in out, out  # its own suite and target check ran and let it land
    assert proc.returncode == 0, out


def test_row3_the_detached_result_of_that_landed_waiter_keeps_pushed_sha(tmp_path: Path):
    world = _World(tmp_path)
    errors: list[BaseException] = []

    def release() -> None:
        try:
            world.release_the_waiter_with_changed_code()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the assert below
            errors.append(exc)

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    result_file = tmp_path / "results" / "r.json"
    rc = detach.run_and_record(world.argv(), result_file, "merge", "b", merge_log=tmp_path / "merge.log")
    thread.join(timeout=30)

    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert not errors, errors
    assert rc == 0, record["output"]
    assert record["pushed_sha"] == world.head
    assert record["reason"] != "failed"
    assert "Traceback" not in record["output"] and PREFIX not in record["output"], record["output"]


def test_the_after_landing_line_is_one_line_for_a_multi_line_exception(monkeypatch, capsys):
    _fake_world(monkeypatch, _landed())

    def boom(_notes):
        raise ImportError("cannot import name 'X'\n  from 'y'\n\n(unknown location)")
    monkeypatch.setattr(target_check, "note_lines", boom)

    assert merge_module.run_merge_command(_args()) == 0

    lines = [ln for ln in capsys.readouterr().out.splitlines() if PREFIX in ln]
    assert lines == [f"{PREFIX} ImportError: cannot import name 'X' from 'y' (unknown location)"]
