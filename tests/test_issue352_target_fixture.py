"""#352 unit 6 (acceptance row 6): a target repo takes the change through ``core update`` with no edit of its own, and a
selection is placed the same way there as in this repo. The target's own ``config/orchestration.json`` carries only its
settings (its repo, the GitHub switch, the test copy, the start-time settings and the keys every settings file needs);
the simulated core update is ``core_settings.load`` on that file, which adds the core's defaults. No repo name, role
name, path or test name of this repo is written here: the start-time values are read from this repo's settings.

Seams: ``test_run.route_selection`` / ``test_run.run_test_run`` and the worker guard's ``run(payload)`` with its real
decision (``GUARD_SEAMS``: settings, clock, a fixed PC line, the GitHub plan injected; ids from the stored durations),
each over a tmp git repo and a tmp durations datafile (``SUITE_DURATIONS_FILE``). No network; the only disk is tmp_path.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import core_settings, merge_github, test_run
from tools.agentops.github_suite import GithubSuiteResult
from tools.agentops.place_decision import Deps
from tools.agentops.util import no_window_flags

HOOKS_DIR = Path(__file__).resolve().parents[1] / "tools" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
import worker_process_guard as guard  # noqa: E402

NOW = 1_000_000.0
BUSY = 1800.0  # a PC line with 30 min of wait ahead of the run
IDLE = 0.0     # an idle PC line
PLAN = merge_github.Plan(remote="https://example.invalid/copy.git", own_repo=False,
                         repo_slug="someone/some-target", repo_name="some-target")

# the stored durations: ten tests per selection, so each selection's expected PC run is known
LONG = {f"tests/x.py::t{i}": 60.0 for i in range(10)}    # 600 s
MEDIUM = {f"tests/m.py::t{i}": 30.0 for i in range(10)}  # 300 s
SHORT = {f"tests/s.py::t{i}": 4.0 for i in range(10)}    # 40 s
DURATIONS = {**LONG, **MEDIUM, **SHORT}


def _home_start() -> dict:
    """The start-time settings as this repo holds them now; the target carries the same two values."""
    cfg = core_settings.load()
    return {key: cfg[key] for key in ("github_start_seconds_linux", "github_start_seconds_windows")}


def _target_settings(bridge: Path, **over) -> dict:
    return {
        "repo": "someone/some-target",
        "brain_chat_title": "Brain: some-target",
        "context_line": 150000,
        "claude_projects_dir": "C--Some-Target-Shop",
        "bridge_dir": str(bridge),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
        "github_suite": True,
        "test_copy_remote": "https://github.com/someone/some-target-copy.git",
        **_home_start(),
        **over,
    }


def _target_repo(tmp_path: Path, **over) -> Path:
    """A target folder (named like a target, not like this repo): a git repo whose only settings file is its own."""
    root = tmp_path / "Some-Target-Shop"
    (root / "tests").mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("x.py", "m.py", "s.py"):
        (root / "tests" / name).write_text("def test_it():\n    pass\n", encoding="utf-8")
    (root / "config" / "orchestration.json").write_text(
        json.dumps(_target_settings(tmp_path / "bridge", **over), indent=2), encoding="utf-8")
    git = ["git", "-C", str(root)]
    for words in (["init", "-q"],
                  ["add", "tests/x.py", "tests/m.py", "tests/s.py", "config/orchestration.json"],
                  ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run([*git, *words], check=True, capture_output=True, creationflags=no_window_flags())
    return root


def _settings_file(root: Path) -> Path:
    return root / "config" / "orchestration.json"


def _durations(path: Path) -> Path:
    """A testmon-shaped datafile: the table ``test_execution`` read by ``suite_plan_plugin.stored_durations``."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE test_execution (test_name TEXT, duration REAL)")
    con.executemany("INSERT INTO test_execution VALUES (?, ?)", list(DURATIONS.items()))
    con.commit()
    con.close()
    return path


def _ids(args: list) -> list:
    """The node ids of one file selection, as the stored durations name them (the collect seam)."""
    return [key for key in DURATIONS if key.split("::", 1)[0] == args[0]]


def _seams(settings_file: Path, wait: float, *, github=None, pc_run=None) -> test_run.Seams:
    """The real decision (stored durations, real git) with the settings read from the target's file through
    ``core_settings.load``, a fixed PC line, the GitHub plan injected; the sending parts are fakes."""
    return test_run.Seams(
        load_settings=lambda: core_settings.load(settings_file),
        clock=lambda: NOW,
        collect=lambda cwd, args, timeout_s: _ids(args),
        deps=Deps(plan_for=lambda: (PLAN, ""), pc_wait_seconds=lambda now: wait, needs_pc=lambda ids: []),
        head=lambda cwd: "abc1234567890",
        pc_only_files=lambda cwd: [],
        github_run=github,
        pc_run=pc_run,
    )


def _payload(root: Path, command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "agent_id": "a", "agent_type": "worker",
            "cwd": str(root)}


@pytest.fixture(autouse=True)
def hook_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / "guard.log"))


@pytest.fixture
def durations(tmp_path, monkeypatch):
    path = _durations(tmp_path / "durations.sqlite")
    monkeypatch.setenv("SUITE_DURATIONS_FILE", str(path))
    return path


# -- the simulated core update: the target's own file plus the core's defaults, nothing else ---------------------------

def test_the_core_update_reads_the_target_settings_and_the_core_defaults(tmp_path, durations):
    root = _target_repo(tmp_path)
    cfg = core_settings.load(_settings_file(root))
    assert cfg["github_suite"] is True
    assert cfg["test_copy_remote"] == "https://github.com/someone/some-target-copy.git"
    assert cfg["github_start_seconds_linux"] == _home_start()["github_start_seconds_linux"]
    assert cfg["place_pc_only_floor_seconds"] == core_settings.DEFAULTS["place_pc_only_floor_seconds"]
    assert cfg["test_run_timeout_minutes"] == core_settings.DEFAULTS["test_run_timeout_minutes"]


# -- row 6: the same routing from the target's settings only --------------------------------------------------------

def test_a_long_selection_goes_to_github_and_a_short_one_to_the_pc_in_a_target(tmp_path, durations):
    root = _target_repo(tmp_path)
    seams = _seams(_settings_file(root), BUSY)
    long_route = test_run.route_selection(["tests/x.py"], root, seams)
    assert long_route.where == "github"
    assert long_route.rest == _ids(["tests/x.py"])
    short_route = test_run.route_selection(["tests/s.py"], root, seams)
    assert (short_route.where, short_route.reason) == ("pc", "run 40 s < floor 2 min")


def test_a_busy_line_sends_a_medium_run_to_github_and_an_idle_line_keeps_it_in_a_target(tmp_path, durations):
    root = _target_repo(tmp_path)
    busy = test_run.route_selection(["tests/m.py"], root, _seams(_settings_file(root), BUSY))
    assert busy.where == "github"  # GitHub 5 min 16 s (16 s start + 5 min) < PC 35 min (30 min wait + 5 min)
    idle = test_run.route_selection(["tests/m.py"], root, _seams(_settings_file(root), IDLE))
    assert idle.where == "pc"      # PC 5 min <= GitHub 5 min 16 s: the start time does not pay on an idle line
    long_idle = test_run.route_selection(["tests/x.py"], root, _seams(_settings_file(root), IDLE))
    assert (long_idle.where, long_idle.pc_ids) == ("pc", [])  # 600 s on the PC against 616 s on GitHub at ratio 1.0


def test_the_guard_refuses_a_bare_pytest_of_the_long_selection_in_a_target(tmp_path, durations, monkeypatch):
    root = _target_repo(tmp_path)
    monkeypatch.setattr(guard, "GUARD_SEAMS", _seams(_settings_file(root), BUSY), raising=False)
    out = guard.run(_payload(root, "python -m pytest tests/x.py"))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("process guard:")
    assert "python -m tools.agentops test run tests/x.py" in reason


def test_the_guard_allows_a_short_pytest_in_a_target(tmp_path, durations, monkeypatch):
    root = _target_repo(tmp_path)
    monkeypatch.setattr(guard, "GUARD_SEAMS", _seams(_settings_file(root), BUSY), raising=False)
    assert guard.run(_payload(root, "python -m pytest tests/s.py")) is None


def test_test_run_takes_the_long_selection_to_github_and_prints_it_in_a_target(tmp_path, durations, capsys):
    root = _target_repo(tmp_path)
    sent, ran = [], []

    def fake_github(plan, **kw):
        sent.append(list(kw["ids"]))
        return GithubSuiteResult(status="green")

    def pc(args, cwd):
        ran.append(list(args))
        return 0

    rc = test_run.run_test_run(["tests/x.py"], cwd=root, seams=_seams(_settings_file(root), BUSY, github=fake_github, pc_run=pc))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0].startswith("test run: GitHub (GitHub ")
    assert sent == [_ids(["tests/x.py"])]
    assert ran == []


def test_test_run_takes_the_short_selection_to_the_pc_in_a_target(tmp_path, durations, capsys):
    root = _target_repo(tmp_path)
    ran = []

    def pc(args, cwd):
        ran.append(list(args))
        return 0

    def no_github(plan, **kw):
        raise AssertionError("a short run never goes to GitHub")

    rc = test_run.run_test_run(["tests/s.py"], cwd=root, seams=_seams(_settings_file(root), BUSY, github=no_github, pc_run=pc))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert ran == [["tests/s.py"]]
    assert out[0] == "test run: PC (run 40 s < floor 2 min)"


def test_the_target_places_every_selection_like_this_repo(tmp_path, durations):
    """The same selections, the same PC line, the repo's own settings file (GitHub switched on, as the target's is):
    the same place and the same numbers in both."""
    root = _target_repo(tmp_path)
    home = {**core_settings.load(), "github_suite": True}
    for args in (["tests/x.py"], ["tests/m.py"], ["tests/s.py"]):
        for wait in (IDLE, BUSY):
            mine = test_run.route_selection(args, root, _seams(_settings_file(root), wait))
            seams = _seams(_settings_file(root), wait)
            seams.load_settings = lambda: home
            theirs = test_run.route_selection(args, root, seams)
            assert (mine.where, mine.reason, mine.rest) == (theirs.where, theirs.reason, theirs.rest), (args, wait)


# -- negative control: a target whose GitHub switch is off runs every selection on the PC -----------------------------

def test_a_target_with_github_suite_off_runs_every_selection_on_the_pc(tmp_path, durations, monkeypatch):
    root = _target_repo(tmp_path, github_suite=False)
    for args in (["tests/x.py"], ["tests/m.py"], ["tests/s.py"]):
        route = test_run.route_selection(args, root, _seams(_settings_file(root), BUSY))
        assert (route.where, route.reason) == ("pc", "github_suite off"), args
    monkeypatch.setattr(guard, "GUARD_SEAMS", _seams(_settings_file(root), BUSY), raising=False)
    assert guard.run(_payload(root, "python -m pytest tests/x.py")) is None
