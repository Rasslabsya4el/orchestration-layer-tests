"""#114 unit 1: the collector's own chain and the dispatcher daemon count under `agents` (their load is no longer
`unmeasured`); a desktop app main process whose command line cannot be read is `app` (its image path says so); a test
run whose quoted exe path holds a space is `tests`. Fixtures are shaped like the 17.10 PC snapshot."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_FOUND = sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))
assert len(_FOUND) == 1, f"the collector is expected in exactly one mod, found {_FOUND}"
spec = importlib.util.spec_from_file_location("collect114", _FOUND[0])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

MB = 2**20
APP_EXE = r"C:\Program Files\WindowsApps\Claude_2.37337.1.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
CLI_EXE = r"C:\Users\user\AppData\Roaming\Claude\claude-code\2.1.295\d2e29494ec83\claude.exe"
NO_OWN = -1  # the collector's pid in a fixture: not in the table, so no chain is walked


class Table:
    """A process table plus its command lines and image paths (`exe`, read when a command line cannot be)."""

    def __init__(self) -> None:
        self.procs: dict[int, dict] = {}
        self.cmd: dict[int, str] = {}
        self.exe: dict[int, str] = {}
        self._next = 100

    def add(self, name: str, ppid: int, rss_mb: int, cmd: str = "", cpu: float = 0.0, exe: str = "") -> int:
        pid = self._next
        self._next += 1
        self.procs[pid] = {"ppid": ppid, "name": name, "rss": rss_mb * MB, "cpu": cpu, "created": 1000.0}
        self.cmd[pid] = cmd
        self.exe[pid] = exe
        return pid


def classify(t: Table, **kw):
    kw.setdefault("own_pid", NO_OWN)
    return collect.classify_processes(t.procs, t.cmd, exes=t.exe, **kw)


def split(procs: dict, role: dict, skip: set, used_gb: float):
    return collect.load_split(procs, role, skip, prev={"t": 990.0, "p": {}}, dt=10.0, cores=8, busy_pct=60.0,
                              used_gb=used_gb, work_key="work")


def test_collectors_own_chain_and_the_daemon_count_as_agents() -> None:
    t = Table()
    explorer = t.add("explorer.exe", 0, 100)
    sess = t.add("claude.exe", explorer, 400, f"{CLI_EXE} --bg --agent orch")
    bash = t.add("bash.exe", sess, 20, "bash.exe")
    me = t.add("python.exe", bash, 90, "python.exe collect.py fast", cpu=12.0)
    daemon = t.add("python.exe", explorer, 120, "python.exe -m tools.agentops dispatch", cpu=5.0)
    own = {me, bash, daemon}

    role, skip = classify(t, own_pid=me, daemon_pid=daemon)
    assert role[me] == role[bash] == role[daemon] == "agents"
    assert role[sess] == "agents"

    used_gb = 4.0
    full = split(t.procs, role, skip, used_gb)
    # the same machine without the collector's chain and the daemon: nothing else differs
    rest = {p: v for p, v in t.procs.items() if p not in own}
    rest_role, rest_skip = collect.classify_processes(rest, {p: t.cmd[p] for p in rest}, exes={}, own_pid=NO_OWN)
    without = split(rest, rest_role, rest_skip, used_gb)

    own_gb = sum(t.procs[p]["rss"] for p in own) / 2**30
    own_cpu = sum(t.procs[p]["cpu"] for p in own) / 10.0 / 8 * 100  # percent of the whole machine, dt 10 s, 8 cores
    assert full["ram"]["unmeasured"] == pytest.approx(without["ram"]["unmeasured"] - own_gb, abs=0.1)
    assert full["cpu"]["unmeasured"] == pytest.approx(without["cpu"]["unmeasured"] - own_cpu, abs=0.15)
    assert full["ram"]["agents"] - without["ram"]["agents"] == pytest.approx(own_gb, abs=0.1)
    assert full["cpu"]["agents"] - without["cpu"]["agents"] == pytest.approx(own_cpu, abs=0.15)


def test_app_main_with_an_unreadable_command_line_is_app_by_its_image_path() -> None:
    t = Table()
    explorer = t.add("explorer.exe", 0, 50)
    main = t.add("Claude.exe", explorer, 300, cmd="", exe=APP_EXE)
    renderer = t.add("claude.exe", main, 150, cmd="", exe=APP_EXE)
    cli = t.add("claude.exe", explorer, 100, cmd="", exe=CLI_EXE)

    role, _ = classify(t)
    assert role[main] == "app"
    assert role[renderer] == "app"
    assert role.get(cli) != "app"  # an unreadable CLI session is not the desktop app


@pytest.mark.parametrize("cmd", [
    r'"C:\Program Files\X\python.exe" -m pytest',
    r'"C:\Program Files\X\Scripts\pytest.exe" -q',
])
def test_test_run_whose_quoted_exe_path_holds_a_space_is_tests(cmd: str) -> None:
    assert collect.is_test_cmd(cmd)
    t = Table()
    explorer = t.add("explorer.exe", 0, 50)
    sess = t.add("claude.exe", explorer, 100, f"{CLI_EXE} --bg --agent orch")
    run = t.add("python.exe", sess, 80, cmd)
    role, _ = classify(t)
    assert role[run] == "tests"
