"""Issue 93 unit 3: a target that lacks `config/core_leak_patterns.json` gets a starter one, from `target add`
(the port step, `port.write_own_files`) and from a core update (`run_update_steps`, in the same commit). The
starter holds one pattern, `L3`, that matches the target's folder path and repo name (both slash kinds);
L1 and L2 are absent. A file that is already there is never touched (its bytes unchanged).

Real files on a tmp folder; the core update runs on the #28 family's real git target (bare origin + clone),
the home is a fake `gh` runner, no network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests import test_issue28_core_update_failed_commit as base
from tools.agentops import core_update, leak_starter, port
from tools.agentops.core_update import CoreUpdateError, run_core_update

PATTERNS = "config/core_leak_patterns.json"
REPO = "owner/third-finances"
OWN_BYTES = b'{\n  "L1": "our own product words"\n}\n'


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _l3(path: Path) -> re.Pattern[str]:
    raw = _load(path)
    assert set(raw) == {"L3"}, f"the starter holds only L3, got keys {sorted(raw)}"
    return re.compile(raw["L3"], re.IGNORECASE)


def _assert_matches_folder(pattern: re.Pattern[str], folder: Path) -> None:
    text = str(folder)
    forward = text.replace("\\", "/")
    backward = text.replace("/", "\\")
    assert pattern.search(text), f"L3 misses the folder path as written: {text}"
    assert pattern.search(forward), f"L3 misses the folder path with forward slashes: {forward}"
    assert pattern.search(backward), f"L3 misses the folder path with backslashes: {backward}"


# -- the module itself (the seam both callers use) ------------------------------------------------

def test_starter_text_matches_folder_path_and_repo_name_and_has_only_l3(tmp_path: Path) -> None:
    folder = (tmp_path / "3go" / "zorblax").resolve()
    text = leak_starter.starter_text(folder, REPO)

    pattern = re.compile(json.loads(text)["L3"], re.IGNORECASE)  # the L3 regex compiles
    assert set(json.loads(text)) == {"L3"}
    _assert_matches_folder(pattern, folder)
    assert pattern.search(REPO)
    assert pattern.search(REPO.replace("/", "\\"))
    assert pattern.search("third-finances")
    assert not pattern.search("an unrelated project")


def test_write_starter_writes_a_missing_file_and_returns_its_path(tmp_path: Path) -> None:
    folder = (tmp_path / "fake target").resolve()
    folder.mkdir()

    written = leak_starter.write_starter(folder, REPO)

    assert written == folder / PATTERNS
    assert written.is_file()
    _assert_matches_folder(_l3(written), folder)


def test_write_starter_keeps_an_existing_file_byte_for_byte(tmp_path: Path) -> None:
    folder = (tmp_path / "fake target").resolve()
    (folder / "config").mkdir(parents=True)
    (folder / PATTERNS).write_bytes(OWN_BYTES)

    assert leak_starter.write_starter(folder, REPO) is None
    assert (folder / PATTERNS).read_bytes() == OWN_BYTES


# -- acceptance 3: `target add` (the port step writes the starter like the #44 stub) -------------------

def _own_files(target: Path) -> list[str]:
    return port.write_own_files(target, REPO, None, "word", home_repo="owner/home",
                                core_version="0" * 40, plugin_name="mattpocock-skills@mattpocock")


def test_target_add_port_step_writes_the_starter_on_a_folder_without_it(tmp_path: Path) -> None:
    folder = (tmp_path / "third").resolve()
    folder.mkdir()

    written = _own_files(folder)

    assert PATTERNS in written
    _assert_matches_folder(_l3(folder / PATTERNS), folder)


def test_target_add_port_step_keeps_a_folder_file_byte_for_byte(tmp_path: Path) -> None:
    folder = (tmp_path / "third").resolve()
    (folder / "config").mkdir(parents=True)
    (folder / PATTERNS).write_bytes(OWN_BYTES)

    written = _own_files(folder)

    assert PATTERNS not in written
    assert (folder / PATTERNS).read_bytes() == OWN_BYTES


# -- acceptance 4: a core update into a target (the post-copy step, in the update's one commit) ---

def _run(root: Path):
    return run_core_update(root, gh_runner=base._fake_gh(base.NEW_HOME_FILES), live_check=lambda: False)


def _last_commit_paths(root: Path) -> list[str]:
    out = base._git(root, "show", "--name-only", "--format=", "HEAD").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_step_writes_starter_when_absent_and_names_the_origin_repo(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    assert not (root / PATTERNS).exists()

    written = core_update.run_update_steps(root)

    assert PATTERNS in written
    pattern = _l3(root / PATTERNS)
    _assert_matches_folder(pattern, root.resolve())
    assert pattern.search("origin"), "the origin remote's repo name is not in L3"


def test_core_update_commits_the_starter_into_a_target_without_it(tmp_path: Path) -> None:
    root = base._target(tmp_path)

    _run(root)

    assert (root / PATTERNS).is_file()
    assert PATTERNS in _last_commit_paths(root)
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


def test_core_update_keeps_a_target_file_byte_for_byte(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    base._write(root, PATTERNS, OWN_BYTES.decode("utf-8"))
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "target's own leak words").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0

    assert PATTERNS not in core_update.run_update_steps(root)
    assert (root / PATTERNS).read_bytes() == OWN_BYTES
    _run(root)
    assert (root / PATTERNS).read_bytes() == OWN_BYTES
    assert PATTERNS not in _last_commit_paths(root)


def test_step_failure_after_the_starter_leaves_no_starter_and_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    root = base._target(tmp_path)
    head_before = base._git(root, "rev-parse", "HEAD").stdout.strip()
    real_steps = core_update.run_update_steps
    seen: dict[str, bool] = {}

    def failing_steps(target: Path) -> list[str]:
        real_steps(target)
        seen["written"] = (target / PATTERNS).is_file()
        raise RuntimeError("step boom")

    monkeypatch.setattr(core_update, "run_update_steps", failing_steps)
    with pytest.raises(CoreUpdateError):
        _run(root)

    assert seen == {"written": True}, "the step did not write the starter before it failed"
    assert not (root / PATTERNS).exists()
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""
    assert base._git(root, "rev-parse", "HEAD").stdout.strip() == head_before
