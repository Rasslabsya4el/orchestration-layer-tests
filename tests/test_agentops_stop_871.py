"""#871 unit 1: `stop --worker-worktree <name>` on a fake process table -- no real process is touched."""

from __future__ import annotations

from tools.agentops.orphans import ProcRow
from tools.agentops.stop import exit_code, stop_processes, stop_worker_worktree

WT = r"D:\Work\target-repo-v2\.claude\worktrees\wt-871-u1"
SIB = r"D:\Work\target-repo-v2\.claude\worktrees\wt-871-u10"


def _row(pid, ppid, cmd):
    return ProcRow(pid=pid, ppid=ppid, create_time=0.0, command_line=cmd)


def _table():
    return [
        _row(100, 1, r'"C:\Claude\claude.exe" --bg --agent orch'),
        _row(110, 100, "bash.exe"),  # the ORCH's own shell, chain reaches claude
        # the returned worker's tree, all chaining through the live claude.exe
        _row(200, 110, f"python -m pytest {WT}/tests/test_x.py"),
        _row(201, 110, f"timeout 119 bash -c 'cd {WT}/.claude/scratch; until tail -n1 r.txt | grep -q passed; do sleep 2; done'"),
        _row(202, 200, f"python.exe {WT}" + "\\helper.py"),  # grandchild
        # sibling worker with a shared name prefix
        _row(300, 110, f"python -m pytest {SIB}/tests/test_y.py"),
        _row(301, 300, f"python {SIB}/x.py"),
        # detached jobs next to it, naming the worktree
        _row(400, 1, f"python -m tools.agentops.detach run --name j -- python scripts/jobs.py submit {WT}/job.json"),
        _row(401, 400, f"python scripts/jobs.py submit {WT}/job.json"),
        _row(410, 1, f"python -m tools.agentops.merge {WT}"),
        _row(420, 1, f"python scripts/benchmark.py --all {WT}/x.json"),
        # unrelated: reaches the live session, not under the worktree
        _row(500, 110, "python other.py"),
    ]


def _run(dry=False):
    killed = []
    out = stop_worker_worktree("wt-871-u1", table=_table, killer=lambda p: (killed.append(p), True)[1],
                               dry_run=dry, log=lambda s: None)
    return killed, out


def test_row1_worker_tree_three_of_three_gone():
    killed, out = _run()
    assert set(killed) == {200, 201, 202}
    assert killed.index(202) < killed.index(200)  # child first
    assert exit_code(out) == 0


def test_row2_sibling_and_detached_jobs_untouched():
    killed, _ = _run()
    for pid in (100, 110, 300, 301, 400, 401, 410, 420, 500):
        assert pid not in killed


def test_dry_run_kills_nothing():
    killed, out = _run(dry=True)
    assert killed == []
    assert {o.pid for o in out if o.action == "dry-run"} == {200, 201, 202}


def test_row4_pid_not_under_worktree_still_refused():
    killed = []
    out = stop_processes([500], table=_table, killer=lambda p: (killed.append(p), True)[1], log=lambda s: None)
    assert killed == [] and out[0].action == "refused"
