"""The pre-push hook that refuses a plain push of main (#379).

Temp repos only: a bare origin plus a clone whose tools/githooks/pre-push is a copy of the real hook
and whose core.hooksPath is set through githooks.install. The real repo's config is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.agentops import core_update, githooks, provisional

REAL_ROOT = Path(__file__).resolve().parents[1]
REAL_HOOK = REAL_ROOT / "tools" / "githooks" / "pre-push"
CLOSING_URL = "https://github.com/Rasslabsya4el/orchestration-layer/issues/5"


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "AGENTOPS_MAIN_PUSH"}
    # the hook runs `python -m tools.agentops closing-check` in a temp clone, which has no tools package
    env["PYTHONPATH"] = str(REAL_ROOT)
    env.update(extra)
    return env


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env or _env())


def _origin_main(origin: Path) -> str:
    return _git(origin, "rev-parse", "refs/heads/main").stdout.strip()


@pytest.fixture()
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        _git(clone, "config", key, value)
    hooks = clone / "tools" / "githooks"
    hooks.mkdir(parents=True)
    shutil.copyfile(REAL_HOOK, hooks / "pre-push")
    (clone / "a.txt").write_text("a\n")
    _git(clone, "checkout", "-q", "-B", "main")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "first")
    assert not githooks.check(clone)
    githooks.install(clone)
    return origin, clone


def test_check_before_and_after_install(tmp_path):
    _git(tmp_path, "init", "-q", str(tmp_path / "r"))
    repo = tmp_path / "r"
    assert githooks.check(repo) is False
    assert githooks.install(repo) == ""
    assert githooks.check(repo) is True
    assert _git(repo, "config", "core.hooksPath").stdout.strip() == "tools/githooks"


def test_check_accepts_relative_spelling(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", str(repo))
    _git(repo, "config", "core.hooksPath", "tools/githooks")
    assert githooks.check(repo) is True


@pytest.mark.skipif(os.name != "nt", reason="backslash spelling is a Windows path")
def test_check_accepts_absolute_backslash_spelling(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", str(repo))
    spelled = str(repo / "tools" / "githooks").replace("/", "\\")
    _git(repo, "config", "core.hooksPath", spelled)
    assert githooks.check(repo) is True


def test_check_refuses_a_foreign_folder(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", str(repo))
    foreign = tmp_path / "elsewhere" / "tools" / "githooks"
    _git(repo, "config", "core.hooksPath", str(foreign))
    assert githooks.check(repo) is False


def test_check_refuses_when_unset(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", str(repo))
    assert _git(repo, "config", "--get", "core.hooksPath").returncode != 0
    assert githooks.check(repo) is False


def test_plain_push_of_main_is_refused(repos):
    origin, clone = repos
    result = _git(clone, "push", "origin", "main")
    assert result.returncode != 0
    assert "python -m tools.agentops commit" in result.stderr
    assert _git(origin, "rev-parse", "--verify", "-q", "refs/heads/main").returncode != 0


def test_deleting_main_is_refused(repos):
    origin, clone = repos
    _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    before = _origin_main(origin)
    result = _git(clone, "push", "origin", ":main")
    assert result.returncode != 0
    assert _origin_main(origin) == before


def test_push_of_main_with_env_passes(repos):
    origin, clone = repos
    result = _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    assert result.returncode == 0, result.stderr
    assert _origin_main(origin) == _git(clone, "rev-parse", "HEAD").stdout.strip()


def test_branch_push_passes(repos):
    origin, clone = repos
    _git(clone, "branch", "orch-x")
    result = _git(clone, "push", "origin", "orch-x")
    assert result.returncode == 0, result.stderr


def _commit(clone: Path, message: str, name: str) -> None:
    (clone / name).write_text(message + "\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", message)


def _on_origin(origin: Path, ref: str) -> bool:
    return _git(origin, "rev-parse", "--verify", "-q", ref).returncode == 0


def test_closing_keyword_on_a_branch_is_refused(repos):
    origin, clone = repos
    _git(clone, "checkout", "-q", "-b", "orch-y")
    _commit(clone, "fix #5", "b.txt")
    result = _git(clone, "push", "origin", "orch-y")
    assert result.returncode != 0
    assert "closing-check" in result.stderr and "#5" in result.stderr
    assert not _on_origin(origin, "refs/heads/orch-y")


def test_closing_keyword_on_main_with_env_is_refused(repos):
    origin, clone = repos
    _commit(clone, "fix #5", "b.txt")
    result = _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    assert result.returncode != 0
    assert "closing-check" in result.stderr and "#5" in result.stderr
    assert not _on_origin(origin, "refs/heads/main")


def test_clean_commit_to_main_with_env_passes(repos):
    origin, clone = repos
    _commit(clone, "Merge: rows 1-2 checked, Refs #5", "b.txt")
    result = _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    assert result.returncode == 0, result.stderr
    assert _origin_main(origin) == _git(clone, "rev-parse", "HEAD").stdout.strip()


def test_closing_keyword_after_main_is_on_origin_is_refused(repos):
    origin, clone = repos
    _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    before = _origin_main(origin)
    _commit(clone, "closed #5", "b.txt")
    result = _git(clone, "push", "origin", "main", env=_env(AGENTOPS_MAIN_PUSH="1"))
    assert result.returncode != 0
    assert "#5" in result.stderr
    assert _origin_main(origin) == before


def test_refs_url_and_ticket_line_pass(repos):
    origin, clone = repos
    _git(clone, "checkout", "-q", "-b", "orch-z")
    _commit(clone, f"Refs {CLOSING_URL}\n\nTicket: {CLOSING_URL}", "b.txt")
    result = _git(clone, "push", "origin", "orch-z")
    assert result.returncode == 0, result.stderr
    assert _on_origin(origin, "refs/heads/orch-z")


def test_deleting_a_branch_passes(repos):
    origin, clone = repos
    _git(clone, "branch", "orch-d")
    assert _git(clone, "push", "origin", "orch-d").returncode == 0
    result = _git(clone, "push", "origin", ":orch-d")
    assert result.returncode == 0, result.stderr
    assert not _on_origin(origin, "refs/heads/orch-d")


def test_a_check_that_cannot_run_lets_the_push_through(repos, tmp_path):
    origin, clone = repos
    bin_dir = tmp_path / "shim"
    bin_dir.mkdir()
    shim = bin_dir / "python"
    shim.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    shim.chmod(0o755)
    env = _env()
    path_key = next(k for k in env if k.upper() == "PATH")
    env[path_key] = f"{bin_dir}{os.pathsep}{env[path_key]}"
    _git(clone, "checkout", "-q", "-b", "orch-f")
    _commit(clone, "fix #5", "b.txt")
    result = _git(clone, "push", "origin", "orch-f", env=env)
    assert result.returncode == 0, result.stderr
    assert "warning" in result.stderr
    assert _on_origin(origin, "refs/heads/orch-f")


def test_core_update_push_passes_the_env(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core_update.subprocess, "run", fake_run)
    core_update._git(tmp_path, "push", "origin", "main", env={"AGENTOPS_MAIN_PUSH": "1"})
    assert seen["env"]["AGENTOPS_MAIN_PUSH"] == "1"
    core_update._git(tmp_path, "status")
    assert seen["env"] is None


def test_provisional_push_passes_the_env(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provisional.subprocess, "run", fake_run)
    provisional._run_git(["push", "origin", "main"], cwd=tmp_path, env={"AGENTOPS_MAIN_PUSH": "1"})
    assert seen["env"]["AGENTOPS_MAIN_PUSH"] == "1"


def test_both_call_sites_pass_the_env():
    root = Path(__file__).resolve().parents[1] / "tools" / "agentops"
    assert '"push", "origin", branch, env={"AGENTOPS_MAIN_PUSH": "1"}' in (root / "core_update.py").read_text(encoding="utf-8")
    assert '["push", remote, branch], cwd=repo, env={"AGENTOPS_MAIN_PUSH": "1"}' in (root / "provisional.py").read_text(encoding="utf-8")
