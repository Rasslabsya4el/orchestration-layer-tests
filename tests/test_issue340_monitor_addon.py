"""#340 unit 8: the target add-on read through the monitor (`addon:<name>`).

Seams under test: `monitor_repos.addon_name(mode)` (the one name check: `addon:<name>`, ^[a-z0-9_-]{1,40}$, never
`serve`); `monitor_repos.read(entry, "addon:<name>")` (argv `[python, <folder>/mods/<source>/bin/collect.py, <name>]`,
no shell, the same subprocess path as a target read, the home repo included, timeout ADDON_TIMEOUT_S); the server's
`GET /api/addon:<name>` (a bad name is 404 and starts nothing; the cadence is fast's; a failed read is the error
envelope with HTTP 200); and the collector's own `main()` answering `{"error": "unknown mode <name>"}` for a name it
does not know (today a KeyError). No real collector runs except the one `collect.py nosuchmode` call, which answers
the unknown-mode line without reading any data.

    python -m pytest -q -p no:faulthandler tests/test_issue340_monitor_addon.py
"""
from __future__ import annotations

import functools
import http.client
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tools.agentops import core_settings, monitor_repos, monitor_server

ROOT = Path(__file__).resolve().parents[1]
#: the monitor's add-on folder, as this repo's own settings name it (monitor.source)
SOURCE = json.loads(core_settings.SETTINGS_PATH.read_text(encoding="utf-8"))["monitor"]["source"]
COLLECTOR = ROOT / "mods" / SOURCE / "bin" / "collect.py"
HOME_NAME = "owner/home-repo"
OTHER_NAME = "owner/other-repo"
SETTINGS = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(ROOT / "other-checkout")}]}


class _Runner:
    """A fake subprocess runner: each call is recorded (args, cwd, timeout, env); outcomes come in turn."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list, str, float, dict]] = []

    def __call__(self, args, cwd, timeout, env):
        self.calls.append((list(args), str(cwd), timeout, dict(env)))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _done(stdout: str | bytes = b"", rc: int = 0, stderr: str | bytes = b""):
    stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _checkout(root: Path, source: str = SOURCE) -> Path:
    """A fake checkout: its config names the monitor source, and the source's collector file exists (never run)."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": {"source": source}}), encoding="utf-8")
    bin_dir = root / "mods" / source / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "collect.py").write_text("# stub: the runner is fake, this file is never run\n", encoding="utf-8")
    return root


def _entry(folder: Path, name: str = OTHER_NAME) -> dict:
    return monitor_repos.repos({"repo": HOME_NAME, "targets": [{"repo": name, "folder": str(folder)}]},
                               home_folder=folder)[1]


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _own_github(monkeypatch, tmp_path):
    """The server's `github` refresher reads the project's merge log and results: this test's folder, not the PC's (#367)."""
    from tools.agentops import monitor_github
    monkeypatch.setattr(monitor_github, "collect", functools.partial(monitor_github.collect, root=tmp_path))


@pytest.fixture
def serving(tmp_path, monkeypatch):
    started = []
    _own_github(monkeypatch, tmp_path)

    def start(settings, runner, refresh=None):
        folder = tmp_path / "monitor"
        folder.mkdir(exist_ok=True)
        collector = SimpleCollector()
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


class SimpleCollector:
    """A stand-in for the home collector module: its own modes answer a fixed value (the addon never reads them)."""

    def fast(self):
        return {"home": True}


# ---- addon_name(mode): the one check of the name --------------------------------------------------

def test_only_fast_is_an_allowed_addon_mode():
    assert monitor_repos.ADDON_MODES == ("fast",)


@pytest.mark.parametrize("mode, name", [("addon:fast", "fast")])
def test_the_allowed_addon_mode_gives_its_name(mode, name):
    assert monitor_repos.addon_name(mode) == name


@pytest.mark.parametrize("mode", [
    "addon:slow",
    "addon:build",
    "addon:a1",
    "addon:a-b_9",
    "addon:0",
    "addon:" + "x" * 40,
    "addon:../x",
    "addon:a b",
    "addon:a/b",
    "addon:a\\b",
    "addon:C:evil",
    "addon:serve",
    "addon:",
    "addon:" + "x" * 41,
    "addon:Fast",
    "addon:fast\n",
    "addon:fast;rm",
    "addon:..",
    "fast",
    "serve",
    "tickets",
    "",
    None,
])
def test_anything_else_is_not_an_addon_name(mode):
    assert monitor_repos.addon_name(mode) is None


def test_the_timeout_constant_is_a_positive_bound():
    assert 0 < monitor_repos.ADDON_TIMEOUT_S <= 600


# ---- read(): the add-on through the checkout's own collector, same subprocess path ---------------

def test_read_of_an_addon_runs_the_collector_with_the_name_as_its_one_argument(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout='{"runs": {}}'))
    data = monitor_repos.read(_entry(folder), "addon:fast", runner=runner)
    assert data == {"runs": {}}
    args, cwd, timeout, env = runner.calls[0]
    assert args == [sys.executable, str(folder / "mods" / SOURCE / "bin" / "collect.py"), "fast"]
    assert cwd == str(folder)
    assert timeout == monitor_repos.ADDON_TIMEOUT_S
    assert env["PYTHONUTF8"] == "1"


def test_the_home_repo_reads_its_addon_through_the_same_path(tmp_path):
    folder = _checkout(tmp_path / "home")
    home = monitor_repos.repos({"repo": HOME_NAME}, home_folder=folder)[0]
    runner = _Runner(_done(stdout="{}"))
    monitor_repos.read(home, "addon:fast", runner=runner)
    assert runner.calls[0][0] == [sys.executable, str(folder / "mods" / SOURCE / "bin" / "collect.py"), "fast"]
    assert runner.calls[0][1] == str(folder)


def test_an_addon_name_that_is_not_allowed_starts_no_process(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(stdout="{}"))
    for mode in ("addon:../x", "addon:serve", "addon:"):
        with pytest.raises(monitor_repos.RepoError):
            monitor_repos.read(_entry(folder), mode, runner=runner)
    assert runner.calls == []


def test_a_failed_addon_read_names_the_repo_in_the_error(tmp_path):
    folder = _checkout(tmp_path / "checkout")
    runner = _Runner(_done(rc=1, stderr="KeyError: 'node'\n"))
    with pytest.raises(monitor_repos.RepoError) as info:
        monitor_repos.read(_entry(folder), "addon:fast", runner=runner)
    assert str(info.value).startswith(f"{OTHER_NAME}: exit 1: ")


# ---- the server: GET /api/addon:<name> ----------------------------------------------------------

def test_unlisted_addon_names_create_no_source_and_no_thread(serving):
    runner = _Runner(_done(stdout="{}"))
    server = serving(SETTINGS, runner)
    before = len(server.refreshers)
    for n in range(20):
        status, _body = _get(server.server_address[1], f"/api/addon:a{n}")
        assert status == 404
    assert len(server.refreshers) == before
    assert server._repo_sources == {}
    assert runner.calls == []


@pytest.mark.parametrize("name", [
    "addon:../x",
    "addon:..%2Fx",
    "addon:a%20b",
    "addon:serve",
    "addon:",
    "addon:" + "x" * 41,
    "addon:FAST",
    "addon:fast%0a",
    "addon:slow",
    "addon:build",
    "addon:a1",
])
def test_a_bad_addon_name_is_404_and_starts_nothing(serving, name):
    runner = _Runner(_done(stdout="{}"))
    server = serving(SETTINGS, runner)
    status, body = _get(server.server_address[1], "/api/" + name)
    assert status == 404
    assert json.loads(body) == {"error": "unknown addon mode"}
    assert runner.calls == []


def test_the_home_repo_addon_answers_the_envelope_with_its_data(serving):
    runner = _Runner(_done(stdout='{"node": {"error": "ssh pending"}}'))
    server = serving(SETTINGS, runner, refresh={"fast": 600})
    status, body = _get(server.server_address[1], "/api/addon:fast")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] == {"node": {"error": "ssh pending"}}
    assert doc["refresh_s"] == 600
    assert isinstance(doc["at_ms"], int)
    assert "error" not in doc
    args, cwd, _timeout, _env = runner.calls[0]
    assert args[-1] == "fast"
    assert cwd == str(core_settings.ROOT)


def test_the_addon_cadence_is_the_fast_cadence(serving):
    runner = _Runner(_done(stdout="{}"))
    server = serving(SETTINGS, runner, refresh={"fast": 600, "tickets": 15})
    port = server.server_address[1]
    for _ in range(3):
        assert _get(port, "/api/addon:fast")[0] == 200
    assert len(runner.calls) == 1  # one process inside the fast cadence, the rest answered from the cache
    assert server.repo_source(server.home_id(), "addon:fast").refresh_s == 600


def test_a_failed_addon_read_is_the_error_envelope_with_200(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(rc=1, stderr="boom"))
    server = serving(settings, runner)
    status, body = _get(server.server_address[1], "/api/addon:fast?repo=other-repo")
    assert status == 200
    doc = json.loads(body)
    assert doc["data"] is None
    assert doc["error"].startswith(f"{OTHER_NAME}: exit 1: boom")
    assert isinstance(doc["at_ms"], int)
    assert doc["refresh_s"] > 0


def test_an_addon_of_another_repo_reads_its_own_checkout(serving, tmp_path):
    folder = _checkout(tmp_path / "checkout")
    settings = {"repo": HOME_NAME, "targets": [{"repo": OTHER_NAME, "folder": str(folder)}]}
    runner = _Runner(_done(stdout='{"status": "build 0/0 rows"}'))
    server = serving(settings, runner)
    status, body = _get(server.server_address[1], "/api/addon:fast?repo=other-repo")
    assert status == 200
    assert json.loads(body)["data"] == {"status": "build 0/0 rows"}
    assert runner.calls[0][1] == str(folder)


def test_an_addon_of_an_unknown_repo_is_404(serving):
    runner = _Runner(_done(stdout="{}"))
    server = serving(SETTINGS, runner)
    assert _get(server.server_address[1], "/api/addon:fast?repo=no-such")[0] == 404
    assert runner.calls == []


# ---- the collector: an unknown mode answers an error line (collect.py main) ------------------------

def test_the_collector_answers_an_unknown_mode_with_an_error_line():
    proc = subprocess.run(
        [sys.executable, str(COLLECTOR), "nosuchmode"], cwd=str(ROOT), capture_output=True, timeout=120,
        env={**_system_env(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-500:]
    assert json.loads(proc.stdout.decode("utf-8")) == {"error": "unknown mode nosuchmode"}


def _system_env() -> dict:
    """The environment a Windows child needs to start the interpreter (SYSTEMROOT); nothing else of the owner's."""
    import os

    return {key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP") if key in os.environ}
