"""#97 unit 1: the PC card's machine split decides a process's source by its parent chain, not by its name, and
never rescales a role: load no process owns is `unmeasured`. The fixture is shaped like the 17.10 11:20 snapshot."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_FOUND = sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))
assert len(_FOUND) == 1, f"the collector is expected in exactly one mod, found {_FOUND}"
COLLECT = _FOUND[0]
spec = importlib.util.spec_from_file_location("collect97", COLLECT)
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

MB = 2**20
APP_EXE = r"C:\Program Files\WindowsApps\Claude_2.37337.1.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
CLI_EXE = r"C:\Users\user\AppData\Roaming\Claude\claude-code\2.1.295\d2e29494ec83\claude.exe"
ROLES = {"work", "agents", "app", "tests", "you", "unmeasured"}


class Table:
    """A process table plus its command lines, built row by row."""

    def __init__(self) -> None:
        self.procs: dict[int, dict] = {}
        self.cmd: dict[int, str] = {}
        self.pids: dict[str, list[int]] = {}
        self._next = 100

    def add(self, group: str, name: str, ppid: int, rss_mb: int, cmd: str = "", cpu: float = 0.0) -> int:
        pid = self._next
        self._next += 1
        self.procs[pid] = {"ppid": ppid, "name": name, "rss": rss_mb * MB, "cpu": cpu, "created": 1000.0}
        self.cmd[pid] = cmd
        self.pids.setdefault(group, []).append(pid)
        return pid

    def mb(self, *groups: str) -> int:
        return sum(self.procs[p]["rss"] for g in groups for p in self.pids.get(g, [])) // MB


def snapshot() -> Table:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 100)
    app = t.add("app", "Claude.exe", explorer, 300, APP_EXE)
    for _ in range(16):
        t.add("app", "claude.exe", app, 150, f'{APP_EXE} --type=renderer --lang=en-US /prefetch:{_}')
    daemon = t.add("agents", "claude.exe", explorer, 100, f'{CLI_EXE} daemon run --origin transient')
    for i in range(4):
        # the desktop app hosts some sessions, the daemon starts others: both are CLI session processes
        sess = t.add("agents", "claude.exe", app if i < 2 else daemon, 400,
                     f'{CLI_EXE} --bg --agent orch --permission-mode bypassPermissions run pytest in tests {i}')
        t.add("agents", "bash.exe", sess, 20, "bash -c git status")
        t.add("agents", "gh.exe", sess, 30, "gh issue view 97")
        t.add("agents", "python.exe", sess, 60, "python collect.py fast")
        if i == 0:
            py = t.add("tests", "python.exe", sess, 800, r"python.exe -m pytest tests/test_x.py -q")
            t.add("tests", "python.exe", py, 50, "python.exe -c worker")
    jr = t.add("you", "buildcalc.exe", explorer, 100, "buildcalc.exe")
    build = t.add("you", "python.exe", jr, 1250, "python.exe -m buildcalc.headless_build")
    for _ in range(2):
        t.add("you", "python.exe", build, 235, "python.exe -c from multiprocessing.spawn import spawn_main")
    t.add("you", "python.exe", 99999, 60, "python.exe orphan.py")  # parent is gone
    t.add("you", "codex-windows-sandbox-service.exe", 0, 40, "codex-windows-sandbox-service.exe")
    return t


def classify(t: Table, **kw):
    return collect.classify_processes(t.procs, t.cmd, **kw)


def by_role(t: Table, role: dict) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for pid in t.procs:
        out.setdefault(role.get(pid, "you"), set()).add(pid)
    return out


def test_snapshot_roles_follow_the_parent_chain() -> None:
    t = snapshot()
    role, _ = classify(t)
    roles = by_role(t, role)
    assert roles["agents"] == set(t.pids["agents"])
    assert roles["app"] == set(t.pids["app"]) and len(roles["app"]) == 17
    assert roles["tests"] == set(t.pids["tests"])
    # the build chain, the orphan python and the service are the owner's own programs
    assert set(t.pids["you"]) <= roles["you"]
    assert "agents" not in {role.get(p) for p in t.pids["you"]}


def test_load_split_adds_the_raw_rss_and_names_the_unmeasured_rest() -> None:
    t = snapshot()
    role, skip = classify(t)
    used_gb = (t.mb("shell", "app", "agents", "tests", "you") + 3072) / 1024  # 3 GB nobody owns
    out = collect.load_split(t.procs, role, skip, prev={"t": 990.0, "p": {}}, dt=10.0, cores=8, busy_pct=40.0,
                             used_gb=used_gb, work_key="work")
    ram = out["ram"]
    assert set(ram) == ROLES
    assert ram["agents"] == pytest.approx(t.mb("agents") / 1024, abs=0.05)  # no rescale into the role
    assert ram["app"] == pytest.approx(t.mb("app") / 1024, abs=0.05)
    assert ram["tests"] == pytest.approx(t.mb("tests") / 1024, abs=0.05)
    assert ram["you"] == pytest.approx(t.mb("shell", "you") / 1024, abs=0.05)
    assert ram["unmeasured"] == pytest.approx(3.0, abs=0.1)
    assert set(out["cpu"]) == ROLES
    assert out["merge"]["ram_mb"] == 0


def test_unmeasured_cpu_is_the_busy_share_no_process_owns() -> None:
    t = snapshot()
    sess = t.pids["agents"][1]
    t.procs[sess]["cpu"] = 8.0  # 8 CPU-seconds in 10 s on 8 cores = 10 % of the machine
    role, skip = classify(t)
    out = collect.load_split(t.procs, role, skip, prev={"t": 990.0, "p": {}}, dt=10.0, cores=8, busy_pct=40.0,
                             used_gb=1.0, work_key="work")
    assert out["cpu"]["agents"] == pytest.approx(10.0, abs=0.1)
    assert out["cpu"]["unmeasured"] == pytest.approx(30.0, abs=0.1)
    assert out["ram"]["unmeasured"] == 0  # used RAM below the measured sum: never negative


def test_python_without_a_session_or_dispatcher_is_not_agents() -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    lone = t.add("you", "python.exe", explorer, 100, "python.exe script.py")
    pw = t.add("you", "pwsh.exe", explorer, 100, "pwsh.exe")
    bash = t.add("you", "bash.exe", explorer, 100, "bash.exe")
    node = t.add("you", "node.exe", explorer, 100, "node.exe server.js")
    pytest_alone = t.add("you", "python.exe", explorer, 100, "python.exe -m pytest tests")
    role, _ = classify(t)
    for pid in (lone, pw, bash, node, pytest_alone):
        assert role.get(pid, "you") == "you"


def test_dispatcher_children_are_agents_and_its_pytest_is_tests() -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    disp = t.add("a", "python.exe", explorer, 80, "python.exe -m tools.agentops dispatch")
    ch = t.add("a", "python.exe", disp, 80, "python.exe helper.py")
    test = t.add("t", "python.exe", disp, 80, "python.exe -m pytest tests -q")
    plug = t.add("t", "claude.exe", disp, 80, f"{CLI_EXE} plugin test mods/x")
    role, _ = classify(t)
    assert (role[disp], role[ch], role[test], role[plug]) == ("agents", "agents", "tests", "tests")


def test_merge_and_work_come_before_tests_and_agents() -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    sess = t.add("a", "claude.exe", explorer, 100, f"{CLI_EXE} --bg --agent orch")
    merge = t.add("m", "python.exe", sess, 80, "python.exe -m tools.agentops merge")
    mtest = t.add("m", "python.exe", merge, 80, "python.exe -m pytest tests")
    cell = t.add("w", "python.exe", sess, 80, "python.exe -m pytest cell")
    wch = t.add("w", "python.exe", cell, 80, "python.exe worker.py")
    prefixed = t.add("w", "BuildHeadless.exe", explorer, 80, "BuildHeadless.exe")
    role, _ = classify(t, merge_pids={merge}, slot_pids={cell}, work_prefix="buildheadless")
    assert (role[merge], role[mtest]) == ("merge", "merge")
    assert (role[cell], role[wch], role[prefixed]) == ("work", "work", "work")
    assert role[sess] == "agents"


def test_a_session_started_under_the_daemon_is_agents_its_other_children_are_work() -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    daemon = t.add("d", "python.exe", explorer, 80, "python.exe runs_daemon.py")
    bench = t.add("w", "python.exe", daemon, 80, "python.exe bench.py")
    helper = t.add("a", "claude.exe", daemon, 80, f"{CLI_EXE} --bg --agent helper")
    helper_child = t.add("a", "bash.exe", helper, 80, "bash.exe")
    role, skip = classify(t, daemon_pid=daemon)
    assert (role[bench], role[helper], role[helper_child]) == ("work", "agents", "agents")
    assert daemon in skip


def test_the_collectors_own_chain_is_skipped_up_to_the_session() -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    sess = t.add("a", "claude.exe", explorer, 100, f"{CLI_EXE} --bg --agent orch")
    sh = t.add("s", "bash.exe", sess, 20, "bash.exe")
    me = t.add("s", "python.exe", sh, 40, "python.exe collect.py fast")
    role, skip = classify(t, own_pid=me)
    assert {sh, me} <= skip and sess not in skip
    assert role[sess] == "agents"


@pytest.mark.parametrize("cmd, is_app", [
    (APP_EXE, True),
    (f"{APP_EXE} --type=gpu-process --field-trial-handle=1", True),
    (f'"{APP_EXE}" --type=utility', True),
    (f"{CLI_EXE} --bg --agent orch --permission-mode bypassPermissions Take every issue --type=x", False),
    (f"{CLI_EXE} agents", False),
    (r"C:\Users\user\.local\bin\claude.exe --resume abc", False),
    (r"C:\Users\user\.local\bin\claude.exe", False),
    ("", False),
])
def test_app_marker_tells_the_desktop_app_from_a_cli_session(cmd: str, is_app: bool) -> None:
    t = Table()
    explorer = t.add("shell", "explorer.exe", 0, 50)
    c = t.add("c", "claude.exe", explorer, 100, cmd)
    role, _ = classify(t)
    assert (role.get(c, "you") == "app") is is_app
    if cmd and not is_app:
        assert role[c] == "agents"  # an interactive CLI session and a `--bg` one are both sessions
