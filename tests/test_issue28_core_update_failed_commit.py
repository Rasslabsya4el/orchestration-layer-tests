"""Issue 28 unit 1: `core update` whose `git commit` (or push) fails leaves the target exactly as it
found it -- every touched path back to its old bytes, the index clean, `core_version` unchanged --
and raises with text ending `undone, nothing changed`. Real git on a tmp target (bare origin + clone);
the home is a fake `gh` runner, no network."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.home_tree_fake import tree_reply
from tools.agentops import core_update
from tools.agentops.core_update import CoreUpdateError, run_core_update

OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_FILES = {"docs/agents/one.md": "one v1\n", "docs/agents/two.md": "two v1\n"}
NEW_HOME_FILES = {"docs/agents/one.md": "one v2\n", "docs/agents/two.md": "two v1\n",
                  "docs/agents/three.md": "three v2 (new)\n"}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest_text(files: dict[str, str]) -> str:
    return "".join(f"{rel}\n" for rel in [MANIFEST, *files])


def _fake_gh(home_files: dict[str, str]):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = _manifest_text(home_files).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            return SimpleNamespace(returncode=0, stdout=NEW_VERSION + "\n", stderr="")
        if "/git/trees/" in path:  # #148: the home's tree, one call
            return tree_reply(contents)
        rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
        body = base64.b64encode(contents[rel]).decode("ascii")
        return SimpleNamespace(returncode=0, stdout=body + "\n", stderr="")

    return run


def _target(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("config", "core.autocrlf", "false"),
                 ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", "var/\n/artifacts/runs/\n")  # the port's runtime exclude: dispatch.out is not git's (#49)
    _write(root, ORCH_JSON, json.dumps({"update_rule": "word", "home": "owner/home",
                                        "core_version": OLD_VERSION}, indent=2) + "\n")
    _write(root, MANIFEST, _manifest_text(OLD_FILES))
    for rel, text in OLD_FILES.items():
        _write(root, rel, text)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "target before the update").returncode == 0
    assert _git(root, "push", "-u", "origin", "main").returncode == 0
    return root


def _hook(root: Path, name: str) -> None:
    path = root / ".git" / "hooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\necho refused-by-test-hook >&2\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    rels = [ORCH_JSON, MANIFEST, *OLD_FILES, *NEW_HOME_FILES]
    return {rel: (root / rel).read_bytes() if (root / rel).is_file() else None for rel in dict.fromkeys(rels)}


def _run_update(root: Path) -> CoreUpdateError:
    with pytest.raises(CoreUpdateError) as caught:
        run_core_update(root, gh_runner=_fake_gh(NEW_HOME_FILES), live_check=lambda: False,
                        step_runner=lambda _root: [])
    return caught.value


def _assert_tree_as_found(root: Path, before: dict[str, bytes | None], head: str) -> None:
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _snapshot(root) == before
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == OLD_VERSION


# Row 1 -- `git commit` fails (a pre-commit hook exits 1): the update is undone in full.
def test_row1_failed_commit_undoes_the_update(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _snapshot(root)
    _hook(root, "pre-commit")

    error = _run_update(root)

    assert "git commit failed" in str(error)
    assert str(error).endswith("undone, nothing changed")
    _assert_tree_as_found(root, before, head)


# Row 2 -- the push is refused (a pre-push hook exits 1): the existing undo, same end state.
def test_row2_refused_push_undoes_the_update(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _snapshot(root)
    _hook(root, "pre-push")

    error = _run_update(root)

    assert "push refused" in str(error)
    assert str(error).endswith("undone, nothing changed")
    _assert_tree_as_found(root, before, head)


def _fail_git(monkeypatch: pytest.MonkeyPatch, *subcommands: str) -> None:
    """`core_update._git` answers rc 1 for these subcommands (a stuck index.lock, say), real git otherwise."""

    real = core_update._git

    def fake(cwd: Path, *args: str, env=None):
        if args and args[0] in subcommands:
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: Unable to create index.lock")
        return real(cwd, *args, env=env)

    monkeypatch.setattr(core_update, "_git", fake)


# Row 3 -- `git add` fails (nothing staged): the written files are put back.
def test_row3_failed_add_undoes_the_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _snapshot(root)
    _fail_git(monkeypatch, "add")

    error = _run_update(root)

    assert "git add failed" in str(error)
    assert str(error).endswith("undone, nothing changed")
    _assert_tree_as_found(root, before, head)


# Row 4 -- `git add` and the unstaging `git reset` both fail: the restore still runs, tree clean.
def test_row4_failed_reset_still_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _snapshot(root)
    _fail_git(monkeypatch, "add", "reset")

    error = _run_update(root)

    assert "git add failed" in str(error)
    assert str(error).endswith("undone, nothing changed")
    _assert_tree_as_found(root, before, head)
