"""Issue #31 unit 1 (target-add-cmd): `target add` runs the deployment in 7 printed steps.

Acceptance rows 1-4 of #31 plus the review fixes: the folder is stored resolved in forward slashes, step 5
reports `queued`, a home config with uncommitted changes stops step 5, a clash at step 3 names its own route,
and step 7 stops on a last line that is not `live pid <n> commit <sha>`.
The port's checks are mocked the way `tests/test_issue11_port_*.py` mock the port; `dispatch --ensure`,
`core status` and `agentops commit` are mocked, never started. The target is a real scratch git repo (git only).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import port, target_add

TITLE = "привет"
REPO_URL = "https://github.com/owner/third-finances.git"
ROUTE_TEXT = "a stop here is a core issue"
CLASH_TEXT = "a clash is ORCH's to report: the deployment ticket goes needs-info to the brain, no --replace"


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"targets": []}), encoding="utf-8")
    return root


@pytest.fixture
def target(tmp_path):
    folder = tmp_path / "third"
    folder.mkdir()
    _git(folder, "init", "-q")
    _git(folder, "remote", "add", "origin", REPO_URL)
    return folder


@pytest.fixture
def port_checks(monkeypatch):
    """The port's checks mocked: a ready machine and a dry run that lists two files and one label."""
    calls = {"run_port": 0}
    monkeypatch.setattr(port, "check_machine_ready", lambda *a, **k: "mattpocock-skills@mattpocock")
    monkeypatch.setattr(port, "plan_port", lambda *a, **k: (["CLAUDE.md", "tools/agentops/port.py"], ["bug"]))

    def fake_run_port(*a, **k):
        calls["run_port"] += 1
        return port.PortReport(copied=["CLAUDE.md"], own_files_written=["config/orchestration.json"],
                               labels_created=["bug"])

    monkeypatch.setattr(port, "run_port", fake_run_port)
    return calls


def _home_bytes(home: Path) -> bytes:
    return (home / "config" / "orchestration.json").read_bytes()


TWO_TARGETS = (
    '{\n'
    '  "targets": [\n'
    '    {\n'
    '      "repo": "owner/Target-Repo",\n'
    '      "folder": "D:/Work/target-repo-v2"\n'
    '    },\n'
    '    {\n'
    '      "repo": "owner/other-repo",\n'
    '      "folder": "D:/Work/3x/3x - Other dashboard"\n'
    '    }\n'
    '  ],\n'
    '  "update_rule": "home"\n'
    '}\n'
)
THREE_TARGETS = (
    '{\n'
    '  "targets": [\n'
    '    {\n'
    '      "repo": "owner/Target-Repo",\n'
    '      "folder": "D:/Work/target-repo-v2"\n'
    '    },\n'
    '    {\n'
    '      "repo": "owner/other-repo",\n'
    '      "folder": "D:/Work/3x/3x - Other dashboard"\n'
    '    },\n'
    '    {\n'
    '      "repo": "owner/third-finances",\n'
    '      "folder": "{folder}"\n'
    '    }\n'
    '  ],\n'
    '  "update_rule": "home"\n'
    '}\n'
)


def _resolved(folder) -> str:
    """The folder as the file holds it: resolved, forward slashes (the command's own rule)."""
    return Path(folder).resolve().as_posix()


def _three(target) -> str:
    """The expected three-entry home text; the folder is JSON-escaped the way the file holds it."""
    return THREE_TARGETS.replace('"{folder}"', json.dumps(_resolved(target)))


def _commit_ok(calls: list):
    """The `agentops commit --detach` call, mocked: records what it was asked and reports a result path."""
    def runner(home_root, message, rel):
        calls.append((Path(home_root), message, rel))
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="detached: result C:/x/result.json pid 77\n", stderr="")
    return runner


def _clean_status(home_root):
    """`git status --porcelain` of the home's config file, mocked: nothing uncommitted."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _status_reader(folder):
    return SimpleNamespace(changes=0)


def _dispatch(stdout: str):
    def runner(cwd):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    return runner


def test_row1_dry_run_prints_seven_steps_and_the_owner_list(home, target, port_checks):
    before = _home_bytes(home)

    rc, lines = target_add.run_target_add(target, TITLE, dry_run=True, home_root=home, settings_path=home / "settings.json")

    assert rc == 0
    step_lines = [line for line in lines if line.startswith("step ")]
    assert [line.split(":")[0] for line in step_lines] == [
        "step 1 target repo", "step 2 machine ready", "step 3 port dry run", "step 4 port",
        "step 5 target-entry", "step 6 core status", "step 7 dispatch",
    ]
    assert "skipped" in step_lines[3]          # step 4: the real port does not run under --dry-run
    assert "would add" in step_lines[4]        # step 5: the entry is only named, not committed
    owner = [line for line in lines if not line.startswith("step ")]
    assert len([line for line in owner if line.lstrip().startswith(("1.", "2.", "3."))]) == 3
    assert port_checks["run_port"] == 0
    assert _home_bytes(home) == before         # the home config is unchanged


def test_row2_real_run_appends_the_entry_and_queues_its_commit_through_agentops(home, target, port_checks,
                                                                               monkeypatch):
    """Row 2: the home's `targets` gets a third entry, byte for byte as the literal below, through the
    agentops commit (mocked here); the target's config names the brain chat title the port wrote."""
    (home / "config" / "orchestration.json").write_text(TWO_TARGETS, encoding="utf-8")

    def fake_run_port(folder, repo, **kwargs):
        port_checks["run_port"] += 1
        (Path(folder) / "config").mkdir(parents=True, exist_ok=True)
        (Path(folder) / "config" / "orchestration.json").write_text(
            json.dumps({"brain_chat_title": kwargs["brain_title"]}), encoding="utf-8")
        return port.PortReport(copied=["CLAUDE.md"], own_files_written=["config/orchestration.json"],
                               labels_created=["bug"])

    monkeypatch.setattr(port, "run_port", fake_run_port)
    commits: list = []

    rc, lines = target_add.run_target_add(
        target, TITLE, dry_run=False, home_root=home, settings_path=home / "settings.json", commit_runner=_commit_ok(commits),
        home_status_runner=_clean_status, status_reader=_status_reader,
        dispatch_runner=_dispatch("live pid 42 commit abcdef0123456789\n"))

    assert rc == 0
    step5 = next(line for line in lines if line.startswith("step 5 "))
    assert step5 == "step 5 target-entry: queued (result C:/x/result.json)"
    assert (home / "config" / "orchestration.json").read_text(encoding="utf-8") == _three(target)
    stored = json.loads((home / "config" / "orchestration.json").read_text(encoding="utf-8"))["targets"][2]
    assert stored == {"repo": "owner/third-finances", "folder": _resolved(target)}
    assert "\\" not in stored["folder"]
    assert commits == [(home, "target add: owner/third-finances", "config/orchestration.json")]
    assert json.loads((target / "config" / "orchestration.json").read_text(encoding="utf-8"))[
        "brain_chat_title"] == TITLE
    assert lines[-4] == "owner's list:"


def test_step5_skips_a_repo_already_listed_and_commits_nothing(home, target, port_checks):
    (home / "config" / "orchestration.json").write_text(_three(target), encoding="utf-8")
    before = _home_bytes(home)
    commits: list = []

    rc, lines = target_add.run_target_add(
        target, TITLE, dry_run=False, home_root=home, settings_path=home / "settings.json", commit_runner=_commit_ok(commits),
        home_status_runner=_clean_status, status_reader=_status_reader,
        dispatch_runner=_dispatch("live pid 42 commit abcdef0123456789\n"))

    assert rc == 0
    assert "step 5 target-entry: already listed" in lines
    assert commits == []
    assert _home_bytes(home) == before


def test_step5_refuses_when_the_home_config_has_uncommitted_changes(home, target, port_checks):
    (home / "config" / "orchestration.json").write_text(TWO_TARGETS, encoding="utf-8")
    before = _home_bytes(home)
    commits: list = []

    def dirty(home_root):
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout=" M config/orchestration.json\n", stderr="")

    rc, lines = target_add.run_target_add(
        target, TITLE, dry_run=False, home_root=home, settings_path=home / "settings.json", commit_runner=_commit_ok(commits),
        home_status_runner=dirty, status_reader=_status_reader,
        dispatch_runner=_dispatch("live pid 42 commit abcdef0123456789\n"))

    assert rc == 1
    assert lines[-1].startswith("step 5 target-entry: stopped: the home's config/orchestration.json has "
                                "uncommitted changes")
    assert ROUTE_TEXT in lines[-1]
    assert commits == []
    assert _home_bytes(home) == before


def test_row3_folder_without_origin_stops_at_step_1(home, tmp_path, port_checks, monkeypatch):
    folder = tmp_path / "no-origin"
    folder.mkdir()
    _git(folder, "init", "-q")
    before = _home_bytes(home)
    monkeypatch.setattr(port, "check_machine_ready", lambda *a, **k: pytest.fail("machine check must not run"))

    rc, lines = target_add.run_target_add(folder, TITLE, dry_run=False, home_root=home, settings_path=home / "settings.json")

    assert rc == 1
    assert len(lines) == 1
    assert lines[0].startswith("step 1 target repo: stopped:")
    assert str(folder) in lines[0]
    assert ROUTE_TEXT in lines[0]
    assert port_checks["run_port"] == 0
    assert _home_bytes(home) == before


def test_row4_machine_check_failure_stops_at_step_2_with_its_text(home, target, port_checks, monkeypatch):
    def failing(*a, **k):
        raise port.PortError("gh --version failed: is gh installed?")

    monkeypatch.setattr(port, "check_machine_ready", failing)
    before = _home_bytes(home)

    rc, lines = target_add.run_target_add(target, TITLE, dry_run=True, home_root=home, settings_path=home / "settings.json")

    assert rc == 1
    assert lines[-1].startswith("step 2 machine ready: stopped:")
    assert "gh --version failed: is gh installed?" in lines[-1]
    assert ROUTE_TEXT in lines[-1]
    assert not any(line.startswith("step 3") for line in lines)
    assert _home_bytes(home) == before


def test_step3_clash_stops_with_the_needs_info_route_not_the_core_issue_route(home, target, port_checks, monkeypatch):
    def clash(*a, **k):
        raise port.PortError("the target already holds these manifest files with other content "
                             "(use --replace): CLAUDE.md")

    monkeypatch.setattr(port, "plan_port", clash)

    rc, lines = target_add.run_target_add(target, TITLE, dry_run=True, home_root=home, settings_path=home / "settings.json")

    assert rc == 1
    assert lines[-1].startswith("step 3 port dry run: stopped: the target already holds these manifest files")
    assert lines[-1].endswith(CLASH_TEXT)
    assert ROUTE_TEXT not in lines[-1]


def test_step3_other_port_error_keeps_the_core_issue_route(home, target, port_checks, monkeypatch):
    def other(*a, **k):
        raise port.PortError("gh label list failed: network down")

    monkeypatch.setattr(port, "plan_port", other)

    rc, lines = target_add.run_target_add(target, TITLE, dry_run=True, home_root=home, settings_path=home / "settings.json")

    assert rc == 1
    assert lines[-1].startswith("step 3 port dry run: stopped: gh label list failed")
    assert ROUTE_TEXT in lines[-1]
    assert CLASH_TEXT not in lines[-1]


def test_step7_stops_when_the_last_line_is_not_live_pid(home, target, port_checks):
    (home / "config" / "orchestration.json").write_text(_three(target), encoding="utf-8")

    rc, lines = target_add.run_target_add(
        target, TITLE, dry_run=False, home_root=home, settings_path=home / "settings.json", home_status_runner=_clean_status,
        status_reader=_status_reader, dispatch_runner=_dispatch("dispatcher exploded\n"))

    assert rc == 1
    assert lines[-1].startswith("step 7 dispatch: stopped: dispatch --ensure's last line is not "
                                "`live|started pid <n> [commit <sha>]`: dispatcher exploded")
    assert ROUTE_TEXT in lines[-1]


def test_step7_names_the_commit_in_eight_characters(target):
    """Row 7 of the spec: the step line reads `live pid <pid> commit <sha8>`, the dispatcher's full sha cut to 8."""
    assert target_add._ensure_in_target(
        target, dispatch_runner=_dispatch("live pid 42 commit abcdef0123456789\n")) == "live pid 42 commit abcdef01"


def test_step7_accepts_a_fresh_start_without_a_commit(tmp_path):
    """`dispatch --ensure` prints `started pid <n>` when it launched the dispatcher itself: that is a pass."""
    assert target_add._ensure_in_target(tmp_path, dispatch_runner=_dispatch("started pid 42\n")) == "started pid 42"
