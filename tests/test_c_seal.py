"""Issue #376 unit 3: the seal of a C clone (tools/replays/c_seal.py), measured on a temp repo of 3 commits.

The base is the second commit; the third is the "solution" a sealed clone must never hold."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.replays import c_seal
from tools.replays.c_seal import SealError, check_sealed, env_from_state, make_clone, sealed_env

TIMEOUT_SECONDS = 120


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          timeout=TIMEOUT_SECONDS)
    return done.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> dict:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@localhost")
    shas = []
    for n in (1, 2, 3):
        (repo / "f.txt").write_text(f"v{n}\n", encoding="utf-8")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", f"c{n}")
        shas.append(git(repo, "rev-parse", "HEAD"))
    return {"repo": repo, "first": shas[0], "base": shas[1], "later": shas[2]}


@pytest.fixture
def sealed(source: dict, tmp_path: Path) -> dict:
    clone = make_clone(source["repo"], source["base"], tmp_path / "clone")
    env = sealed_env(clone, tmp_path / "state")
    return {**source, "clone": clone, "env": env, "state": tmp_path / "state"}


def test_clean_clone_passes(sealed: dict) -> None:
    clone = sealed["clone"]
    assert git(clone, "rev-parse", "HEAD") == sealed["base"]
    assert git(clone, "branch", "--format=%(refname:short)") == "c-base"
    assert git(clone, "remote") == ""
    assert git(clone, "rev-list", "--all").split() == [sealed["base"], sealed["first"]]
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", sealed["later"]],
                          timeout=TIMEOUT_SECONDS).returncode != 0
    assert check_sealed(clone, sealed["base"], [sealed["later"]], sealed["env"]) is None


def test_clone_with_the_branch_name_given(source: dict, tmp_path: Path) -> None:
    clone = make_clone(source["repo"], source["base"], tmp_path / "clone", branch="c-127")
    assert git(clone, "branch", "--format=%(refname:short)") == "c-127"


def test_env_has_no_token_and_the_clone_denies_the_checkouts(source: dict, tmp_path: Path, monkeypatch) -> None:
    tokens = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
    for name in tokens:
        monkeypatch.setenv(name, "dummy-secret")
    clone = make_clone(source["repo"], source["base"], tmp_path / "clone")
    deny_checkout = tmp_path / "real checkout"
    env = sealed_env(clone, tmp_path / "state", deny_checkout=deny_checkout)
    assert not [name for name in env if name in tokens or name.removeprefix("REPLAY_") in tokens]
    assert "dummy-secret" not in env.values()
    settings = json.loads((clone / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert settings["autoMemoryEnabled"] is False
    forward = deny_checkout.as_posix()
    assert f"Bash(*{forward}*)" in settings["permissions"]["deny"]
    assert f"Read({forward}/**)" in settings["permissions"]["deny"]


def test_env_from_state_writes_nothing_and_equals_the_sealed_env(sealed: dict) -> None:
    def files(root: Path) -> dict:
        return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    before = {**files(sealed["state"]), **files(sealed["clone"] / ".claude")}
    again = env_from_state(sealed["clone"], sealed["state"])
    assert {k: v for k, v in again.items() if k != "PYTEST_CURRENT_TEST"} == \
        {k: v for k, v in sealed["env"].items() if k != "PYTEST_CURRENT_TEST"}
    assert {**files(sealed["state"]), **files(sealed["clone"] / ".claude")} == before


def test_planted_leak_ref_is_refused(sealed: dict) -> None:
    git(sealed["clone"], "fetch", "-q", str(sealed["repo"]), f"{sealed['later']}:refs/heads/leak")
    with pytest.raises(SealError, match="not an ancestor of the base"):
        check_sealed(sealed["clone"], sealed["base"], [], sealed["env"])


def test_planted_leak_object_without_a_ref_is_refused(sealed: dict) -> None:
    git(sealed["clone"], "fetch", "-q", str(sealed["repo"]), sealed["later"])
    with pytest.raises(SealError, match="not an ancestor of the base"):
        check_sealed(sealed["clone"], sealed["base"], [], sealed["env"])


def test_remote_is_refused(sealed: dict) -> None:
    git(sealed["clone"], "remote", "add", "origin", str(sealed["repo"]))
    with pytest.raises(SealError, match="remote"):
        check_sealed(sealed["clone"], sealed["base"], [sealed["later"]], sealed["env"])


def plant_talking_gh(state: Path) -> None:
    (state / "gh.cmd").write_text("@echo off\r\necho issue text\r\n", encoding="utf-8", newline="")
    (state / "gh").write_text("#!/bin/sh\necho issue text\n", encoding="utf-8", newline="\n")
    os.chmod(state / "gh", 0o755)


def test_gh_that_returns_text_is_refused(sealed: dict) -> None:
    plant_talking_gh(sealed["state"])
    with pytest.raises(SealError, match="gh issue view 1 answered"):
        check_sealed(sealed["clone"], sealed["base"], [sealed["later"]], sealed["env"])


def test_probe_with_the_tickets_own_number_returns_empty(sealed: dict) -> None:
    assert check_sealed(sealed["clone"], sealed["base"], [], sealed["env"], issues=(376, 127)) is None
    gh = shutil.which("gh", path=sealed["env"]["PATH"])
    done = subprocess.run([gh, "issue", "view", "376"], cwd=sealed["clone"], env=sealed["env"], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS)
    assert done.stdout == "" and done.returncode != 0


def test_forbidden_sha_present_is_refused(sealed: dict) -> None:
    with pytest.raises(SealError, match="forbidden"):
        check_sealed(sealed["clone"], sealed["base"], [sealed["first"]], sealed["env"])


def test_main_make_prints_sealed_and_refusal_exits_1(source: dict, tmp_path: Path, capsys) -> None:
    args = ["make", "--source", str(source["repo"]), "--base", source["base"], "--dest", str(tmp_path / "m1"),
            "--forbid", source["later"], "--state", str(tmp_path / "s1")]
    assert c_seal.main(args) == 0
    assert capsys.readouterr().out.strip() == "sealed"
    args[args.index("--forbid") + 1] = source["first"]  # an ancestor of the base is in the clone: refused
    args[args.index("--dest") + 1] = str(tmp_path / "m2")
    assert c_seal.main(args) == 1
    assert "forbidden" in capsys.readouterr().out


def test_main_check_is_read_only_and_sees_a_gh_planted_in_the_state_dir(source: dict, tmp_path: Path, capsys) -> None:
    state, clone = tmp_path / "s1", tmp_path / "m1"
    make = ["make", "--source", str(source["repo"]), "--base", source["base"], "--dest", str(clone),
            "--forbid", source["later"], "--issue", "376", "--state", str(state)]
    assert c_seal.main(make) == 0
    check = ["check", "--clone", str(clone), "--base", source["base"], "--forbid", source["later"], "--issue", "376",
            "--state", str(state)]
    assert c_seal.main(check) == 0
    plant_talking_gh(state)
    planted = {p: p.read_bytes() for p in [*state.rglob("*"), *(clone / ".claude").rglob("*")] if p.is_file()}
    capsys.readouterr()
    assert c_seal.main(check) == 1
    assert "gh issue view 376 answered" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in planted} == planted  # the check rewrote no shim and no setting
