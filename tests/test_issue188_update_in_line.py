"""#188 rule 5 / acceptance row 7: a pending core update takes the target's merge lock as one more waiter in the
merge line. It joins behind the merge that holds the lock, waits for its turn (not 10 s per attempt, withdrawing its
ticket each time) and merges queued after it wait behind it. Only the pending-mark retry does this: the update before
an ORCH start stays the bounded attempt, so a start never queues behind the whole line.

Driven through `Dispatcher.tick` with the production callables `apply_auto_core_update` and
`apply_auto_core_update_in_line` handed to the constructor's `core_update` and `pending_core_update`, on a real tmp
target (bare origin + clone), the home a fake `gh`, the merges real helper processes holding the real merge lock.
(`run_dispatch` passes the same two names; its wiring is one line and is not run here.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.agentops.dispatch as dispatch
from tools.agentops import core_update as core_update_mod
from tools.agentops.core_pending import read_mark, write_mark
from tools.agentops.core_update import ORCH_STATE_ENTRY
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue
from tests.test_issue49_core_update import (NEW_VERSION, ORCH_JSON, REPO, WAIT_S, _fake_gh, _stop, _target,
                                            _version)

FIRST_MERGE_S = 12.0  # longer than the old 10 s wait: the case that was refused every ~2 min in Hermes 18.10

MERGE = """
import json, sys, time
from pathlib import Path
from tools.agentops.merge import merge_then_checkout_locked
lock, config, seconds = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
print("started", flush=True)
with merge_then_checkout_locked(lock):
    print("held " + json.loads(config.read_text(encoding="utf-8"))["core_version"], flush=True)
    time.sleep(seconds)
"""


def _lock_file(root: Path) -> Path:
    return root / "var" / "locks" / "merge.lock"


def _start_merge(root: Path, seconds: float) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.Popen([sys.executable, "-c", MERGE, str(_lock_file(root)), str(root / ORCH_JSON),
                             str(seconds)], cwd=str(REPO), env=env, stdout=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _readline(proc: subprocess.Popen) -> str:
    """One line of a helper's output; a helper that says nothing in `WAIT_S` fails the test instead of hanging it."""
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(WAIT_S)
    if not line:
        pytest.fail(f"the helper printed nothing in {WAIT_S:.0f} s")
    return line[0].strip()


def _queued(root: Path) -> int:
    queue = root / "var" / "locks" / "merge.lock.queue"
    return len(list(queue.iterdir())) if queue.is_dir() else 0


def _until(what: str, check, deadline_s: float = WAIT_S) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.1)
    pytest.fail(f"{what}: not reached in {deadline_s:.0f} s")


class Rig:
    """A Dispatcher on a real tmp target with fake sessions and a fake home. `pending_calls` counts how often the
    pending retry's callable ran."""

    def __init__(self, tmp_path: Path, monkeypatch, core_update, pending_core_update=None) -> None:
        monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
        real = dispatch._load_settings
        monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "update_rule": "auto"})
        monkeypatch.setattr(core_update_mod, "_subprocess_step_runner", lambda _root: [])  # no post-copy steps
        self.root = _target(tmp_path)
        other = tmp_path / "dispatcher"
        other.mkdir()
        self.claude = FakeClaude()
        self.gh = FakeGh(candidates=[_issue(1)])
        self.clock = FakeClock()
        wall0 = datetime.now(timezone.utc).timestamp()
        self.pending_calls = 0
        self.gh_runner = _fake_gh({"docs/agents/one.md": "one v2\n"})

        def counted_pending():
            self.pending_calls += 1
            return pending_core_update()

        self.d = Dispatcher(claude=self.claude, gh=self.gh, max_hours=40.0, events_file=other / "events.jsonl",
                            state_file=self.root / ORCH_STATE_ENTRY, projects_dir=other / "projects",
                            finishing_dir=other / "finishing", context_line=1_000_000, clock=self.clock,
                            timestamp=lambda: datetime.fromtimestamp(wall0 + self.clock.value, timezone.utc),
                            core_update=core_update,
                            pending_core_update=counted_pending if pending_core_update is not None else None,
                            merge_lock_path=_lock_file(self.root), core_update_wait_s=0.2, core_root=self.root,
                            orch_offer_max=99)
        self.d.admission = lambda previous_wait: SimpleNamespace(go=True, reason="", line=lambda: "go")
        self.d.log = lambda _line: None

    def first_start_then_issue_2(self) -> None:
        """The ordinary first start; then every live session is finishing and issue 2 turns ready."""
        _until("the first start", lambda: (self.tick_once(), len(self.claude.started) == 1)[1])
        self.d.sessions[0].finishing = True
        self.gh._candidates = [_issue(2)]

    def tick_once(self) -> None:
        self.clock.advance(30.0)
        self.d.tick()


def test_row7_update_joins_the_line_and_lands_right_after_the_merge_it_queued_behind(tmp_path, monkeypatch):
    rig: Rig

    def pending():
        return dispatch.apply_auto_core_update_in_line(rig.root, settings={"update_rule": "auto"},
                                                       gh_runner=rig.gh_runner)

    # the before-start update has nothing to take at the first start
    rig = Rig(tmp_path, monkeypatch, lambda: "core update (auto): up to date", pending)
    root = rig.root
    rig.first_start_then_issue_2()
    write_mark(root, NEW_VERSION)

    first = _start_merge(root, FIRST_MERGE_S)
    second = None
    try:
        assert _readline(first) == "started"
        _until("the first merge holds the lock", _lock_file(root).exists)
        time.sleep(0.5)

        rig.tick_once()  # the update joins the line behind the first merge
        _until("the update's ticket stands in the line", lambda: _queued(root) == 1)
        second = _start_merge(root, 0.5)  # the next merge, queued after the update
        assert _readline(second) == "started"
        _until("both the update and the second merge wait", lambda: _queued(root) == 2)

        end = time.monotonic() + WAIT_S + FIRST_MERGE_S
        while read_mark(root) is not None and time.monotonic() < end:
            rig.tick_once()  # the same job keeps its place across ticks: no new ticket per tick
            assert _queued(root) <= 2
        assert read_mark(root) is None, "the update was not taken"
        assert _version(root) == NEW_VERSION
        assert rig.pending_calls == 1  # one job, one attempt, across all those ticks

        assert _readline(second) == f"held {NEW_VERSION}"  # the second merge got the lock only after the update
    finally:
        for proc in (first, second):
            if proc is not None:
                _stop(proc)

    out = root / "artifacts" / "runs" / "dispatch.out"
    text = out.read_text(encoding="utf-8") if out.is_file() else ""
    assert "merge running" not in text  # 0 refusals after it joined the line
    events = (root / "artifacts" / "runs" / "events.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in events.splitlines() if line.strip()]
    assert all("merge running" not in str(e.get("message")) for e in lines if e.get("kind") == "core_update_auto")


def test_before_start_update_stays_bounded_a_busy_line_does_not_hold_the_start(tmp_path, monkeypatch):
    """No mark stands: the update before an ORCH start gives up after its bounded wait (today's behaviour) and the
    start goes on; it leaves no ticket in the line and the pending retry never runs."""
    rig: Rig

    def ordinary():
        return dispatch.apply_auto_core_update(rig.root, settings={"update_rule": "auto"}, gh_runner=rig.gh_runner,
                                               lock_wait_s=0.5)

    def no_pending():
        pytest.fail("the pending retry ran with no mark standing")

    rig = Rig(tmp_path, monkeypatch, ordinary, no_pending)
    monkeypatch.setattr(core_update_mod, "blocking_orch_live", lambda _root: False)  # reach the lock, not the live check
    rig.first_start_then_issue_2()  # the first start's own update takes the core: the target is at the home's head
    assert read_mark(rig.root) is None

    holder = _start_merge(rig.root, 6.0)
    try:
        assert _readline(holder) == "started"
        _until("the merge holds the lock", _lock_file(rig.root).exists)
        time.sleep(0.5)

        end = time.monotonic() + WAIT_S
        while len(rig.claude.started) < 2 and time.monotonic() < end:
            rig.tick_once()
        assert len(rig.claude.started) == 2  # the start went on although the line was busy
        assert _queued(rig.root) == 0  # the bounded attempt withdrew its ticket
        assert rig.pending_calls == 0
    finally:
        _stop(holder)
    out = (rig.root / "artifacts" / "runs" / "dispatch.out").read_text(encoding="utf-8")
    assert "refused: merge running" in out  # the busy line refused the bounded attempt; the start did not wait


def test_a_holder_past_the_age_ceiling_is_a_busy_line_not_a_failure(tmp_path, monkeypatch):
    """The wait ends at the lock's age ceiling (`MergeLockTimeout`): the update reads it as `merge running`, so the
    dispatcher keeps the core hold and joins the line again on the next tick instead of releasing the start."""
    from tools.agentops.merge import MergeLockTimeout

    def stuck(*_a, **_k):
        raise MergeLockTimeout("the merge lock is held by pid 1, held for 31 min", pid=1, held=1860.0, silent=None)

    monkeypatch.setattr(core_update_mod, "merge_then_checkout_locked", stuck)
    with pytest.raises(core_update_mod.CoreUpdateBusy, match="merge running"):
        with core_update_mod._target_lock(tmp_path, None):
            pass
