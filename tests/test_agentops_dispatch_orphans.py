"""#217/#225: `Dispatcher._sweep_orphans` -- only called when `orphan_sweep` is injected (every
other test's default construction, and `Dispatcher()` with no `orphan_sweep` given, must never touch
a real process or pay a real PowerShell/`ps` query); a sweep that raises must never break `_finish` /
`_timeout`'s own bookkeeping; and (#225) it is called with the ended session's own `claude_pid` and
every OTHER still-live session's, never a missing-parent guess."""

from __future__ import annotations

from pathlib import Path

import tools.agentops.orphans as orphans_module
from tools.agentops.dispatch import Dispatcher, _Current

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh


def _dispatcher(tmp_path: Path, *, claude: FakeClaude, gh: FakeGh, clock: FakeClock,
                orphan_sweep=None) -> Dispatcher:
    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=clock, orphan_sweep=orphan_sweep)


def _live_current(claude: FakeClaude, clock: FakeClock) -> _Current:
    session_id, _ = claude.start([])
    return _Current(session=session_id, started_at=clock())


def test_orphan_sweep_called_once_per_finish_with_the_sessions_wall_clock_start(tmp_path: Path,
                                                                                monkeypatch):
    calls: list[tuple] = []
    clock = FakeClock(start=100.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock,
                    orphan_sweep=lambda started_at, **kw: calls.append((started_at, kw)))

    monkeypatch.setattr("tools.agentops.dispatch.time.time", lambda: 1_000_000.0)
    cur = _live_current(claude, clock)
    clock.advance(30.0)  # the session has been "alive" 30 monotonic seconds when it ends

    d._finish(cur, "test")

    assert len(calls) == 1
    assert calls[0][0] == 1_000_000.0 - 30.0  # time.time() - (clock() - started_at)


def test_orphan_sweep_called_once_per_timeout(tmp_path: Path, monkeypatch):
    calls: list[tuple] = []
    clock = FakeClock(start=0.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock,
                    orphan_sweep=lambda started_at, **kw: calls.append((started_at, kw)))

    monkeypatch.setattr("tools.agentops.dispatch.time.time", lambda: 500.0)
    cur = _live_current(claude, clock)

    d._timeout(cur)

    assert len(calls) == 1


def test_orphan_sweep_gets_the_ended_sessions_pid_and_every_other_live_sessions(tmp_path: Path,
                                                                                monkeypatch):
    """#225: `_sweep_orphans` passes `cur`'s own `claude_pid` as `ended_claude_pid`, and every OTHER
    still-live session's `claude_pid` (never `cur`'s own) as `live_claude_pids`."""

    calls: list[dict] = []
    clock = FakeClock(start=0.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock,
                    orphan_sweep=lambda started_at, **kw: calls.append(kw))
    monkeypatch.setattr("tools.agentops.dispatch.time.time", lambda: 1_000.0)

    cur = _live_current(claude, clock)
    cur.claude_pid = 111
    other = _live_current(claude, clock)
    other.claude_pid = 222
    d.sessions.extend([cur, other])

    d._finish(cur, "test")

    assert len(calls) == 1
    assert calls[0]["ended_claude_pid"] == 111
    assert calls[0]["live_claude_pids"] == [222]


def test_default_construction_never_calls_real_process_table(tmp_path: Path, monkeypatch):
    """No `orphan_sweep` given (every existing dispatch test's construction) -> `_finish` /
    `_timeout` never reach `real_process_table` at all."""

    def _boom():
        raise AssertionError("real_process_table must never be called by a test")

    monkeypatch.setattr(orphans_module, "real_process_table", _boom)
    clock = FakeClock()
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)  # orphan_sweep defaults to None

    cur1 = _live_current(claude, clock)
    d._finish(cur1, "test")
    cur2 = _live_current(claude, clock)
    d._timeout(cur2)  # neither call raises: real_process_table is never reached


def test_sweep_orphans_gets_the_sessions_recorded_owned_pids(tmp_path: Path, monkeypatch):
    """#225's own follow-up (the OPEN RISK): `_sweep_orphans` passes `cur.owned_pids` on as
    `recorded_owned_pids` -- the tie left once ending the session has killed the bridge rows a fresh
    chain walk would need."""

    calls: list[dict] = []
    clock = FakeClock(start=0.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock,
                    orphan_sweep=lambda started_at, **kw: calls.append(kw))
    monkeypatch.setattr("tools.agentops.dispatch.time.time", lambda: 1_000.0)

    cur = _live_current(claude, clock)
    cur.claude_pid = 111
    cur.owned_pids = frozenset({222, 333})
    d.sessions.append(cur)

    d._finish(cur, "test")

    assert len(calls) == 1
    assert calls[0]["recorded_owned_pids"] == frozenset({222, 333})


def test_maybe_update_owned_pids_folds_the_snapshot_into_each_live_session(tmp_path: Path):
    """#225's own follow-up: on an eligible tick, every live session's `claude_pid` is snapshotted at
    once and the result is UNIONED into `owned_pids` (never shrunk across ticks)."""

    calls: list[list[int]] = []

    def snapshot(claude_pids):
        calls.append(list(claude_pids))
        return {111: frozenset({222}), 999: frozenset({888})}

    clock = FakeClock(start=0.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.owned_pids_snapshot = snapshot

    cur = _live_current(claude, clock)
    cur.claude_pid = 111
    cur.owned_pids = frozenset({111000})  # a pid recorded on an earlier tick -- must survive the union
    other = _live_current(claude, clock)
    other.claude_pid = 999
    d.sessions.extend([cur, other])

    d._maybe_update_owned_pids()

    assert calls == [[111, 999]]
    assert cur.owned_pids == frozenset({111000, 222})
    assert other.owned_pids == frozenset({888})

    # a second call inside the throttle window pays nothing more
    d._maybe_update_owned_pids()
    assert calls == [[111, 999]]


def test_maybe_update_owned_pids_is_throttled_and_skips_sessions_with_no_claude_pid_yet(
    tmp_path: Path):
    calls: list[list[int]] = []
    clock = FakeClock(start=0.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.owned_pids_snapshot = lambda pids: calls.append(list(pids)) or {}

    cur = _live_current(claude, clock)  # claude_pid still None -- nothing to snapshot yet
    d.sessions.append(cur)

    d._maybe_update_owned_pids()
    assert calls == []  # no live claude_pid at all: never called

    cur.claude_pid = 111
    d._maybe_update_owned_pids()
    assert calls == [[111]]

    clock.advance(1.0)  # inside the throttle window
    d._maybe_update_owned_pids()
    assert calls == [[111]]


def test_default_construction_never_calls_snapshot_owned_pids(tmp_path: Path, monkeypatch):
    """No `owned_pids_snapshot` given (every other test's construction) -> `tick`'s own call never
    reaches `snapshot_owned_pids` on the real machine."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("snapshot_owned_pids must never be called by a test")

    monkeypatch.setattr(orphans_module, "snapshot_owned_pids", _boom)
    clock = FakeClock()
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)  # owned_pids_snapshot defaults to None

    cur = _live_current(claude, clock)
    cur.claude_pid = 111
    d.sessions.append(cur)
    d._maybe_update_owned_pids()  # does not raise: snapshot_owned_pids is never reached


def test_a_raising_sweep_is_logged_and_outcomes_still_resolved(tmp_path: Path):
    logged: list[str] = []
    clock = FakeClock()
    claude, gh = FakeClaude(), FakeGh()

    def _raising_sweep(started_at: float, **_kwargs):
        raise TimeoutError("boom")

    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock, orphan_sweep=_raising_sweep)
    d.log = logged.append

    cur = _live_current(claude, clock)
    gh.claim(1)
    cur.held = {1}
    gh.close(1)

    outcomes = d._finish(cur, "test")

    assert outcomes == {1: "closed"}
    assert any("orphan sweep" in line and cur.session in line for line in logged)
