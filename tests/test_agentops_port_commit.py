"""Issue #39: the port commits the paths it writes, and only those.

Seams (agreed with the spec): ``port.run_port`` on a tmp git repo (row 1), ``port.run_dry_run`` (row 2) and
``port.run_port_command`` on a folder that is not a git repo (row 3). gh, the labels, the machine's plugin check
and the home's core check are mocked as in tests/test_issue1080_pocock_skill_check.py; the monitor's install
folder is redirected by env as in tests/test_issue11_port_writes_plugin_id.py. Every target is a tmp folder.
"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from tools.agentops import port
from tools.agentops.core_check import MONITOR_LITERAL

REPO = Path(__file__).resolve().parents[1]
OWNER_REPO = "owner/target"
PLUGIN = "mattpocock-skills@claude-plugins-official"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return done.stdout


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Fake install root, fake home, and this repo's settings (as in test_issue11_port_writes_plugin_id)."""
    mods = tmp_path / "mods"
    mods.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    this_repo = tmp_path / "this-repo"
    (this_repo / "config").mkdir(parents=True)
    (this_repo / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": MONITOR_LITERAL}}), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(this_repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # the home's core check, the machine's plugin check and the labels are not under test here
    monkeypatch.setattr(port, "check_home_clean", lambda root=None: None)
    monkeypatch.setattr(port, "check_machine_ready", lambda *a, **k: PLUGIN)
    monkeypatch.setattr(port, "sync_labels", lambda repo, gh_runner=None, labels=None: ([], []))
    return mods


@pytest.fixture
def settings(tmp_path) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabledPlugins": {PLUGIN: True}}), encoding="utf-8")
    return path


def _make_target(root: Path, name: str = "target") -> Path:
    """A git repo with one committed file and the remote the port checks for."""
    target = root / name
    target.mkdir(parents=True)
    _git(target, "init", "-q")
    _git(target, "config", "user.name", "owner")
    _git(target, "config", "user.email", "owner@example.com")
    _git(target, "remote", "add", "origin", "https://github.com/owner/target.git")
    (target / "README.md").write_text("readme\n", encoding="utf-8")
    _git(target, "add", "--", "README.md")
    _git(target, "commit", "-q", "-m", "initial")
    return target


def _head_files(target: Path) -> set[str]:
    return set(_git(target, "show", "--name-only", "--format=", "HEAD").split())


def _commit_count(target: Path) -> int:
    return int(_git(target, "rev-list", "--count", "HEAD").strip())


def _run(target: Path, settings: Path):
    return port.run_port(target, OWNER_REPO, settings_path=settings, home_root=REPO)


# -- row 1: one commit of exactly the written paths --------------------------------------------

def test_row1_port_commits_exactly_the_written_paths_and_leaves_the_owners_work(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    (target / "own.py").write_text("v1\n", encoding="utf-8")
    _git(target, "add", "--", "own.py")
    _git(target, "commit", "-q", "-m", "owner's own file")
    (target / "own.py").write_text("v2 owner edit\n", encoding="utf-8")   # modified, unstaged
    (target / "tmp_x.py").write_text("scratch\n", encoding="utf-8")       # untracked
    before = _commit_count(target)

    report = _run(target, settings.parent / "settings.json")

    assert _commit_count(target) == before + 1
    # a directory stub ("docs/adr/") is written as its .gitkeep file, the path git commits
    written = {p + ".gitkeep" if p.endswith("/") else p
               for p in [*report.copied, *report.own_files_written, *report.stubs_written]}
    assert written, "the port wrote nothing"
    assert _head_files(target) == written
    status = _git(target, "status", "--porcelain").splitlines()
    assert " M own.py" in status, status
    assert "?? tmp_x.py" in status, status
    assert _git(target, "diff", "--cached", "--name-only").strip() == ""
    assert report.commit_sha == _git(target, "rev-parse", "HEAD").strip()
    assert len(report.committed) == len(written)


def test_row1_commit_message_names_the_home_and_its_core_version(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    _run(target, settings)
    message = _git(target, "log", "-1", "--format=%s").strip()
    home_commit = _git(REPO, "rev-parse", "HEAD").strip()
    assert message == f"orchestration core ported from {port._home_repo(REPO)} at {home_commit}", message


def test_row1_the_summary_names_the_commit_sha_and_the_count(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    report = _run(target, settings)
    line = report.render()
    assert "\n" not in line
    assert report.commit_sha[:8] in line
    assert f"{len(report.committed)} file" in line


def test_row1_a_staged_file_of_the_owner_stays_staged_and_out_of_the_commit(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    (target / "extra.txt").write_text("owner staged this\n", encoding="utf-8")
    _git(target, "add", "--", "extra.txt")

    _run(target, settings)

    assert "extra.txt" not in _head_files(target)
    assert _git(target, "diff", "--cached", "--name-only").split() == ["extra.txt"]


def test_row1_a_gitignored_written_path_is_reported_and_skipped_without_failing(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    (target / ".gitignore").write_text("CLAUDE.md\n", encoding="utf-8")
    _git(target, "add", "--", ".gitignore")
    _git(target, "commit", "-q", "-m", "ignore CLAUDE.md")

    report = _run(target, settings)

    assert (target / "CLAUDE.md").is_file(), "the ignored file is still written"
    assert "CLAUDE.md" not in _head_files(target)
    assert report.ignored == ["CLAUDE.md"]
    assert "CLAUDE.md" in report.render()
    assert report.commit_sha


def test_row1_a_second_port_with_nothing_new_makes_no_commit_and_does_not_fail(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    _run(target, settings)
    head = _git(target, "rev-parse", "HEAD").strip()

    report = _run(target, settings)

    assert _git(target, "rev-parse", "HEAD").strip() == head
    assert report.commit_sha == ""
    assert report.committed == []


def test_row1_a_failed_commit_ends_the_port_with_one_line_and_the_files_left_written(
        machine, settings, tmp_path, monkeypatch):
    target = _make_target(tmp_path)
    # no identity anywhere: the port's own commit must fail (the identity case of the spec)
    empty_global = tmp_path / "empty.gitconfig"
    empty_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    _git(target, "config", "--unset", "user.name")
    _git(target, "config", "--unset", "user.email")
    _git(target, "config", "user.useConfigOnly", "true")
    head = _git(target, "rev-parse", "HEAD").strip()

    with pytest.raises(port.PortError) as info:
        _run(target, settings)

    assert "git commit" in str(info.value)
    assert "\n" not in str(info.value)
    assert _git(target, "rev-parse", "HEAD").strip() == head
    assert (target / "CLAUDE.md").is_file()
    assert (target / "config" / "orchestration.json").is_file()


# -- row 2: --dry-run writes nothing and commits nothing -----------------------------------------

def test_row2_dry_run_makes_no_commit_and_writes_no_file(machine, settings, tmp_path, monkeypatch):
    target = _make_target(tmp_path)
    (target / "tmp_x.py").write_text("scratch\n", encoding="utf-8")
    head = _git(target, "rev-parse", "HEAD").strip()
    status = _git(target, "status", "--porcelain")
    gh_empty = lambda args: subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    text = port.run_dry_run(target, OWNER_REPO, settings_path=settings, home_root=REPO, gh_runner=gh_empty)

    assert "nothing was written" in text
    assert _git(target, "rev-parse", "HEAD").strip() == head
    assert _git(target, "status", "--porcelain") == status
    assert not (target / "CLAUDE.md").exists()
    assert not (target / "config" / "orchestration.json").exists()


# -- row 3: a folder that is not a git repo ----------------------------------------------------

def test_row3_a_non_git_folder_exits_non_zero_with_one_cause_line(machine, settings, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    args = Namespace(target=plain, repo=OWNER_REPO, brain_title=None, update_rule="word",
                     replace=False, dry_run=False)

    rc = port.run_port_command(args, settings_path=settings)

    out = capsys.readouterr().out.strip().splitlines()
    assert rc != 0
    assert len(out) == 1, out
    assert "not a git repo" in out[0]
    # the spec's "files written" is not met here: the existing target check refuses before any write
    assert not (plain / "CLAUDE.md").exists()


# -- follow-up (ORCH decision): an owner's uncommitted edit to a path the port writes is never committed ---

def test_owner_edited_claude_md_is_left_uncommitted_and_the_rest_committed(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    (target / "CLAUDE.md").write_text("# my repo\n", encoding="utf-8")
    _git(target, "add", "--", "CLAUDE.md")
    _git(target, "commit", "-q", "-m", "owner's CLAUDE.md")
    (target / "CLAUDE.md").write_text("# my repo\n\nowner's uncommitted edit\n", encoding="utf-8")   # not committed

    report = _run(target, settings)

    written = {p + ".gitkeep" if p.endswith("/") else p
               for p in [*report.copied, *report.own_files_written, *report.stubs_written]}
    assert "CLAUDE.md" in written, "the port must write (import line) into CLAUDE.md for this test to mean anything"
    assert report.left_uncommitted == ["CLAUDE.md"]
    assert _head_files(target) == written - {"CLAUDE.md"}
    assert report.commit_sha
    status = _git(target, "status", "--porcelain").splitlines()
    assert " M CLAUDE.md" in status, status
    assert _git(target, "diff", "--cached", "--name-only").strip() == ""
    text = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "owner's uncommitted edit" in text
    assert "@docs/agents/core-block.md" in text
    assert "left uncommitted, your own edits: CLAUDE.md" in report.render()
