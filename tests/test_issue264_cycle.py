"""#264: the collector's parent walks stop on a cycle.

The cause is INFERRED from the code and the shape of the memory samples; it was not observed (no process-table
snapshot of 20.10 17:3x exists). `classify_processes` walks from the collector's own pid up to a live session with
no visited set and appends every pid to `chain`; a ppid cycle in the scan's snapshot (for instance a dead
ancestor's pid taken again by a child of the collector) makes that walk grow `chain` until memory runs out: about
1 GB per minute in the 17:3x-17:5x samples. Earlier episodes (samples.csv / samples_poe.csv 16:09-16:59 on 20.10)
rose and fell back to ~40 MB: the walk ends in MemoryError, which `machine()`'s caller catches (`except Exception`,
collect.py ~961), so a cycle lasts as long as the reused pid lives. The opencode walk to a session's root
(`opencode()`) has the same missing visited set, but it only burns CPU (it allocates nothing).

No test runs a real endless loop: each table / session counts its own lookups and raises after `LIMIT`.

    python -m pytest -q -p no:faulthandler tests/test_issue264_cycle.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_FOUND = sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))
assert len(_FOUND) == 1, f"the collector is expected in exactly one mod, found {_FOUND}"
spec = importlib.util.spec_from_file_location("collect264", _FOUND[0])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

LIMIT = 10_000
MB = 2**20
CLI_EXE = r"C:\tools\claude-code\1.0.0\claude.exe"


class Runaway(Exception):
    """The code under test looked up more than LIMIT times: it is walking a cycle."""


class CountingTable(dict):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.lookups = 0

    def __getitem__(self, key):
        self.lookups += 1
        if self.lookups > LIMIT:
            raise Runaway(f"{self.lookups} lookups in the process table")
        return super().__getitem__(key)


def proc(name: str, ppid: int) -> dict:
    return {"ppid": ppid, "name": name, "rss": 10 * MB, "cpu": 0.0, "created": 1000.0}


def test_collectors_own_chain_with_a_ppid_cycle_ends() -> None:
    # collector 500 -> cmd 400 -> (dead WMI host, pid 300 reused by the collector's child) 300 -> collector 500
    procs = CountingTable({
        500: proc("pythonw.exe", 400),
        400: proc("cmd.exe", 300),
        300: proc("gh.exe", 500),
    })
    cmds = {500: "pythonw.exe collect.py serve", 400: "cmd /c pythonw.exe collect.py serve", 300: "gh.exe api"}
    role, skip = collect.classify_processes(procs, cmds, own_pid=500)
    assert procs.lookups < LIMIT
    # no live session above the chain: it is nobody's agents (the owner's own `you`), as with any chain without one
    assert not skip & {500, 400, 300}
    assert not role.keys() & {500, 400, 300}


GROWTH_CAP = 1 * MB  # named cap: size after 10,100 calls on a cyclic table <= size after 100 calls + this


def test_cyclic_table_memory_after_10000_more_calls_stays_within_the_cap() -> None:
    import tracemalloc

    def one_call() -> None:
        # a fresh counting table per call: the old code raises Runaway on the first call instead of looping on
        procs = CountingTable({500: proc("pythonw.exe", 400), 400: proc("cmd.exe", 300), 300: proc("gh.exe", 500)})
        collect.classify_processes(procs, {500: "pythonw.exe collect.py serve"}, own_pid=500)

    tracemalloc.start()
    try:
        for _ in range(100):
            one_call()
        after_100 = tracemalloc.get_traced_memory()[0]
        for _ in range(10_000):
            one_call()
        after_10100 = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    assert after_10100 - after_100 <= GROWTH_CAP


def test_acyclic_chain_up_to_a_session_is_unchanged() -> None:
    procs = CountingTable({
        100: proc("explorer.exe", 0),
        110: proc("claude.exe", 100),
        120: proc("bash.exe", 110),
        130: proc("python.exe", 120),
    })
    cmds = {110: f"{CLI_EXE} --bg --agent orch", 120: "bash.exe", 130: "python.exe collect.py fast"}
    role, skip = collect.classify_processes(procs, cmds, own_pid=130)
    assert role[130] == role[120] == role[110] == "agents"
    assert {130, 120} <= skip


def test_acyclic_chain_without_a_session_is_unchanged() -> None:
    procs = CountingTable({100: proc("explorer.exe", 0), 120: proc("bash.exe", 100), 130: proc("python.exe", 120)})
    role, skip = collect.classify_processes(procs, {130: "python.exe collect.py fast"}, own_pid=130)
    assert not skip & {130, 120, 100}
    assert 130 not in role


class Session(dict):
    """An opencode session row that counts how often its parent link is read."""

    reads = 0

    def get(self, key, default=None):
        if key == "parentID":
            Session.reads += 1
            if Session.reads > LIMIT:
                raise Runaway(f"{Session.reads} parent reads")
        return super().get(key, default)


def test_opencode_root_walk_with_a_parent_cycle_ends(tmp_path) -> None:
    """Burns CPU only (the old walk allocated nothing): the counter turns the endless loop into a failure."""
    Session.reads = 0
    now = 1_700_000_000_000
    rows = [Session(id="a", title="a", parentID="b", directory=str(tmp_path), time={"created": now, "updated": now}),
            Session(id="b", title="b", parentID="a", directory=str(tmp_path), time={"created": now, "updated": now})]

    class FakeClient:
        def call(self, method, path, directory=None, timeout=None):
            if path == "/session":
                return rows
            if path == "/session/status":
                return {}
            return []

    out = collect.opencode(client=FakeClient(), registry={}, now_ms=now, launch_cache=tmp_path / "launch.json")
    assert Session.reads < LIMIT
    assert sorted(r["id"] for r in out) == ["a", "b"]


def test_opencode_root_walk_without_a_cycle_finds_the_launcher(tmp_path) -> None:
    Session.reads = 0
    now = 1_700_000_000_000
    rows = [Session(id="a", title="a", directory=str(tmp_path), time={"created": now, "updated": now}),
            Session(id="b", title="b", parentID="a", directory=str(tmp_path), time={"created": now, "updated": now})]

    class FakeClient:
        def call(self, method, path, directory=None, timeout=None):
            return rows if path == "/session" else ({} if path == "/session/status" else [])

    reg = {"a": {"claude_session": "claude-1", "directory": str(tmp_path)}}
    out = collect.opencode(client=FakeClient(), registry=reg, now_ms=now, launch_cache=tmp_path / "launch.json")
    assert {r["id"]: r["claude"] for r in out} == {"a": "claude-1", "b": "claude-1"}
