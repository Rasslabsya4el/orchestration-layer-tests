"""#340 unit 1: the monitor server's state file (tools/agentops/monitor_server.py: write_state, clear_state, head_commit, main).

Seams under test (the spec's own): write_state writes the four keys with their types, atomically, and leaves no temp
file behind; clear_state removes the file only when its pid is the caller's, and never raises on a missing or an
unreadable file; head_commit gives the 8-character short sha of this worktree and "" for a folder that is no repo;
main() writes the state file once the server is bound (the bound port, its own pid) and removes it when the server
stops. No real collector and no GitHub read run: a fake collector stands in, and the GitHub mode is stubbed.

    python -m pytest -q -p no:faulthandler tests/test_issue340_server_state.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import monitor_server

WORKTREE = Path(__file__).resolve().parents[1]
#: the bound on each wait in this file (a server that never binds must fail the test, not hang it)
WAIT_S = 10
#: the keys the state file carries, and nothing else
STATE_KEYS = {"pid", "checkout", "commit", "port"}


def _fake_collector(**modes):
    """A stand-in for the collector module: each mode is a zero-argument callable."""
    return SimpleNamespace(**modes)


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / "var" / "agentops" / "monitor_server.json"


def _write(path: Path, pid: int) -> None:
    monitor_server.write_state(path, pid=pid, checkout="/work/checkout", commit="0a1b2c3d", port=8750)


# --- write_state ---------------------------------------------------------------------------------------------------

def test_write_state_round_trip_keeps_four_keys_and_types(tmp_path):
    path = _state_path(tmp_path)
    monitor_server.write_state(path, pid=4242, checkout="/work/checkout", commit="0a1b2c3d", port=8750)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"pid": 4242, "checkout": "/work/checkout", "commit": "0a1b2c3d", "port": 8750}
    assert set(data) == STATE_KEYS
    assert type(data["pid"]) is int
    assert type(data["port"]) is int
    assert type(data["checkout"]) is str
    assert type(data["commit"]) is str


def test_write_state_leaves_no_temp_file_and_replaces_atomically(tmp_path, monkeypatch):
    path = _state_path(tmp_path)
    _write(path, pid=1)
    _write(path, pid=2)  # an overwrite: still one file, holding the new content
    assert sorted(p.name for p in path.parent.iterdir()) == ["monitor_server.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 2

    # a failed replace keeps the old file whole and leaves no temp file in its folder
    def broken_replace(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(monitor_server.os, "replace", broken_replace)
    with pytest.raises(OSError):
        _write(path, pid=3)
    assert sorted(p.name for p in path.parent.iterdir()) == ["monitor_server.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 2


# --- clear_state ---------------------------------------------------------------------------------------------------

def test_clear_state_removes_the_file_of_its_own_pid(tmp_path):
    path = _state_path(tmp_path)
    _write(path, pid=1)
    monitor_server.clear_state(path, 1)
    assert not path.exists()


def test_clear_state_keeps_another_pids_file(tmp_path):
    path = _state_path(tmp_path)
    _write(path, pid=2)
    monitor_server.clear_state(path, 1)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 2


def test_clear_state_on_a_missing_file_does_not_raise(tmp_path):
    path = _state_path(tmp_path)
    monitor_server.clear_state(path, 1)  # no raise
    assert not path.exists()


def test_clear_state_on_an_unreadable_file_does_not_raise(tmp_path):
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    monitor_server.clear_state(path, 1)  # no raise, and nothing is deleted
    assert path.read_text(encoding="utf-8") == "{not json"


# --- head_commit ---------------------------------------------------------------------------------------------------

def test_head_commit_of_this_worktree_is_eight_hex_chars():
    commit = monitor_server.head_commit(WORKTREE)
    assert re.fullmatch(r"[0-9a-f]{8}", commit), commit


def test_head_commit_of_a_folder_that_is_no_repo_is_empty(tmp_path):
    assert monitor_server.head_commit(tmp_path) == ""


# --- main() --------------------------------------------------------------------------------------------------------

@pytest.fixture
def run_main(tmp_path, monkeypatch):
    """Run `main()` on an ephemeral port in a thread, with a fake collector, a fixed commit and no GitHub read.

    Returns (state_path, server_box, thread_box); the caller stops the server with `server_box["server"].shutdown()`.
    """
    state_path = _state_path(tmp_path)
    server_box: dict = {}
    thread_box: dict = {}
    fake = _fake_collector(fast=lambda: {"n": 1}, tickets=lambda: [], slow=lambda: {}, orch=lambda: {})

    monkeypatch.setattr(monitor_server, "serve_port", lambda: 0)
    monkeypatch.setattr(monitor_server, "head_commit", lambda *args, **kwargs: "0a1b2c3d")
    monkeypatch.setattr(monitor_server, "_github_compute", lambda: {})

    def make(port):
        server = monitor_server.make_server(port, collector=fake, static_dir=tmp_path / "monitor", refresh={"fast": 60})
        server_box["server"] = server
        return server

    def start(**kwargs):
        def target():
            thread_box["rc"] = monitor_server.main(state_path=state_path, make=make, **kwargs)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread_box["thread"] = thread
        return thread

    start.state_path = state_path
    return start, server_box, thread_box


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + WAIT_S
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"no state file at {path} within {WAIT_S} s")
        time.sleep(0.05)


def _stop(server_box, thread_box) -> None:
    server_box["server"].shutdown()
    thread_box["thread"].join(timeout=WAIT_S)
    assert not thread_box["thread"].is_alive(), "main() did not return after the server stopped"


def test_main_writes_the_bound_port_and_removes_the_file_on_stop(run_main):
    start, server_box, thread_box = run_main
    start()
    _wait_for(start.state_path)
    data = json.loads(start.state_path.read_text(encoding="utf-8"))
    assert set(data) == STATE_KEYS
    assert data["port"] == server_box["server"].server_address[1] and data["port"] > 0
    assert data["pid"] == os.getpid()
    assert data["checkout"] == str(monitor_server.ROOT.resolve())
    assert data["commit"] == "0a1b2c3d"

    _stop(server_box, thread_box)
    assert thread_box["rc"] == 0
    assert not start.state_path.exists()


def test_main_keeps_serving_when_the_state_file_cannot_be_written(run_main, monkeypatch, capsys):
    start, server_box, thread_box = run_main

    def refuse(*args, **kwargs):
        raise OSError("disk refused")

    monkeypatch.setattr(monitor_server, "write_state", refuse)
    start()
    # the server still binds and answers: the state file is the only thing that failed
    deadline = time.monotonic() + WAIT_S
    while "server" not in server_box and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "server" in server_box
    _stop(server_box, thread_box)
    assert thread_box["rc"] == 0
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 1 and "monitor_server" in err_lines[0] and "disk refused" in err_lines[0]
