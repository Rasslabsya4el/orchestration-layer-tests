"""#340 unit 1: the monitor's loopback server and its logon task (tools/agentops/monitor_server.py, monitor_boot.py).

Seams under test (the spec's own): a server on an ephemeral port answers `/api/fast` with `{at_ms, refresh_s, data}`;
it binds 127.0.0.1 only (a connect to the LAN address is refused); a path traversal is 404; `/api/tabs` lists the tab
files of a folder; a collector exception shows as `error` with data null, never as empty data; the boot module builds
the expected `schtasks` argv through an injected runner. No real `schtasks` is called, no real collector runs: a fake
collector module stands in for the monitor mod's `bin/collect.py`.

    python -m pytest -q -p no:faulthandler tests/test_issue340_monitor_server.py
"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import cli, core_settings, monitor_boot, monitor_server

ROOT = Path(__file__).resolve().parents[1]
#: the bound on the LAN-address lookup in this file (a hung resolver must not hang the suite)
LOOKUP_TIMEOUT_S = 5


def _fake_collector(**modes):
    """A stand-in for the collector module: each mode is a zero-argument callable."""
    return SimpleNamespace(**modes)


@pytest.fixture
def serving(tmp_path):
    """Start a server on an ephemeral port over `tmp_path/monitor`; stop it and its refreshers after the test."""
    started = []

    def start(collector, static_dir=None, **kwargs):
        folder = static_dir if static_dir is not None else tmp_path / "monitor"
        folder.mkdir(parents=True, exist_ok=True)
        server = monitor_server.make_server(0, collector=collector, static_dir=folder, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()
        server.stop_refreshers()


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


# ---- the API envelope ---------------------------------------------------------------------------

def test_api_fast_answers_with_the_envelope_keys(serving):
    collector = _fake_collector(fast=lambda: {"at": "now", "agents": [], "merge_error": None})
    server = serving(collector)
    status, ctype, body = _get(server.server_address[1], "/api/fast")
    assert status == 200
    assert ctype.startswith("application/json")
    doc = json.loads(body)
    assert set(doc) >= {"at_ms", "refresh_s", "data"}
    assert isinstance(doc["at_ms"], int)
    assert doc["data"]["agents"] == []  # the collector's data, not an empty stand-in
    assert doc["data"]["merge_error"] is None  # a `*_error` key of the collector stays in data as it is


def test_api_github_serves_the_monitor_github_collect(serving, monkeypatch):
    from tools.agentops import monitor_github

    monkeypatch.setattr(monitor_github, "collect", lambda *a, **k: {"merges": [], "marker": 7})
    server = serving(_fake_collector(fast=lambda: {}))
    status, _ctype, body = _get(server.server_address[1], "/api/github")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] == {"merges": [], "marker": 7}
    assert not doc.get("error")


def test_unknown_api_mode_is_404(serving):
    server = serving(_fake_collector(fast=lambda: {}))
    status, _ctype, _body = _get(server.server_address[1], "/api/nope")
    assert status == 404


# ---- a failed read is an error, never empty data ------------------------------------------------

def test_collector_exception_shows_as_error_not_as_empty_data(serving):
    def broken():
        raise RuntimeError("boom in the collector")

    server = serving(_fake_collector(fast=broken))
    status, _ctype, body = _get(server.server_address[1], "/api/fast")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] is None
    assert "boom in the collector" in doc["error"]
    assert isinstance(doc["at_ms"], int)


# ---- the bind: loopback only --------------------------------------------------------------------

def test_server_binds_loopback_only(serving):
    server = serving(_fake_collector(fast=lambda: {}))
    assert server.server_address[0] == "127.0.0.1"


def _lan_address(timeout: float = LOOKUP_TIMEOUT_S) -> str | None:
    """The machine's non-loopback address, looked up in a thread so a hung resolver is bounded by `timeout`."""
    found: list[str] = []

    def lookup():
        try:
            found.append(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    if not found:
        return None
    return None if found[0].startswith("127.") else found[0]


def test_connect_to_the_lan_address_is_refused(serving):
    lan = _lan_address()
    if lan is None:
        pytest.skip("this host has no LAN address besides loopback")
    server = serving(_fake_collector(fast=lambda: {}))
    port = server.server_address[1]
    # refused on a plain LAN adapter; a virtual adapter's address (measured: a CGNAT 100.x on this host) times out
    # instead. Either way the server does not accept on that address, and that is the property under test.
    with pytest.raises(OSError):
        socket.create_connection((lan, port), timeout=2).close()


# ---- static files and traversal -----------------------------------------------------------------

def test_index_is_served_at_root(serving, tmp_path):
    folder = tmp_path / "monitor"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><p>page</p>", encoding="utf-8")
    server = serving(_fake_collector(fast=lambda: {}), static_dir=folder)
    status, ctype, body = _get(server.server_address[1], "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"<p>page</p>" in body


def test_path_traversal_is_404(serving, tmp_path):
    folder = tmp_path / "monitor"
    folder.mkdir()
    (tmp_path / "secret.txt").write_text("not for the page", encoding="utf-8")
    server = serving(_fake_collector(fast=lambda: {}), static_dir=folder)
    port = server.server_address[1]
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/sub/../../secret.txt"):
        status, _ctype, body = _get(port, path)
        assert status == 404, path
        assert b"not for the page" not in body, path


# ---- /api/tabs ----------------------------------------------------------------------------------

def test_api_tabs_lists_the_tab_files_of_the_folder(serving, tmp_path):
    folder = tmp_path / "monitor"
    folder.mkdir()
    for name in ("tab-home.js", "tab-tickets.js", "app.js", "tab-notes.css", "index.html"):
        (folder / name).write_text("// stub", encoding="utf-8")
    server = serving(_fake_collector(fast=lambda: {}), static_dir=folder)
    status, _ctype, body = _get(server.server_address[1], "/api/tabs")
    assert status == 200
    assert json.loads(body) == ["home", "tickets"]


# ---- the boot task: schtasks argv through an injected runner ------------------------------------

class _Recorder:
    """A fake `schtasks`: records each argv, reads the `/XML` file while the call is live (the module deletes it after)."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.xml: dict[int, str] = {}

    def __call__(self, args):
        self.calls.append(list(args))
        if "/XML" in args and args[1] == "/Create":
            self.xml[len(self.calls) - 1] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return subprocess.CompletedProcess(args, self.returncode, stdout="", stderr="")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(monitor_boot, "_is_windows", lambda: True)


def test_task_name_comes_from_the_root_folder(tmp_path):
    assert monitor_boot.task_name(tmp_path) == f"agentops-monitor-{tmp_path.name}"


def test_install_builds_the_create_argv_and_a_logon_only_pythonw_task(windows, tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    runner = _Recorder()
    ok, message = monitor_boot.install(root=tmp_path, runner=runner, python_exe=str(tmp_path / "python.exe"))
    assert ok, message
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    name = monitor_boot.task_name(tmp_path)
    assert argv[:4] == ["schtasks", "/Create", "/TN", name]
    assert argv[4] == "/XML"
    assert argv[-1] == "/F"

    root = ET.fromstring(runner.xml[0])
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    triggers = [child.tag.replace(ns, "") for child in root.find(f"{ns}Triggers")]
    assert triggers == ["LogonTrigger"]  # started at logon, no clock trigger
    exec_ = root.find(f"{ns}Actions/{ns}Exec")
    assert exec_.find(f"{ns}Command").text == f'"{pythonw}"'  # pythonw: no console window
    assert exec_.find(f"{ns}Arguments").text == "-m tools.agentops.monitor_server"
    assert exec_.find(f"{ns}WorkingDirectory").text == str(tmp_path)
    assert root.find(f"{ns}Settings/{ns}Hidden").text == "true"
    assert root.find(f"{ns}Settings/{ns}ExecutionTimeLimit").text == "PT0S"  # a server runs past Task Scheduler's 72 h default


def test_install_refused_by_schtasks_is_reported(windows, tmp_path):
    ok, message = monitor_boot.install(root=tmp_path, runner=_Recorder(returncode=1), python_exe=str(tmp_path / "python.exe"))
    assert ok is False
    assert "schtasks refused" in message


def test_status_and_uninstall_argv(windows, tmp_path):
    name = monitor_boot.task_name(tmp_path)
    runner = _Recorder()
    count, _message = monitor_boot.status(root=tmp_path, runner=runner)
    assert count == 1
    assert runner.calls[-1] == ["schtasks", "/Query", "/TN", name, "/XML"]
    ok, _message = monitor_boot.uninstall(root=tmp_path, runner=runner)
    assert ok
    assert runner.calls[-1] == ["schtasks", "/Delete", "/TN", name, "/F"]


def test_install_is_windows_only(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor_boot, "_is_windows", lambda: False)
    runner = _Recorder()
    ok, message = monitor_boot.install(root=tmp_path, runner=runner)
    assert ok is False
    assert "Windows only" in message
    assert runner.calls == []


# ---- the settings default ------------------------------------------------------------------------

def test_monitor_port_default_is_8750():
    assert core_settings.DEFAULTS["monitor_port"] == 8750


# ---- review fixes (#340 unit 1, second review) --------------------------------------------------

def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _get_with_host(port: int, path: str, host: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# item 3: one lock around every collector call, so no two modes' reads overlap (NOW is module-level)

def test_collector_calls_never_overlap(serving):
    log: list[tuple[str, str]] = []
    log_lock = threading.Lock()

    def reader(name):
        def read():
            with log_lock:
                log.append(("start", name))
            time.sleep(0.03)
            with log_lock:
                log.append(("end", name))
            return {"mode": name}
        return read

    collector = _fake_collector(fast=reader("fast"), tickets=reader("tickets"), slow=reader("slow"), orch=reader("orch"))
    server = serving(collector, refresh={"fast": 0.01, "tickets": 0.01, "slow": 0.01, "orch": 0.01, "github": 600})
    time.sleep(0.6)
    server.stop_refreshers()
    with log_lock:
        events = list(log)
    depth = peak = 0
    for kind, _name in events:
        depth += 1 if kind == "start" else -1
        peak = max(peak, depth)
    assert peak == 1, "two collector reads ran at once"
    assert {name for _kind, name in events} == {"fast", "tickets", "slow", "orch"}


# item 4: a first read that is not in yet is answered within 5 s, with `not collected yet` and data null

def test_first_request_before_any_read_answers_within_5_seconds(serving):
    assert monitor_server.WAIT_FIRST_S == 5
    release = threading.Event()

    def blocked():
        release.wait(30)
        return {}

    try:
        server = serving(_fake_collector(fast=blocked))
        started = time.monotonic()
        status, _ctype, body = _get(server.server_address[1], "/api/fast")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert status == 200
    assert elapsed < 6.5
    doc = json.loads(body)
    assert doc["data"] is None
    assert doc["error"] == "not collected yet"


# item 5: a failed refresh keeps the last good data and sets the error beside it

def test_failed_refresh_keeps_the_last_good_data(serving):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"agents": ["a"]}
        raise RuntimeError("second read failed")

    server = serving(_fake_collector(fast=flaky), refresh={"fast": 0.05})
    port = server.server_address[1]

    def shows_error():
        return json.loads(_get(port, "/api/fast")[2]).get("error") is not None

    assert _wait_for(shows_error)
    doc = json.loads(_get(port, "/api/fast")[2])
    assert doc["data"] == {"agents": ["a"]}
    assert "second read failed" in doc["error"]


# item 6: DNS-rebinding guard: only the loopback names, with or without the port, are served

def test_a_foreign_host_header_is_refused(serving, tmp_path):
    folder = tmp_path / "monitor"
    folder.mkdir()
    (folder / "index.html").write_text("<p>page</p>", encoding="utf-8")
    server = serving(_fake_collector(fast=lambda: {}), static_dir=folder)
    port = server.server_address[1]
    for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1.evil.example:{port}"):
        status, _body = _get_with_host(port, "/api/fast", host)
        assert status == 403, host
    status, body = _get_with_host(port, "/", "evil.example")
    assert status == 403
    assert b"<p>page</p>" not in body


def test_loopback_host_headers_are_served(serving):
    server = serving(_fake_collector(fast=lambda: {"ok": True}))
    port = server.server_address[1]
    for host in (f"127.0.0.1:{port}", "127.0.0.1", f"localhost:{port}", "localhost", f"LOCALHOST:{port}"):
        status, body = _get_with_host(port, "/api/fast", host)
        assert status == 200, host
        assert json.loads(body)["data"] == {"ok": True}


# item 7: explicit content types for the static files

def test_static_files_get_explicit_content_types(serving, tmp_path):
    folder = tmp_path / "monitor"
    folder.mkdir()
    for name in ("shell.js", "mod.mjs", "style.css", "index.html", "data.json"):
        (folder / name).write_text("x", encoding="utf-8")
    server = serving(_fake_collector(fast=lambda: {}), static_dir=folder)
    port = server.server_address[1]
    expected = {
        "shell.js": "text/javascript; charset=utf-8",
        "mod.mjs": "text/javascript; charset=utf-8",
        "style.css": "text/css; charset=utf-8",
        "index.html": "text/html; charset=utf-8",
        "data.json": "application/json",
    }
    for name, ctype in expected.items():
        status, got, _body = _get(port, f"/{name}")
        assert status == 200, name
        assert got == ctype, name


# item 1: every schtasks call is bounded; a timeout is a clear error result, never a traceback

def test_default_runner_bounds_every_schtasks_call(monkeypatch):
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(monitor_boot.subprocess, "run", fake_run)
    monitor_boot._default_runner(["schtasks", "/Query"])
    assert seen["timeout"] == monitor_boot.SCHTASKS_TIMEOUT_S
    assert 0 < monitor_boot.SCHTASKS_TIMEOUT_S <= 120


def test_a_schtasks_timeout_is_a_clear_error(windows, tmp_path):
    def hangs(args):
        raise subprocess.TimeoutExpired(args, monitor_boot.SCHTASKS_TIMEOUT_S)

    ok, message = monitor_boot.install(root=tmp_path, runner=hangs, python_exe=str(tmp_path / "python.exe"))
    assert ok is False
    assert "timed out" in message
    count, message = monitor_boot.status(root=tmp_path, runner=hangs)
    assert count == 0
    assert "timed out" in message
    ok, message = monitor_boot.uninstall(root=tmp_path, runner=hangs)
    assert ok is False
    assert "timed out" in message


def test_lan_lookup_is_bounded(monkeypatch):
    release = threading.Event()

    def hangs(name):
        release.wait(30)
        return "10.0.0.5"

    monkeypatch.setattr(socket, "gethostbyname", hangs)
    started = time.monotonic()
    try:
        assert _lan_address(timeout=0.2) is None
    finally:
        release.set()
    assert time.monotonic() - started < 3


# item 2: the CLI wiring and the manifest

def test_cli_monitor_boot_subcommands_reach_the_module(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(monitor_boot, "install", lambda: (calls.append("install") or True, "installed (fake)"))
    monkeypatch.setattr(monitor_boot, "status", lambda: (calls.append("status") or (1, "1 task (fake)")))
    monkeypatch.setattr(monitor_boot, "uninstall", lambda: (calls.append("uninstall") or (True, "removed (fake)")))
    assert cli.main(["monitor-boot", "install"]) == 0
    assert cli.main(["monitor-boot", "uninstall"]) == 0
    assert cli.main(["monitor-boot", "status"]) in (0, 1)
    assert calls == ["install", "uninstall", "status"]
    assert "installed (fake)" in capsys.readouterr().out


def test_core_manifest_lists_the_new_files():
    lines = {line.strip() for line in (ROOT / "tools/agentops/core_manifest.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")}
    for path in ("tools/agentops/monitor_boot.py", "tools/agentops/monitor_server.py", "tools/agentops/monitor_repos.py",
                 "tests/test_issue340_monitor_server.py", "tests/test_issue340_monitor_repos.py"):
        assert path in lines, path
