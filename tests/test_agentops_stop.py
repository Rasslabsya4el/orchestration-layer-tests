"""#250: `tools.agentops.stop` on a fake process table -- no real process is ever touched."""

from __future__ import annotations

from tools.agentops.orphans import ProcRow
from tools.agentops.stop import exit_code, live_claude_pids, stop_processes


def _row(pid: int, ppid: int, create_time: float, command_line: str, *,
        msys_pid: int | None = None, msys_ppid: int | None = None,
        msys_pgid: int | None = None) -> ProcRow:
    return ProcRow(pid=pid, ppid=ppid, create_time=create_time, command_line=command_line,
                  msys_pid=msys_pid, msys_ppid=msys_ppid, msys_pgid=msys_pgid)


def test_live_claude_pids_matches_only_the_claude_exe_rows():
    processes = [
        _row(1000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent brain'),
        _row(1001, ppid=1000, create_time=0.0, command_line="bash.exe -c foo"),
        _row(1002, ppid=1, create_time=0.0, command_line=r"C:\tools\clauderelated.exe --version"),
    ]
    assert live_claude_pids(processes) == [1000]


# -- acceptance row 1: 3 min old, chain reaches a live session -> refused, killer never called -----

def test_row1_process_reaching_a_live_session_is_refused():
    now = 2000.0
    processes = [
        _row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        # 3 min old (180 s), child of the live claude.exe
        _row(9100, ppid=9000, create_time=now - 180.0, command_line="python worker.py"),
    ]
    killed: list[int] = []
    logged: list[str] = []
    outcomes = stop_processes([9100], table=lambda: processes,
                              killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, log=logged.append)
    assert killed == []
    assert len(outcomes) == 1
    assert outcomes[0].action == "refused"
    assert outcomes[0].reaches_claude == 9000
    assert exit_code(outcomes) != 0
    assert any("refused" in line for line in logged)


# -- acceptance row 2: 2 s old, child of a live session's shell, while True: pass -> refused --------

def test_row2_two_second_old_child_of_live_sessions_shell_is_refused():
    now = 2000.0
    processes = [
        _row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(9050, ppid=9000, create_time=0.0, command_line="bash.exe"),
        _row(9200, ppid=9050, create_time=now - 2.0,
             command_line='python -c "while True: pass"'),
    ]
    killed: list[int] = []
    logged: list[str] = []
    outcomes = stop_processes([9200], table=lambda: processes,
                              killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, log=logged.append)
    assert killed == []
    assert outcomes[0].action == "refused"
    assert outcomes[0].reaches_claude == 9000
    assert exit_code(outcomes) != 0


# -- acceptance row 3: 12 processes, parent gone, chain reaches no live session -> 12 of 12 stopped --

def test_row3_twelve_untied_processes_all_stopped():
    now = 2000.0
    # a live session elsewhere in the table -- these 12 rows never reach it
    processes = [_row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg')]
    orphan_pids = list(range(100, 112))
    for pid in orphan_pids:
        processes.append(_row(pid, ppid=99999, create_time=now - 820.0,
                              command_line='python -c "while True: pass"'))

    killed: list[int] = []
    logged: list[str] = []
    outcomes = stop_processes(orphan_pids, table=lambda: processes,
                              killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, log=logged.append)
    assert killed == orphan_pids
    assert len(killed) == 12
    assert all(o.action == "stopped" for o in outcomes)
    assert exit_code(outcomes) == 0


# -- --dry-run stops nothing, and a not-found pid counts against the exit code -----------------------

def test_dry_run_stops_nothing():
    now = 2000.0
    processes = [_row(500, ppid=99999, create_time=now - 820.0, command_line="sleep 300")]
    killed: list[int] = []
    outcomes = stop_processes([500], table=lambda: processes,
                              killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, dry_run=True)
    assert killed == []
    assert outcomes[0].action == "dry-run"
    assert exit_code(outcomes) == 0


def test_a_pid_not_in_the_table_is_reported_not_found_and_fails_the_exit_code():
    outcomes = stop_processes([424242], table=lambda: [], killer=lambda pid: True, now=2000.0)
    assert outcomes[0].action == "not-found"
    assert exit_code(outcomes) != 0


# -- #258 row 1: MSYS table unreadable, a child of a live session's Git Bash shell whose Windows
# parent is gone -> refused, killer never called, exit code != 0, the line names the unread table --

def test_row1_258_msys_unreadable_refuses_without_computing_chain():
    now = 2000.0
    processes = [
        _row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(9050, ppid=9000, create_time=0.0, command_line="bash.exe",
            msys_pid=500, msys_ppid=1, msys_pgid=500),
        # the Windows ppid names an already-dead launcher (99999) -- only the MSYS chain
        # (msys_ppid=500, the live shell above) ties this to the live session, and that chain is
        # exactly what an unreadable table makes unavailable
        _row(9300, ppid=99999, create_time=now - 5.0, command_line="python worker.py",
            msys_pid=501, msys_ppid=500, msys_pgid=500),
    ]
    killed: list[int] = []
    logged: list[str] = []
    outcomes = stop_processes([9300], table=lambda: processes, msys_ok=False,
                              killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, log=logged.append)
    assert killed == []
    assert len(outcomes) == 1
    assert outcomes[0].action == "refused"
    assert exit_code(outcomes) != 0
    assert len(logged) == 1
    assert "9300" in logged[0] and "msys table" in logged[0].lower()


# -- #258 row 2: the same failure, 12 processes whose parent is gone -> 12 of 12 refused, 0 stopped -

def test_row2_258_msys_unreadable_refuses_all_twelve():
    now = 2000.0
    processes = [_row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg')]
    pids = list(range(200, 212))
    for pid in pids:
        processes.append(_row(pid, ppid=99999, create_time=now - 820.0,
                              command_line='python -c "while True: pass"'))

    killed: list[int] = []
    outcomes = stop_processes(pids, table=lambda: processes, msys_ok=False,
                              killer=lambda pid: (killed.append(pid), True)[1], now=now,
                              log=lambda _line: None)
    assert len(pids) == 12
    assert killed == []
    assert all(o.action == "refused" for o in outcomes)
    assert exit_code(outcomes) != 0


# -- #258 row 3: `ps` not found -> the Windows chain decides, as before this issue -----------------

def test_row3_258_ps_not_found_returns_empty_table_without_raising(monkeypatch):
    import tools.agentops.orphans as orphans

    monkeypatch.setattr(orphans, "_find_ps_exe", lambda: None)
    assert orphans.real_msys_table() == {}


def test_row3_258_windows_chain_alone_decides_when_ps_not_found():
    now = 2000.0
    processes = [
        _row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(9100, ppid=9000, create_time=now - 180.0, command_line="python worker.py"),
    ]
    outcomes = stop_processes([9100], table=lambda: processes, msys_ok=True,
                              killer=lambda pid: True, now=now, log=lambda _line: None)
    assert outcomes[0].action == "refused"
    assert outcomes[0].reaches_claude == 9000


# -- #258 row 4: lines printed for 3 pids (one refused, one stopped, one not found) -> 3 lines -----

def test_row4_258_exactly_one_line_per_pid():
    now = 2000.0
    processes = [
        _row(9000, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(9100, ppid=9000, create_time=now - 180.0, command_line="python worker.py"),  # refused
        _row(9200, ppid=99999, create_time=now - 820.0, command_line="python orphan.py"),  # stopped
    ]
    logged: list[str] = []
    outcomes = stop_processes([9100, 9200, 424242], table=lambda: processes,
                              killer=lambda pid: True, now=now, log=logged.append)
    assert len(logged) == 3
    assert [o.action for o in outcomes] == ["refused", "stopped", "not-found"]


# -- #258, ORCH's review: one ps -eW read decides both the chains and the refusal -----------------

def test_258_real_run_refuses_when_the_one_table_read_fails(monkeypatch):
    """A real run (no `table`, no `msys_ok`) reads the process table once with `strict_msys=True`;
    when that read's MSYS table is unreadable every pid is refused -- no second `ps -eW` read that
    could succeed where the one behind the chains failed."""
    import tools.agentops.stop as stop_mod
    from tools.agentops.orphans import MsysTableUnreadable

    now = 2000.0
    windows_rows = [
        _row(9000, ppid=1, create_time=0.0, command_line="claude.exe --bg --agent orch"),
        _row(9300, ppid=99999, create_time=now - 5.0, command_line="python worker.py"),
    ]
    calls: list[bool] = []

    def fake_table(*, strict_msys: bool = False):
        calls.append(strict_msys)
        exc = MsysTableUnreadable("ps -eW timed out")
        exc.rows = windows_rows
        raise exc

    monkeypatch.setattr(stop_mod, "real_process_table", fake_table)
    killed: list[int] = []
    logged: list[str] = []
    outcomes = stop_processes([9300], killer=lambda pid: (killed.append(pid), True)[1],
                              now=now, log=logged.append)
    assert calls == [True]
    assert killed == []
    assert [o.action for o in outcomes] == ["refused"]
    assert exit_code(outcomes) != 0
    assert len(logged) == 1 and "9300" in logged[0] and "msys table" in logged[0].lower()


def test_258_merge_msys_strict_reraises_with_rows_and_the_sweep_does_not(monkeypatch):
    import pytest

    import tools.agentops.orphans as orphans_mod

    def unreadable():
        raise orphans_mod.MsysTableUnreadable("ps -eW exited 1")

    monkeypatch.setattr(orphans_mod, "real_msys_table", unreadable)
    rows = [_row(9300, ppid=99999, create_time=1.0, command_line="python worker.py")]
    assert orphans_mod._merge_msys(rows) == rows
    with pytest.raises(orphans_mod.MsysTableUnreadable) as info:
        orphans_mod._merge_msys(rows, strict=True)
    assert info.value.rows == rows


# -- #890: the trial (dry_run=True) and the real call walk one chain and give one verdict -------------

def _both_modes(pid: int, processes: list[ProcRow], now: float):
    """(dry-run outcome, real outcome, lines logged by each, pids the real call killed) on one table."""
    trial_log: list[str] = []
    real_log: list[str] = []
    killed: list[int] = []
    trial = stop_processes([pid], table=lambda: processes, killer=lambda p: (killed.append(p), True)[1],
                           dry_run=True, now=now, log=trial_log.append)[0]
    assert killed == []  # the trial never kills
    real = stop_processes([pid], table=lambda: processes, killer=lambda p: (killed.append(p), True)[1],
                          dry_run=False, now=now, log=real_log.append)[0]
    return trial, real, trial_log, real_log, killed


def test_53_map_run_and_an_old_full_run_are_never_a_worker_leftover():
    from tools.agentops.stop import _worktree_selection
    wt = r"D:\Work\orchestration-layer\.claude\worktrees\wt-871-u1"
    rows = [
        _row(100, ppid=1, create_time=0.0, command_line=f"python -m tools.agentops map-run --commit abc --cwd {wt}"),
        _row(101, ppid=1, create_time=0.0, command_line=f"python -m tools.agentops full-run --commit abc --cwd {wt}"),
        _row(102, ppid=1, create_time=0.0, command_line=f"python -m pytest {wt}\\tests"),
    ]
    assert [p.pid for p in _worktree_selection("wt-871-u1", rows)] == [102]


def test_890_chain_reaching_a_live_session_is_refused_in_both_modes_with_the_same_reaches():
    now = 1.7e9
    processes = [
        _row(13836, ppid=1, create_time=0.0, command_line=r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(25864, ppid=13836, create_time=0.0, command_line="bash.exe"),
        _row(4200, ppid=25864, create_time=now - 3 * 3600.0,
             command_line="python -m tools.agentops map-run --commit d37672a2"),
    ]
    trial, real, trial_log, real_log, killed = _both_modes(4200, processes, now)
    assert trial.action == real.action == "refused"
    assert trial.reaches_claude == real.reaches_claude == 13836
    assert killed == []
    assert trial_log == real_log  # the same head and the same verdict text, line for line


def test_890_chain_reaching_none_would_stop_in_the_trial_and_stops_for_real():
    now = 1.7e9
    processes = [
        _row(25864, ppid=1, create_time=0.0, command_line="bash.exe"),
        _row(4200, ppid=25864, create_time=now - 3 * 3600.0,
             command_line="python -m tools.agentops full-run --commit d37672a2"),
    ]
    trial, real, trial_log, real_log, killed = _both_modes(4200, processes, now)
    assert (trial.action, real.action) == ("dry-run", "stopped")
    assert trial.reaches_claude is None and real.reaches_claude is None
    assert killed == [4200]
    assert "would stop" in trial_log[0] and "stopped" in real_log[0]
    assert trial_log[0].split(" -- ")[:2] == real_log[0].split(" -- ")[:2]  # the same head (reaches none)
