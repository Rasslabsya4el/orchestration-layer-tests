"""Issue #357, unit 4: a replay run tree is a sealed standalone repository (`tools/replays/arms.py` `make_run_tree`).

The tree's history ends at the case's commit: no ref but HEAD, no remote, no object newer than the case commit. The world is a
small upstream repo (3 commits, a branch and a tag on the newest), a case repo cloned from it (so it has `origin/main`),
a role repo and a draft dir, all in tmp_path. The gh half: the stub's calls to the real gh carry `-R <REPLAY_REPO>`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools.replays import arms, gh_stub  # noqa: E402

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=60)
    return done.stdout.strip()


def _try(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _commit(repo: Path, rel: str, text: str, message: str) -> str:
    _put(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    role = tmp_path / "role"
    role.mkdir()
    _git(role, "init", "-q")
    _put(role, "docs/agents/brain.md", "old brain\n")
    _put(role, ".claude/agents/brain.md", "old agent\n")
    _put(role, ".claude/settings.json", '{"hooks": {}}')
    _put(role, "tools/hooks/brain_reset_nudge.py",
         'def _reply_check_text(payload, transcript_path):\n    """Issue #65."""\n    return "check"\n')
    _put(role, "tools/hooks/brain_state_inject.py",
         'def _inject():\n    fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)\n'
         '    notes.append(FETCH_FAILED_NOTE)\n')
    _put(role, "tools/agentops/core.py", "core = 'role'\n")
    _git(role, "add", "-A")
    _git(role, "commit", "-qm", "role")
    role_commit = _git(role, "rev-parse", "HEAD")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "CLAUDE.md", "case own part\n", "one")
    _commit(upstream, "docs/STATE.md", "state at moment\n", "two")
    case_commit = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "branch", "feature")
    _git(upstream, "tag", "v1")
    newer = _commit(upstream, "docs/STATE.md", "TODAY: #340 parity\n", "three, newer than the moment")
    case = tmp_path / "case"
    subprocess.run(["git", "clone", "-q", str(upstream), str(case)], check=True, capture_output=True, timeout=60)
    assert _git(case, "rev-parse", "origin/main") == newer

    draft = tmp_path / "draft"
    for name in ("brain.md", "brain-reference.md", "agent.md"):
        _put(draft, name, f"draft {name}\n")
    base = tmp_path / "trees"
    base.mkdir()
    return {"role": role, "role_commit": role_commit, "case": {"repo": str(case), "commit": case_commit, "key": "a"},
            "newer": newer, "draft": draft, "base": base}


@pytest.fixture
def build(world, monkeypatch):
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", world["role_commit"])

    def go(arm: str, n: int = 1) -> Path:
        return arms.make_run_tree(world["case"], arm, n, world["base"], world["role"],
                                  world["draft"] if arm == "new" else None)

    return go


def test_every_object_reachable_in_the_tree_is_the_case_commits_history(world, build):  # (a)
    tree = build("old")
    case_commit = world["case"]["commit"]
    assert sorted(_git(tree, "rev-list", "--all").split()) == sorted(_git(tree, "rev-list", case_commit).split())
    assert len(_git(tree, "rev-list", case_commit).split()) == 2


def test_the_tree_has_no_ref_but_head(world, build):  # (b)
    for arm in ("old", "new"):
        tree = build(arm, 1 if arm == "old" else 2)
        assert _git(tree, "for-each-ref") == ""
        assert not (tree / ".git/packed-refs").exists() or "refs/" not in (tree / ".git/packed-refs").read_text()


def test_the_tree_has_no_remote_and_origin_cannot_be_fetched_or_read(world, build):  # (c) (d)
    tree = build("old")
    assert _git(tree, "remote") == ""
    assert _try(tree, "fetch", "origin").returncode != 0
    assert _try(tree, "show", "origin/main:docs/STATE.md").returncode != 0


def test_an_object_newer_than_the_case_commit_is_not_in_the_tree(world, build):  # (e)
    tree = build("old")
    assert _try(tree, "cat-file", "-e", world["newer"]).returncode != 0
    assert _try(tree, "cat-file", "-e", world["case"]["commit"]).returncode == 0
    assert not (tree / ".git/objects/info/alternates").exists()


def test_head_is_the_case_commit_and_the_tree_is_clean_before_the_overlay(world, build, monkeypatch):  # (f)
    seen = {}
    real_seal = arms._seal_tree

    def spy(source, tree, commit):
        real_seal(source, tree, commit)
        seen["head"] = _git(tree, "rev-parse", "HEAD")
        seen["status"] = _try(tree, "status", "--porcelain").stdout.strip()  # the machine's own autocrlf

    monkeypatch.setattr(arms, "_seal_tree", spy)
    tree = build("old")
    assert seen == {"head": world["case"]["commit"], "status": ""}
    assert _git(tree, "rev-parse", "HEAD") == world["case"]["commit"]
    assert _try(tree, "symbolic-ref", "-q", "HEAD").returncode != 0  # detached
    assert (tree / "docs/STATE.md").read_text() == "state at moment\n"


def test_the_overlay_of_both_arms_is_in_the_sealed_tree(world, build):  # (g)
    old, new = build("old"), build("new", 2)
    assert (old / "docs/agents/brain.md").read_text() == "old brain\n"
    assert (old / ".claude/agents/brain.md").read_text() == "old agent\n"
    assert (new / "docs/agents/brain.md").read_text() == "draft brain.md\n"
    assert (new / "docs/agents/brain-reference.md").read_text() == "draft brain-reference.md\n"
    assert (new / ".claude/agents/brain.md").read_text() == "draft agent.md\n"
    for tree in (old, new):
        assert (tree / "tools/agentops/core.py").read_text() == "core = 'role'\n"
        assert (tree / "CLAUDE.md").read_text() == "case own part\n"
        assert "fetch = None" in (tree / "tools/hooks/brain_state_inject.py").read_text()


def test_the_tree_can_commit_without_a_global_identity(world, build, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nofile"))
    tree = build("old")
    _put(tree, "run.txt", "x\n")
    subprocess.run(["git", "-C", str(tree), "add", "run.txt"], check=True, capture_output=True, timeout=60)
    done = _try(tree, "commit", "-qm", "run commit")
    assert done.returncode == 0, done.stderr


# ---- the stub's calls to the real gh name the case's repo ------------------------------------------------------------

REPO = "acme/repo"


@pytest.mark.parametrize("args", [
    ["issue", "view", "340", "--json", "title"],
    ["issue", "list", "--state", "all"],
    ["pr", "view", "12"],
    ["label", "list"],
    ["run", "list"],
    ["workflow", "view", "ci.yml"],
    ["release", "list"],
])
def test_a_repo_command_without_a_repo_flag_gets_r_repo(args):
    assert gh_stub.pin_repo(args, REPO) == [*args, "-R", REPO]


@pytest.mark.parametrize("args", [
    ["issue", "view", "340", "-R", "other/repo"],
    ["issue", "view", "340", "--repo", "other/repo"],
    ["issue", "view", "340", "--repo=other/repo"],
    ["issue", "list", "-Rother/repo"],
])
def test_a_call_that_names_a_repo_is_left_as_it_is(args):
    assert gh_stub.pin_repo(args, REPO) == args


@pytest.mark.parametrize("args", [
    ["--version"],
    ["auth", "status"],
    ["api", "repos/acme/repo/issues"],
    ["search", "issues", "fix"],
    ["repo", "list", "acme"],
])
def test_commands_that_take_no_repo_flag_are_left_as_they_are(args):
    assert gh_stub.pin_repo(args, REPO) == args


def test_repo_view_without_a_repo_gets_it_as_the_positional():
    assert gh_stub.pin_repo(["repo", "view", "--json", "name"], REPO) == ["repo", "view", REPO, "--json", "name"]
    assert gh_stub.pin_repo(["repo", "view", "x/y"], REPO) == ["repo", "view", "x/y"]


def test_the_real_gh_runs_with_the_repo_pinned_and_gh_repo_in_its_env(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(gh_stub.subprocess, "run", fake_run)
    rc, out, _ = gh_stub.real_gh_runner("real-gh", REPO)(["issue", "view", "340"])
    assert (rc, out) == (0, "ok")
    assert seen["argv"] == ["real-gh", "issue", "view", "340", "-R", REPO]
    assert seen["env"]["GH_REPO"] == REPO
