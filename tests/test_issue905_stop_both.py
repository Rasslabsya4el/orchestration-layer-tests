"""#905: stopping either process of the dispatcher ends both; `live pid` names the commit."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from tools.agentops import dispatch
from tools.agentops.orphans import ProcRow, real_process_table, taskkill_killer
from tools.agentops.stop import stop_processes

MARK = ["-m", "tools.agentops", "dispatch"]
NOW = 5000.0


def _row(pid, ppid, cmd):
    return ProcRow(pid=pid, ppid=ppid, create_time=NOW - 60.0, command_line=cmd,
                   msys_pid=None, msys_ppid=None, msys_pgid=None)


def _fake():
    return [_row(100, 1, 'cmd /c cd /d "C:/x" && "python.exe" -m tools.agentops dispatch >> o 2>&1'),
            _row(200, 100, '"python.exe" -m tools.agentops dispatch')]


def _run(table, pids):
    killed = []
    stop_processes(pids, table=lambda: table, killer=lambda p: (killed.append(p), True)[1],
                   now=NOW, log=lambda _l: None)
    return killed


def test_fake_wrapper_ends_both():
    assert _run(_fake(), [100]) == [200, 100]


def test_fake_python_ends_both():
    assert _run(_fake(), [200]) == [200, 100]


def test_fake_claude_chain_still_refused():
    table = [_row(50, 1, r'"C:\Claude\claude.exe" --bg')] + [
        _row(100, 50, _fake()[0].command_line), _row(200, 100, _fake()[1].command_line)]
    assert _run(table, [100]) == [] and _run(table, [200]) == []


def test_live_pid_line_names_the_commit(tmp_path, capsys, monkeypatch):
    import os
    import json
    lock = tmp_path / "dispatch.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started": dispatch._now_str(),
                                "commit": "abcd1234"}))
    assert dispatch.run_ensure(lock_path=lock, out_path=tmp_path / "o",
                               launch=lambda **k: 1, root=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"live pid {os.getpid()} commit abcd1234" in out


def test_lock_records_commit(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(dispatch, "_head_commit", lambda: "abcd1234")  # #367: the lock's commit, not the real HEAD
    lock = tmp_path / "dispatch.lock"
    with dispatch.dispatcher_lock(lock):
        data = json.loads(lock.read_text())
        assert "commit" in data


PARENT_CODE = ("import subprocess,sys,time;"
               "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']+sys.argv[1:],"
               "creationflags=0x08000000);time.sleep(120)")


def _spawn_pair(tmp_path, monkeypatch):
    # started through the detach launcher (WMI): its chain never reaches this session's claude.exe
    from tools.agentops import detach
    monkeypatch.setenv(detach.ENV_RESULTS_DIR, str(tmp_path / "results"))
    monkeypatch.setenv(detach.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    marker = "time.sleep(120)"
    _, wpid = detach.launch([sys.executable, "-c", PARENT_CODE, *MARK], "commit", None)
    deadline = time.time() + 40
    while time.time() < deadline:
        table = real_process_table()
        parents = [p for p in table if p.ppid == wpid and "subprocess.Popen" in p.command_line]
        for par in parents:
            kids = [p for p in table if p.ppid == par.pid and marker in p.command_line]
            if kids:
                return wpid, par.pid, kids[0].pid
        time.sleep(0.3)
    taskkill_killer(wpid)
    pytest.fail("pair never appeared")


def _alive(pids):
    return [p.pid for p in real_process_table() if p.pid in pids]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@pytest.mark.parametrize("which", ["wrapper", "child"])
def test_real_pair_both_gone(which, tmp_path, monkeypatch):
    wpid, parent, child = _spawn_pair(tmp_path, monkeypatch)
    try:
        stop_processes([parent if which == "wrapper" else child], log=lambda _l: None)
        deadline = time.time() + 60
        while time.time() < deadline and _alive({parent, child}):
            time.sleep(0.25)
        assert _alive({parent, child}) == []
    finally:
        for pid in (child, parent, wpid):
            taskkill_killer(pid)
