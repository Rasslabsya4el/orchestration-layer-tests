"""#340 unit 10: the monitor page reads four repos, the server reads one (tools/agentops/monitor_repos.py,
monitor_server.py).

Seams under test (the prompt's own): `repos(settings)` gives this repo first, then one entry per target; `read()` runs
the checkout's own collector as a subprocess (cwd = its folder, the mode as argument, the output decoded as strict
UTF-8 and parsed as JSON) and turns each failure kind into an error text that names the repo; a per-mode timeout; mode
`github` spawns nothing for a target; `monitor.source` must be a bare folder name; the server answers
`GET /api/repos`, `GET /api/<mode>?repo=<id>` (cached per cadence), an unknown id with 404, and the home id as before.
The subprocess runner is injected everywhere: no real collector runs, no real checkout is read.

    python -m pytest -q -p no:faulthandler tests/test_issue340_monitor_repos.py
"""
from __future__ import annotations

import functools
import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_settings, monitor_repos, monitor_server

HOME_NAME = "owner/home-repo"
OTHER_NAME = "owner/other-repo"
THIRD_NAME = "owner/third-repo"
OTHER_FOLDER = str(Path(__file__).resolve().parent / "other-checkout")
THIRD_FOLDER = str(Path(__file__).resolve().parent / "third-checkout")
SETTINGS = {
    "repo": HOME_NAME,
    "targets": [
        {"repo": OTHER_NAME, "folder": OTHER_FOLDER},
        {"repo": THIRD_NAME, "folder": THIRD_FOLDER},
    ],
}
#: the per-mode timeouts the review asked for (seconds, machine values)
TIMEOUTS = {"fast": 30, "tickets": 60, "slow": 300, "orch": 300}


class _Runner:
    """A fake subprocess runner: each call is recorded as (args, cwd, timeout, env); the outcomes are returned in turn,
    the last one again after that. An outcome that is an exception is raised."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list, str, float, dict]] = []

    def __call__(self, args, cwd, timeout, env):
        self.calls.append((list(args), str(cwd), timeout, dict(env)))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _done(stdout: str | bytes = b"", rc: int = 0, stderr: str | bytes = b"") -> subprocess.CompletedProcess:
    """A finished child as the runner gives it: its output as bytes (a str is encoded as UTF-8 here)."""
    stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


#: the monitor's add-on folder, as this repo's own settings name it (monitor.source)
SOURCE = json.loads(core_settings.SETTINGS_PATH.read_text(encoding="utf-8"))["monitor"]["source"]


def _checkout(root: Path, source: str | None = SOURCE, collector: bool = True, config: bool = True) -> Path:
    """A fake checkout: a config naming its monitor source, and that source's collector file (never run)."""
    root.mkdir(parents=True, exist_ok=True)
    if config:
        (root / "config").mkdir(exist_ok=True)
        monitor = {"source": source} if source is not None else {}
        (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": monitor}), encoding="utf-8")
    if collector and source is not None:
        bin_dir = root / "mods" / source / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "collect.py").write_text("# stub: the runner is fake, this file is never run\n", encoding="utf-8")
    return root


def _entry(folder: Path, name: str = OTHER_NAME) -> dict:
    settings = {"repo": HOME_NAME, "targets": [{"repo": name, "folder": str(folder)}]}
    return monitor_repos.repos(settings, home_folder=folder)[1]


def _fake_collector(**modes):
    return SimpleNamespace(**modes)


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _own_github(monkeypatch, tmp_path):
    """The server's `github` refresher reads the project's merge log and results: this test's folder, not the PC's (#367)."""
    from tools.agentops import monitor_github
    monkeypatch.setattr(monitor_github, "collect", functools.partial(monitor_github.collect, root=tmp_path))


@pytest.fixture
def serving(tmp_path, monkeypatch):
    """Start a server on an ephemeral port; stop it and its refreshers after the test."""
    started = []
    _own_github(monkeypatch, tmp_path)

    def start(settings, runner, collector=None, refresh=None):
        folder = tmp_path / "monitor"
        folder.mkdir(exist_ok=True)
        if collector is None:
            collector = _fake_collector(fast=lambda: {"home": True})
        server = monitor_server.make_server(0, collector=collector, static_dir=folder, settings=settings,
                                            repo_runner=runner, refresh=refresh)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()
        server.stop_refreshers()


# ---- repos(settings): the list, home first ---------------------------------------------------------

def test_repos_lists_this_repo_first_then_the_targets_in_order():
    listed = monitor_repos.repos(SETTINGS)
    assert [entry["id"] for entry in listed] == ["home-repo", "other-repo", "third-repo"]
    assert [entry["label"] for entry in listed] == [HOME_NAME, OTHER_NAME, THIRD_NAME]
    assert listed[0]["folder"] == str(core_settings.ROOT)  # this repo's folder is the repo root
    assert listed[1]["folder"] == OTHER_FOLDER
    assert listed[2]["folder"] == THIRD_FOLDER


def test_each_repo_entry_has_exactly_id_label_and_folder():
    for entry in monitor_repos.repos(SETTINGS):
        assert set(entry) == {"id", "label", "folder"}


def test_repos_with_no_targets_is_this_repo_alone(tmp_path):
    listed = monitor_repos.repos({"repo": HOME_NAME}, home_folder=tmp_path)
    assert listed == [{"id": "home-repo", "label": HOME_NAME, "folder": str(tmp_path)}]


# ---- read(): the checkout's own collector, as a subprocess --------------------------------------

def test_read_runs_the_checkouts_collector_in_its_folder_and_parses_stdout(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout=json.dumps({"agents": ["a"]})))
    data = monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert data == {"agents": ["a"]}
    script = folder / "mods" / SOURCE / "bin" / "collect.py"
    args, cwd, timeout, _env = runner.calls[0]
    assert args == [sys.executable, str(script), "fast"]
    assert cwd == str(folder)
    assert timeout == TIMEOUTS["fast"]


def test_read_takes_the_monitor_source_from_that_checkouts_config(tmp_path):
    folder = _checkout(tmp_path / "checkout", source="alt-monitor")
    runner = _Runner(_done(stdout="{}"))
    monitor_repos.read(_entry(folder), "tickets", runner=runner)
    script = folder / "mods" / "alt-monitor" / "bin" / "collect.py"
    assert runner.calls[0][0] == [sys.executable, str(script), "tickets"]


def test_a_checkout_without_config_is_not_installed(tmp_path):
    folder = _checkout(tmp_path / "old", config=False, collector=False)
    runner = _Runner(_done(stdout="{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value) == f"monitor not installed in {OTHER_NAME}"
    assert runner.calls == []  # no process starts for a checkout without the monitor


def test_a_config_without_a_monitor_source_is_not_installed(tmp_path):
    folder = _checkout(tmp_path / "old", source=None, collector=False)
    runner = _Runner(_done(stdout="{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value) == f"monitor not installed in {OTHER_NAME}"
    assert runner.calls == []


def test_a_checkout_without_its_collector_file_is_not_installed(tmp_path):
    folder = _checkout(tmp_path / "old", collector=False)
    runner = _Runner(_done(stdout="{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value) == f"monitor not installed in {OTHER_NAME}"
    assert runner.calls == []


def test_a_non_zero_exit_names_the_repo_and_the_stderr_tail(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(rc=3, stderr="traceback line\nValueError: bad row in the ticket file\n"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    text = str(info.value)
    assert text.startswith(f"{OTHER_NAME}: exit 3: ")
    assert "ValueError: bad row in the ticket file" in text


def test_unparseable_stdout_names_the_repo(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout="<html>not json</html>"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value).startswith(f"{OTHER_NAME}: unreadable output")


# ---- review fix 1: a timeout per mode, a human unit in the error text ---------------------------

def test_each_mode_has_its_own_timeout_constant():
    assert monitor_repos.CALL_TIMEOUT_S == TIMEOUTS
    for value in monitor_repos.CALL_TIMEOUT_S.values():
        assert 0 < value <= 600


@pytest.mark.parametrize("mode", sorted(TIMEOUTS))
def test_the_runner_gets_the_timeout_of_its_mode(tmp_path, mode):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout="{}"))
    monitor_repos.read(_entry(folder), mode, runner=runner)
    assert runner.calls[0][2] == TIMEOUTS[mode]


def test_a_timeout_names_the_repo_and_the_limit_in_a_human_unit(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(subprocess.TimeoutExpired(cmd=["python"], timeout=TIMEOUTS["orch"]))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "orch", runner=runner)
    assert str(info.value) == f"{OTHER_NAME}: timed out after 5 min"  # 300 s is not written as a bare count of seconds
    runner = _Runner(subprocess.TimeoutExpired(cmd=["python"], timeout=TIMEOUTS["fast"]))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value) == f"{OTHER_NAME}: timed out after 30 s"


# ---- review fix 2: mode github is not a collector mode: a target spawns nothing ---------------------

def test_github_for_a_target_spawns_nothing(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout="{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "github", runner=runner)
    assert str(info.value) == f"{OTHER_NAME}: github mode is read in the home checkout only"
    assert runner.calls == []


def test_github_for_a_target_spawns_no_real_process_either(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a process was started for mode github")

    monkeypatch.setattr(monitor_repos.subprocess, "run", forbidden)
    folder = _checkout(tmp_path / "checkout")
    with pytest.raises(monitor_repos.RepoError):
        monitor_repos.read(_entry(folder), "github")


# ---- review fix 3: the child is forced to UTF-8; strict decoding, a clear error ----------------------

def test_the_child_env_forces_utf8_and_russian_bytes_decode(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    body = json.dumps({"tickets": ["Тикет: готов"]}, ensure_ascii=False).encode("utf-8")
    runner = _Runner(_done(stdout=body))
    data = monitor_repos.read(_entry(folder), "tickets", runner=runner)
    assert data == {"tickets": ["Тикет: готов"]}
    env = runner.calls[0][3]
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_run_passes_the_merged_env_as_bytes_without_replace(monkeypatch):
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(monitor_repos.subprocess, "run", fake_run)
    monitor_repos.run(["python", "x"], "C:/x", 30, {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    assert seen["env"]["PYTHONUTF8"] == "1"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen.get("text") is not True
    assert seen.get("errors") != "replace"
    assert seen["timeout"] == 30


def test_non_utf8_stdout_is_a_clear_error_not_replaced_text(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout=b"\xff\xfe{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value).startswith(f"{OTHER_NAME}: output is not UTF-8")


def test_non_utf8_stderr_on_a_failed_exit_is_a_clear_error(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(rc=2, stderr=b"\xff\xfe"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value) == f"{OTHER_NAME}: exit 2: stderr is not UTF-8"


# ---- review fix 4: monitor.source is a bare folder name ----------------------------------------

@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "..", ".", "/abs/path", "C:evil", ""])
def test_a_monitor_source_that_is_not_a_bare_folder_name_is_an_error_naming_the_repo(tmp_path, bad):
    folder = _checkout(tmp_path / "checkout", source=bad, collector=False)
    runner = _Runner(_done(stdout="{}"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "fast", runner=runner)
    assert str(info.value).startswith(f"{OTHER_NAME}: monitor.source is not a bare folder name")
    assert runner.calls == []


# ---- the server: /api/repos, ?repo=<id>, 404, the home id -----------------------------------------

def test_api_repos_lists_id_and_label_without_folder(serving):
    server = serving(SETTINGS, _Runner(_done(stdout="{}")))
    status, ctype, body = _get(server.server_address[1], "/api/repos")
    assert status == 200
    assert ctype.startswith("application/json")
    listed = json.loads(body)
    assert listed == [
        {"id": "home-repo", "label": HOME_NAME},
        {"id": "other-repo", "label": OTHER_NAME},
        {"id": "third-repo", "label": THIRD_NAME},
    ]


def test_another_repo_runs_its_collector_once_within_the_cadence(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(stdout=json.dumps({"agents": ["alpha"]})))
    server = serving(settings, runner, refresh={"fast": 600})
    assert runner.calls == []  # the collector of another checkout is read on its first request, not before
    port = server.server_address[1]
    for _ in range(2):
        status, _ctype, body = _get(port, "/api/fast?repo=other-repo")
        assert status == 200
        doc = json.loads(body)
        assert doc["data"] == {"agents": ["alpha"]}
        assert "error" not in doc
    assert len(runner.calls) == 1  # cached: two requests inside one cadence, one process
    args, cwd, _timeout, _env = runner.calls[0]
    assert cwd == str(folder)
    assert args[-1] == "fast"


def test_each_mode_of_another_repo_is_its_own_cached_read(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(stdout='{"n": 1}'))
    server = serving(settings, runner, refresh={"fast": 600, "tickets": 600})
    port = server.server_address[1]
    _get(port, "/api/fast?repo=other-repo")
    _get(port, "/api/tickets?repo=other-repo")
    _get(port, "/api/tickets?repo=other-repo")
    assert sorted(call[0][-1] for call in runner.calls) == ["fast", "tickets"]


def test_github_for_another_repo_is_an_error_and_spawns_nothing(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(stdout="{}"))
    server = serving(settings, runner)
    status, _ctype, body = _get(server.server_address[1], "/api/github?repo=other-repo")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] is None
    assert doc["error"] == f"{OTHER_NAME}: github mode is read in the home checkout only"
    assert runner.calls == []


FAILURES = {
    "not_installed": (None, "monitor not installed in owner/other-repo"),
    "exit": (_done(rc=2, stderr="boom tail"), "owner/other-repo: exit 2: boom tail"),
    "timeout": (subprocess.TimeoutExpired(cmd=["python"], timeout=30), "owner/other-repo: timed out"),
    "unparseable": (_done(stdout="not json"), "owner/other-repo: unreadable output"),
}


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_each_failure_kind_shows_as_error_with_data_null_naming_the_repo(serving, tmp_path, kind):
    outcome, expected = FAILURES[kind]
    folder = _checkout(tmp_path / kind, collector=(kind != "not_installed"))
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(outcome if outcome is not None else _done(stdout="{}"))
    server = serving(settings, runner)
    status, _ctype, body = _get(server.server_address[1], "/api/fast?repo=other-repo")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] is None
    assert doc["error"].startswith(expected)
    assert isinstance(doc["at_ms"], int)
    if kind == "not_installed":
        assert runner.calls == []


def test_a_failed_refresh_of_another_repo_keeps_its_last_good_data(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(stdout='{"agents": ["a"]}'), _done(rc=1, stderr="second read failed"))
    server = serving(settings, runner, refresh={"fast": 0.05})
    port = server.server_address[1]

    def shows_error():
        return "error" in json.loads(_get(port, "/api/fast?repo=other-repo")[2])

    assert _wait_for(shows_error)
    doc = json.loads(_get(port, "/api/fast?repo=other-repo")[2])
    assert doc["data"] == {"agents": ["a"]}
    assert "second read failed" in doc["error"]


def test_an_unknown_repo_id_is_404(serving):
    server = serving(SETTINGS, _Runner(_done(stdout="{}")))
    status, _ctype, _body = _get(server.server_address[1], "/api/fast?repo=no-such-repo")
    assert status == 404


def test_the_home_id_reads_as_before_and_runs_no_process(serving):
    runner = _Runner(_done(stdout="{}"))
    server = serving(SETTINGS, runner)
    port = server.server_address[1]
    for path in ("/api/fast", "/api/fast?repo=home-repo"):
        status, _ctype, body = _get(port, path)
        assert status == 200, path
        assert json.loads(body)["data"] == {"home": True}, path
    assert runner.calls == []
