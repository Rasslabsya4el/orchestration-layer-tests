"""#217: `tools.agentops.orphans` on a fake process table -- no real process is ever touched."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from tools.agentops import orphans as orphans_module
from tools.agentops.orphans import (ProcRow, find_orphans, find_stale_whole_disk_searches,
                                    find_untied_candidates, is_orphan_command, snapshot_owned_pids,
                                    stop_orphans, sweep_orphans)


@pytest.fixture(autouse=True)
def _side_files_in_tmp(tmp_path, monkeypatch):
    """#1071: the side-file folder is redirected to tmp_path for every test here, so no test ever
    writes the real `artifacts/runs/orphans`."""
    monkeypatch.setattr(orphans_module, "ORPHANS_DIR", tmp_path / "orphans")
    return tmp_path / "orphans"


def _side_lines() -> list[str]:
    """Every line of every side file in the (redirected) folder, in file-name order."""
    folder = orphans_module.ORPHANS_DIR
    if not folder.exists():
        return []
    out: list[str] = []
    for path in sorted(folder.glob("*.txt")):
        out.extend(path.read_text(encoding="utf-8").splitlines())
    return out


def _row(pid: int, ppid: int, create_time: float, command_line: str, *,
        msys_pid: int | None = None, msys_ppid: int | None = None,
        msys_pgid: int | None = None) -> ProcRow:
    return ProcRow(pid=pid, ppid=ppid, create_time=create_time, command_line=command_line,
                  msys_pid=msys_pid, msys_ppid=msys_ppid, msys_pgid=msys_pgid)


# -- is_orphan_command -------------------------------------------------------------------------------

def test_is_orphan_command_matches_the_named_executables():
    assert is_orphan_command(r'"C:\Program Files\Git\usr\bin\find.exe" / -name *.log')
    assert is_orphan_command("grep -r foo .")
    assert is_orphan_command("rg pattern src")
    assert is_orphan_command("tail -f dispatch.out")
    assert is_orphan_command("sleep 300")


def test_is_orphan_command_matches_bash_until_loop():
    assert is_orphan_command('bash -c "until grep -q done state.txt; do sleep 5; done"')


def test_is_orphan_command_rejects_substring_lookalikes():
    assert not is_orphan_command(r"C:\tools\grepper.exe --version")
    assert not is_orphan_command(r'"C:\Program Files\Findify\findify.exe" --scan')
    assert not is_orphan_command("python -m tools.agentops dispatch")
    assert not is_orphan_command("")


# -- find_orphans -------------------------------------------------------------------------------------

def test_two_orphans_in_two_stopped():
    """Acceptance row 2: 2 orphans in -> 2 stopped, a process with a live parent untouched."""

    session_started_at = 1000.0
    processes = [
        _row(100, ppid=1, create_time=1010.0, command_line="find / -name *.go"),          # orphan
        _row(101, ppid=1, create_time=1020.0, command_line="grep -r foo /"),               # orphan
        _row(102, ppid=200, create_time=1015.0, command_line="find / -name *.py"),         # live parent
    ]
    live_pids = {200}  # pid 200 (the live parent) is alive; 100/101's parent (1) is not in the table
    orphans = find_orphans(processes, session_started_at=session_started_at, live_pids=live_pids)
    assert {p.pid for p in orphans} == {100, 101}

    stopped_pids: list[int] = []
    logged: list[str] = []
    stopped = stop_orphans(orphans, killer=lambda pid: stopped_pids.append(pid) or True,
                           log=logged.append)
    assert {p.pid for p in stopped} == {100, 101}
    assert stopped_pids == [100, 101]
    assert len(logged) == 2
    assert all("stopped orphan pid" in line for line in logged)


def test_process_started_before_the_session_is_never_an_orphan():
    session_started_at = 1000.0
    processes = [_row(100, ppid=1, create_time=999.0, command_line="find / -name *.go")]
    orphans = find_orphans(processes, session_started_at=session_started_at, live_pids=set())
    assert orphans == []


def test_another_sessions_or_the_owners_process_is_never_stopped():
    """Acceptance row 3: any live parent, or a command line outside the list -> 0 stopped."""

    session_started_at = 1000.0
    processes = [
        # another session's `find`, but its parent (the other session's shell, pid 500) is alive
        _row(300, ppid=500, create_time=1050.0, command_line="find / -name *.go"),
        # the owner's editor, no live parent, command line outside the orphan list
        _row(301, ppid=1, create_time=1060.0, command_line="notepad.exe C:\\notes.txt"),
    ]
    live_pids = {500}
    orphans = find_orphans(processes, session_started_at=session_started_at, live_pids=live_pids)
    assert orphans == []


# -- sweep_orphans (the wiring point dispatch.py calls) ------------------------------------------------

def test_sweep_orphans_reads_the_table_finds_and_stops():
    processes = [
        _row(100, ppid=1, create_time=1010.0, command_line="find / -name *.go"),
        # pid 200 is itself a row (still running) -- its child at pid 102 has a live parent
        _row(200, ppid=1, create_time=900.0, command_line="powershell.exe -NoExit"),
        _row(102, ppid=200, create_time=1015.0, command_line="find / -name *.py"),
    ]
    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_orphans(
        session_started_at=1000.0,
        log=logged.append,
        table=lambda: processes,
        killer=lambda pid: (killed.append(pid), True)[1],
    )
    # `sweep_orphans` derives `live_pids` from the table itself: 200 is present as a row (alive), so
    # its child 102 is left untouched; 100's parent (pid 1) is absent from the table, so 100 is the
    # only orphan here.
    assert killed == [100]
    assert [p.pid for p in stopped] == [100]
    assert len(logged) == 1


# -- #225's own follow-up: the MSYS chain's Git-Bash-launcher boundary, and the OPEN RISK ------------------

def _msys_session_table():
    """Reproduces the exact chain #225's follow-up measured live (issue #225's own quoted rows): a
    fake claude.exe (1000) spawns a Git Bash launcher (2000), which exec's into the real MSYS
    `bash.exe` (3000, msys_pid=30, msys_ppid=1 -- the boundary: MSYS has no tracked parent for it,
    but its OWN Windows `ppid` correctly names 2000); 3000 forks a subshell (msys_pid=31, msys_ppid=30,
    winpid=4000) that runs the leaf `sleep 300` (msys_pid=32, msys_ppid=31, winpid=5000, ppid=9999 --
    9999 is the already-dead launcher stub #217 first measured, never present in the table)."""

    return [
        _row(1000, ppid=1, create_time=1000.0, command_line="python fake_claude.py"),
        _row(2000, ppid=1000, create_time=1000.0,
            command_line='"C:\\Program Files\\Git\\bin\\bash.exe" -c "sleep 300 &"',
            msys_pid=20, msys_ppid=0),
        _row(3000, ppid=2000, create_time=1000.0,
            command_line='"C:\\Program Files\\Git\\bin\\..\\usr\\bin\\bash.exe" -c "sleep 300 &"',
            msys_pid=30, msys_ppid=1),
        _row(4000, ppid=8888, create_time=1000.0, command_line="/usr/bin/bash",
            msys_pid=31, msys_ppid=30),
        _row(5000, ppid=9999, create_time=1010.0, command_line='"...\\sleep.exe" 300',
            msys_pid=32, msys_ppid=31),
    ]


def test_chain_falls_back_to_windows_ppid_at_the_msys_launcher_boundary():
    """#225's own follow-up: the leaf's MSYS chain (32 -> 31 -> 30) dead-ends at msys_ppid=1 (row
    3000), but 3000's own Windows `ppid` (2000) is live and correctly chains on to the session's
    claude.exe (1000) -- so the sweep (while everything is still alive) must tie the leaf back to it,
    never stop at the boundary as though the shell had exited."""

    processes = _msys_session_table()
    orphans = find_orphans(processes, session_started_at=999.0,
                           live_pids={p.pid for p in processes}, ended_claude_pid=1000)
    assert [p.pid for p in orphans] == [5000]


def test_recorded_owned_pids_ties_a_leaf_the_dead_sessions_own_bridge_can_no_longer_reach():
    """The OPEN RISK itself (issue #225's follow-up note): once the session ends, `taskkill /T` kills
    the bridge rows (2000, 3000, 4000) that made the chain walk above possible -- their Windows ppid
    relationships were correct and taskkill's own tree-kill found them. The leaf (5000) survives
    (its own `ppid` was always the dead #217 launcher stub, never a real child of anything taskkill's
    tree-walk could find). With only the post-death table, NO chain walk can reach 1000 any more --
    `recorded_owned_pids` (captured while everything was alive, e.g. from `snapshot_owned_pids`) is
    the only tie left, and the sweep must use it."""

    processes = _msys_session_table()
    owned = snapshot_owned_pids([1000], table=lambda: processes)[1000]
    assert 5000 in owned  # captured while alive, matching the test above

    # the session ends: the bridge (1000, 2000, 3000, 4000) is gone from the table, only the leaf remains
    post_death = [p for p in processes if p.pid == 5000]
    assert [p.pid for p in post_death] == [5000]

    orphans_without_the_tie = find_orphans(post_death, session_started_at=999.0,
                                          live_pids={p.pid for p in post_death},
                                          ended_claude_pid=1000)
    assert orphans_without_the_tie == []  # confirms the risk: no chain reaches 1000 any more

    untied = find_untied_candidates(post_death, session_started_at=999.0, ended_claude_pid=1000,
                                    live_claude_pids=())
    assert [p.pid for p in untied] == [5000]  # left alone, one line -- without the recorded tie

    orphans_with_the_tie = find_orphans(post_death, session_started_at=999.0,
                                       live_pids={p.pid for p in post_death},
                                       ended_claude_pid=1000, recorded_owned_pids=owned)
    assert [p.pid for p in orphans_with_the_tie] == [5000]

    untied_with_the_tie = find_untied_candidates(post_death, session_started_at=999.0,
                                                ended_claude_pid=1000, live_claude_pids=(),
                                                recorded_owned_pids=owned)
    assert untied_with_the_tie == []  # no longer "left alone": the recorded tie stops it instead

    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_orphans(session_started_at=999.0, log=logged.append, ended_claude_pid=1000,
                            recorded_owned_pids=owned, table=lambda: post_death,
                            killer=lambda pid: (killed.append(pid), True)[1])
    assert killed == [5000]
    assert [p.pid for p in stopped] == [5000]
    assert len(logged) == 1


def test_recorded_owned_pids_never_ties_a_process_reaching_a_live_session():
    """A pid recorded as the ended session's own is still checked against `live_claude_pids` -- if
    the CURRENT table's chain now reaches a still-live session instead (e.g. the pid was reused, or
    the row was tied to a live one all along), it is never stopped."""

    processes = [
        _row(1000, ppid=1, create_time=1000.0, command_line="python fake_claude.py"),
        _row(2000, ppid=1, create_time=1000.0, command_line="python other_live_claude.py"),
        _row(5000, ppid=2000, create_time=1010.0, command_line="sleep 300"),
    ]
    orphans = find_orphans(processes, session_started_at=999.0,
                           live_pids={p.pid for p in processes}, ended_claude_pid=1000,
                           live_claude_pids=[2000], recorded_owned_pids=[5000])
    assert orphans == []


# -- #239's own follow-up: a subshell-wrapped background pipeline ((cmd | cmd &)) ---------------------

def _subshell_session_table():
    """Reproduces the exact shape measured live on the worker's own processes (#239): a fake
    claude.exe (1000) spawns a Git Bash launcher (2000), which runs the real MSYS `bash.exe` (3000,
    msys_pid=30, msys_ppid=1 -- the #225 launcher boundary; msys_pgid=30, its own group leader:
    non-interactive bash gives a backgrounded pipeline no process group of its own). That shell runs
    `(sleep 1000 | tail -f /dev/null &)`: the wrapping subshell exits at once, so its two children --
    the leaf `sleep` (6000) and `tail` (6001) -- come back with BOTH their MSYS `ppid` (1, unresolvable)
    AND their Windows `ppid` (9999, the already-dead launcher stub, never in the table) broken; only
    their `msys_pgid` (30) still names the live shell that started them, exactly as measured."""

    return [
        _row(1000, ppid=1, create_time=1000.0, command_line="python fake_claude.py"),
        _row(2000, ppid=1000, create_time=1000.0,
            command_line='"C:\\Program Files\\Git\\bin\\bash.exe" -c "(sleep 1000 | tail -f /dev/null &)"',
            msys_pid=20, msys_ppid=0),
        _row(3000, ppid=2000, create_time=1000.0,
            command_line='"C:\\Program Files\\Git\\bin\\..\\usr\\bin\\bash.exe" -c "(sleep 1000 | tail -f /dev/null &)"',
            msys_pid=30, msys_ppid=1, msys_pgid=30),
        _row(6000, ppid=9999, create_time=1010.0, command_line='"...\\sleep.exe" 1000',
            msys_pid=41, msys_ppid=1, msys_pgid=30),
        _row(6001, ppid=9999, create_time=1010.0, command_line='"...\\tail.exe" -f /dev/null',
            msys_pid=42, msys_ppid=1, msys_pgid=30),
    ]


def test_neither_msys_nor_windows_ppid_ties_the_subshell_leaves_without_the_pgid_fallback():
    """The defect itself (#239, acceptance row 3): with `msys_pgid` stripped (the pre-fix shape --
    every OTHER field exactly as measured), `find_orphans` cannot tie either leaf back to the session's
    `claude.exe`: 2 of the shell's own 4 backgrounded processes (the subshell-wrapped pair) come up
    untied, even though everything is still alive."""

    processes = [
        p if p.pid not in (6000, 6001) else
        ProcRow(pid=p.pid, ppid=p.ppid, create_time=p.create_time, command_line=p.command_line,
               msys_pid=p.msys_pid, msys_ppid=p.msys_ppid, msys_pgid=None)
        for p in _subshell_session_table()
    ]
    orphans = find_orphans(processes, session_started_at=999.0,
                           live_pids={p.pid for p in processes}, ended_claude_pid=1000)
    assert orphans == []  # neither leaf ties -- the leak: 2 of 4 not tied

    untied = find_untied_candidates(processes, session_started_at=999.0, ended_claude_pid=1000,
                                    live_claude_pids=())
    assert {p.pid for p in untied} == {6000, 6001}


def test_msys_pgid_ties_the_subshell_leaves_while_the_owning_shell_is_still_alive():
    """After the fix (acceptance row 2/4): both leaves' `msys_pgid` (30) names the still-live shell
    (3000), so the chain now reaches `claude.exe` (1000) for both -- `find_orphans` ties them, and
    `snapshot_owned_pids` (the live-caught tie the dispatcher takes every 60s while the session lives)
    records both, exactly as it must for the session to still be able to stop them once it has ended."""

    processes = _subshell_session_table()
    orphans = find_orphans(processes, session_started_at=999.0,
                           live_pids={p.pid for p in processes}, ended_claude_pid=1000)
    assert {p.pid for p in orphans} == {6000, 6001}

    owned = snapshot_owned_pids([1000], table=lambda: processes)[1000]
    assert {6000, 6001} <= owned


def test_msys_pgid_tie_survives_the_shells_own_death_only_through_recorded_owned_pids():
    """Acceptance row 5: once the session ends and the bridge (2000, 3000 -- the shell whose pgid
    made the tie possible) is gone, a fresh chain walk over the leaves alone can no longer resolve
    anything (their own `msys_pgid` now names nobody in the table) -- `recorded_owned_pids`, captured
    while the shell was still alive, is what ties (and stops) both: 2 of 2 stopped, one line each."""

    processes = _subshell_session_table()
    owned = snapshot_owned_pids([1000], table=lambda: processes)[1000]

    post_death = [p for p in processes if p.pid in (6000, 6001)]
    orphans_without_the_tie = find_orphans(post_death, session_started_at=999.0,
                                          live_pids={p.pid for p in post_death},
                                          ended_claude_pid=1000)
    assert orphans_without_the_tie == []  # the bridge (and its pgid) is gone: no fresh chain resolves

    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_orphans(session_started_at=999.0, log=logged.append, ended_claude_pid=1000,
                            recorded_owned_pids=owned, table=lambda: post_death,
                            killer=lambda pid: (killed.append(pid), True)[1])
    assert set(killed) == {6000, 6001}
    assert {p.pid for p in stopped} == {6000, 6001}
    assert len(logged) == 2


def test_subshell_leaf_tied_to_a_live_other_session_is_never_stopped():
    """Acceptance row 6: a process of the same subshell-leak shape, but whose `msys_pgid` chain
    reaches a DIFFERENT, still-live session's `claude.exe`, is never touched, whatever else is true."""

    processes = [
        _row(1000, ppid=1, create_time=1000.0, command_line="python fake_claude.py"),
        _row(5000, ppid=1, create_time=1000.0, command_line="python other_live_claude.py"),
        _row(5030, ppid=5000, create_time=1000.0, command_line="/usr/bin/bash",
            msys_pid=53, msys_ppid=1, msys_pgid=53),
        _row(5060, ppid=9999, create_time=1010.0, command_line='"...\\sleep.exe" 1000',
            msys_pid=54, msys_ppid=1, msys_pgid=53),
    ]
    orphans = find_orphans(processes, session_started_at=999.0,
                           live_pids={p.pid for p in processes}, ended_claude_pid=1000,
                           live_claude_pids=[5000])
    assert orphans == []


def test_whole_disk_sweep_ties_a_subshell_wrapped_stale_search():
    """Acceptance row 7: a stale `find /` in the same subshell shape (its own `ppid` dead, `msys_ppid`
    unresolvable) is tied to a live `claude.exe` through its `msys_pgid` exactly like a `sleep`/`tail`
    leaf -- the whole-disk sweep draws no live/ended distinction, so this is the same verdict either
    way once the tie resolves."""

    processes = [
        _row(1000, ppid=1, create_time=0.0, command_line="claude.exe"),
        _row(2000, ppid=1000, create_time=0.0, command_line="/usr/bin/bash",
            msys_pid=20, msys_ppid=1, msys_pgid=20),
        _row(6000, ppid=9999, create_time=0.0, command_line="find / -name *.log",
            msys_pid=41, msys_ppid=1, msys_pgid=20),
    ]
    stale = find_stale_whole_disk_searches(processes, now=1000.0, min_age_seconds=600.0)
    assert [p.pid for p in stale] == [6000]


# -- #947: the sweep's veto set holds EVERY live claude.exe, not only the ORCH sessions' ---------------

def _947_sweep(processes, *, recorded=(), live_orch=()):
    """`sweep_orphans` on a fake table, as `Dispatcher._sweep_orphans` calls it: ended session E is
    `claude.exe` 1000, the ORCH sessions' own `live_claude_pids` are `live_orch`, nothing else."""
    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_orphans(
        session_started_at=999.0,
        log=logged.append,
        ended_claude_pid=1000,
        live_claude_pids=list(live_orch),
        recorded_owned_pids=frozenset(recorded),
        table=lambda: processes,
        killer=lambda pid: (killed.append(pid), True)[1],
    )
    return stopped, killed, logged


def _947_ended_and_autopilot_table():
    """E (`claude.exe` 1000) has ended but is still listed; L (`claude.exe` 2000) is a live autopilot
    session the dispatcher does not run, so it is NOT in the ORCH sessions' `live_claude_pids`. The
    grep (3000) is a leaf of L's own bash shell (2500): its Windows parent (4000) is already gone and
    its chain is reached only through the MSYS ppid (31 -> 25 -> bash 2500 -> L 2000)."""
    return [
        _row(1000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p ended'),
        _row(2000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p autopilot'),
        _row(2500, ppid=2000, create_time=950.0, command_line="/usr/bin/bash",
             msys_pid=25, msys_ppid=1, msys_pgid=25),
        _row(3000, ppid=4000, create_time=1010.0, command_line="grep -r foo /c/x",
             msys_pid=31, msys_ppid=25, msys_pgid=31),
    ]


def test_947_grep_tied_to_the_ended_session_by_its_recorded_pid_is_not_stopped_when_it_reaches_a_live_autopilot():
    """Row 1 (variant a): the grep's only tie to E is the recorded owned set (E saw it while alive);
    its chain reaches L, a live `claude.exe` the ORCH sessions do not list -> never stopped, and the
    `left orphan candidate` line names L's pid (2000). Fails on the unchanged code: the grep is killed."""
    processes = _947_ended_and_autopilot_table()
    stopped, killed, logged = _947_sweep(processes, recorded={3000}, live_orch=[])
    assert killed == []
    assert stopped == []
    assert any(line.startswith("left orphan candidate pid 3000") and "2000" in line for line in _side_lines())


def test_947_grep_whose_chain_also_passes_a_shared_bash_recorded_for_the_ended_session_is_not_stopped():
    """Row 1 (variant b): the same grep, and the shared bash (2500) on its chain is ALSO in E's recorded
    owned set -- the bash is tied to E as well as to L. The veto by L still wins: never stopped.
    Fails on the unchanged code: the grep is killed."""
    processes = _947_ended_and_autopilot_table()
    stopped, killed, logged = _947_sweep(processes, recorded={2500, 3000}, live_orch=[])
    assert killed == []
    assert stopped == []
    assert any(line.startswith("left orphan candidate pid 3000") and "2000" in line for line in _side_lines())


def test_947_real_orphan_of_the_ended_session_is_still_stopped():
    """Row 2: a grep whose chain reaches only E (through its MSYS bash 3500, whose Windows parent is E)
    and no live session at all -> stopped, one kill, as today."""
    processes = [
        _row(1000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p ended'),
        _row(3500, ppid=1000, create_time=950.0, command_line="/usr/bin/bash",
             msys_pid=35, msys_ppid=1, msys_pgid=35),
        _row(3100, ppid=4100, create_time=1010.0, command_line="grep -r foo /c/x",
             msys_pid=41, msys_ppid=35, msys_pgid=41),
    ]
    stopped, killed, _logged = _947_sweep(processes)
    assert killed == [3100]
    assert [p.pid for p in stopped] == [3100]


def test_947_candidate_with_no_tie_to_any_session_is_left_alone_with_the_untied_line():
    """Row 3: a grep tied to neither E nor any live session -> 0 stops, the `left orphan candidate`
    line with its existing wording, as today."""
    processes = [
        _row(1000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p ended'),
        _row(3200, ppid=4200, create_time=1010.0, command_line="grep -r foo /c/x"),
    ]
    stopped, killed, logged = _947_sweep(processes)
    assert killed == []
    assert stopped == []
    assert len(logged) == 1
    assert "left 1 orphan candidates (0 tied to live sessions, 1 untied)" in logged[0]
    side = _side_lines()
    assert len(side) == 1
    assert side[0].startswith("left orphan candidate pid 3200 with no tie to the ended session or any live one")


# -- #1071: one summary line per sweep, the per-process lines in a side file, 14-day retention -------------

_NOW = datetime(2026, 10, 15, 14, 30)


def _five_candidates_table():
    """E (`claude.exe` 1000) has ended; L (`claude.exe` 2000) is still live. Three greps (3000-3002) reach
    L through the MSYS bash 2500 (tied to a live session); two greps (3200, 3201) reach neither (untied)."""
    rows = [
        _row(1000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p ended'),
        _row(2000, ppid=1, create_time=900.0, command_line='"C:\\Users\\u\\claude.exe" -p live'),
        _row(2500, ppid=2000, create_time=950.0, command_line="/usr/bin/bash",
             msys_pid=25, msys_ppid=1, msys_pgid=25),
    ]
    for pid in (3000, 3001, 3002):
        rows.append(_row(pid, ppid=4000, create_time=1010.0, command_line="grep -r foo /c/x",
                         msys_pid=pid - 2969, msys_ppid=25, msys_pgid=pid - 2969))
    for pid in (3200, 3201):
        rows.append(_row(pid, ppid=4200, create_time=1010.0, command_line="grep -r foo /c/y"))
    return rows


def test_five_candidates_leave_one_summary_line_and_a_five_line_side_file(tmp_path):
    """#1071 acceptance 1: 5 candidates (3 tied, 2 untied), 0 stopped -> exactly 1 log line naming 5, 3,
    2 and the side file; the side file holds 5 lines, each with its pid."""
    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_orphans(
        session_started_at=999.0, log=logged.append, ended_claude_pid=1000,
        live_claude_pids=[], recorded_owned_pids=frozenset(), table=_five_candidates_table,
        killer=lambda pid: (killed.append(pid), True)[1],
        ended_session="s42", orphans_dir=tmp_path / "orphans", now=_NOW,
    )
    assert stopped == []
    assert killed == []
    assert len(logged) == 1
    side_file = tmp_path / "orphans" / "20261015-1430-s42.txt"
    line = logged[0]
    assert "left 5 orphan candidates" in line
    assert "3 tied to live sessions" in line
    assert "2 untied" in line
    assert side_file.name in line
    side = side_file.read_text(encoding="utf-8").splitlines()
    assert len(side) == 5
    assert sorted(int(s.split()[4]) for s in side) == [3000, 3001, 3002, 3200, 3201]
    assert all(s.startswith("left orphan candidate pid ") for s in side)


def test_zero_candidates_write_no_log_line_and_no_side_file(tmp_path):
    """#1071 acceptance 1: 0 candidates -> 0 log lines, and the folder is not even created."""
    logged: list[str] = []
    sweep_orphans(
        session_started_at=999.0, log=logged.append, ended_claude_pid=1000,
        live_claude_pids=[], recorded_owned_pids=frozenset(),
        table=lambda: [_row(1000, ppid=1, create_time=900.0,
                            command_line='"C:\\Users\\u\\claude.exe" -p ended')],
        killer=lambda pid: True, ended_session="s42", orphans_dir=tmp_path / "orphans", now=_NOW,
    )
    assert logged == []
    assert not (tmp_path / "orphans").exists()


def test_side_file_older_than_fourteen_days_is_removed_and_thirteen_days_stays(tmp_path):
    """#1071 acceptance 2: a side file dated 15 days back is removed by the sweep, one dated 13 days back
    stays. The sweep here has no candidate at all: retention runs on every sweep."""
    folder = tmp_path / "orphans"
    folder.mkdir()
    old = folder / "20260930-1200-s1.txt"
    recent = folder / "20261002-1200-s2.txt"
    old.write_text("left orphan candidate pid 1 with no tie\n", encoding="utf-8")
    recent.write_text("left orphan candidate pid 2 with no tie\n", encoding="utf-8")
    fifteen_days = (_NOW - timedelta(days=15)).timestamp()
    thirteen_days = (_NOW - timedelta(days=13)).timestamp()
    os.utime(old, (fifteen_days, fifteen_days))
    os.utime(recent, (thirteen_days, thirteen_days))

    logged: list[str] = []
    sweep_orphans(
        session_started_at=999.0, log=logged.append, ended_claude_pid=1000,
        live_claude_pids=[], recorded_owned_pids=frozenset(),
        table=lambda: [_row(1000, ppid=1, create_time=900.0,
                            command_line='"C:\\Users\\u\\claude.exe" -p ended')],
        killer=lambda pid: True, ended_session="s42", orphans_dir=folder, now=_NOW,
    )
    assert not old.exists()
    assert recent.exists()
    assert logged == []
