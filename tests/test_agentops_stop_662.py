"""#662: stopping a detach wrapper also ends the one command it runs (child first)."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from tools.agentops.orphans import ProcRow, real_process_table, taskkill_killer
from tools.agentops.stop import stop_processes

W_CMD = ("pythonw -m tools.agentops.detach run --result r.json --kind commit --branch  "
         "--log merge.log -- python -m tools.agentops commit -m x --state-as brain")
C_CMD = "python -m tools.agentops commit -m x --state-as brain"
NOW = 5000.0


def _row(pid, ppid, cmd):
    return ProcRow(pid=pid, ppid=ppid, create_time=NOW - 60.0, command_line=cmd,
                   msys_pid=None, msys_ppid=None, msys_pgid=None)


def _run(table, pids, **kw):
    killed, logged = [], []
    outcomes = stop_processes(pids, table=lambda: table,
                              killer=lambda p: (killed.append(p), True)[1],
                              now=NOW, log=logged.append, **kw)
    return outcomes, killed, logged


def _wc():
    return [_row(100, 1, W_CMD), _row(200, 100, C_CMD), _row(300, 200, "git status")]


def test_row1_wrapper_stops_child_then_wrapper_one_line():
    outcomes, killed, logged = _run(_wc(), [100])
    assert killed == [200, 100]
    assert len(logged) == 1 and "200" in logged[0] and "pid 100" in logged[0]
    assert "its command pid 200 stopped" in logged[0]
    assert [o.action for o in outcomes] == ["stopped"]


def test_row2_dry_run_names_both_kills_nothing():
    outcomes, killed, logged = _run(_wc(), [100], dry_run=True)
    assert killed == []
    assert len(logged) == 1 and "would stop" in logged[0]
    assert "its command pid 200 would stop" in logged[0]
    assert outcomes[0].action == "dry-run"


def test_row3_live_claude_ancestor_refuses_both():
    table = [_row(50, 1, r'"C:\Claude\claude.exe" --bg')] + [
        _row(100, 50, W_CMD), _row(200, 100, C_CMD)]
    outcomes, killed, logged = _run(table, [100])
    assert killed == []
    assert outcomes[0].action == "refused"
    assert len(logged) == 1 and "refused" in logged[0]


def test_row4_wrapper_alone_one_kill():
    outcomes, killed, logged = _run([_row(100, 1, W_CMD)], [100])
    assert killed == [100]
    assert outcomes[0].action == "stopped" and len(logged) == 1


def test_row5_child_alone_one_kill():
    outcomes, killed, logged = _run([_row(200, 1, C_CMD)], [200])
    assert killed == [200]
    assert outcomes[0].action == "stopped" and len(logged) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="WMI-launched wrapper is Windows only")
def test_row6_real_detached_sleep_is_gone_within_5s_of_the_stop(tmp_path, monkeypatch):
    from tools.agentops import detach
    monkeypatch.setenv(detach.ENV_RESULTS_DIR, str(tmp_path / "results"))
    monkeypatch.setenv(detach.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    marker = "import time; time.sleep(120)"

    sleeper = []  # #68: the pid of the wrapper's command child, captured while waiting below

    def leftovers():
        return [p for p in real_process_table() if p.pid in sleeper and marker in p.command_line]

    import shutil
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("a bash shell is needed to quote the marker")
    _, wpid = detach.launch([sys.executable, "-c", marker], "commit", None)
    # #68: a bystander shell whose command line only quotes the marker; it must never count as a leftover
    bystander = subprocess.Popen([bash, "-c", f"echo '{marker}'; cat"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            table = real_process_table()
            kids = [p.pid for p in table if p.ppid == wpid and marker in p.command_line]
            if kids and not sleeper:
                sleeper.extend(kids)  # captured as soon as seen, before the bystander is checked
            if sleeper and any(p.pid == bystander.pid and marker in p.command_line for p in table):
                break
            time.sleep(0.5)
        else:
            pytest.fail("the wrapper's command child or the bystander shell never appeared")
        stop_processes([wpid], log=lambda _l: None)
        # #754: under load one process-table read alone took up to ~7 s (stop 9.7 s, poll 7.0 s
        # measured beside 8 CPU burners), so a 5 s bound measured the machine, not `stop`. The
        # patience is 6 x the largest measured interval (9.71 s); the assert is "gone eventually".
        deadline = time.time() + 60
        while time.time() < deadline and leftovers():
            time.sleep(0.25)
        # the bystander is still alive and quoting the marker, and leftovers() does not count it
        assert bystander.poll() is None
        assert any(p.pid == bystander.pid and marker in p.command_line for p in real_process_table())
        assert leftovers() == []
    finally:
        taskkill_killer(wpid)
        for p in leftovers():
            taskkill_killer(p.pid)
        taskkill_killer(bystander.pid)
        bystander.stdin.close()  # last: a raise here must not skip the kills above
