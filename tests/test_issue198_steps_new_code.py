"""#198 change 3: the post-copy steps run from the UPDATED code. The #172 export append is one of them: it lives in
``run_update_steps`` (the fresh process of the new files), not in the old process's ``_write_and_commit``, so a target
crossing an export-adding step from an older core still gets the export before the one commit. Real git on a tmp
target; the home is a fake gh runner. The helpers are this file's own (a manifest file may not import a test module)."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_check, project_exports
from tools.agentops import core_update
from tools.agentops.core_update import CoreUpdateError, CoreUpdateRefused, run_core_update, run_update_steps

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
REPO = "owner/Target-Repo"
MONITOR_FOLDER = f"mods/{core_check.MONITOR_LITERAL}"
REGISTER = f"{MONITOR_FOLDER}/hooks/register.tsx"
PROJECT = f"{MONITOR_FOLDER}/hooks/project.ts"
STUB = "config/core_templates/monitor_project.ts.stub"
OLD_REGISTER = "import { MOD_DIR } from './project'\n"
NEW_REGISTER = "import { MOD_DIR, COLLECTOR_FAST } from './project'\n"
OLD_STUB = "export const MOD_DIR = '{mod_dir}'\n"
NEW_STUB = (OLD_STUB
            + "// the fast payload an always-running collector writes\n"
            + "export const COLLECTOR_FAST = `${MOD_DIR}/bin/.cache/collector-fast.json`\n")
PROJECT_OLD = "// the target's own\nexport const MOD_DIR = 'X:/mods/target-monitor'\nexport const REPO_NAME = 'owner/Target-Repo'\n"
HOME_BASE = {REGISTER: OLD_REGISTER, STUB: OLD_STUB, "docs/agents/one.md": "one v1\n"}
HOME_NEW = {REGISTER: NEW_REGISTER, STUB: NEW_STUB, "docs/agents/one.md": "one v1\n"}
DECLARATION = ("// the fast payload an always-running collector writes\n"
               "export const COLLECTOR_FAST = `${MOD_DIR}/bin/.cache/collector-fast.json`\n")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, data: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8"))


def _fake_gh(home_files: dict[str, str], head: str):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = "".join(f"{rel}\n" for rel in [MANIFEST, *home_files]).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            return SimpleNamespace(returncode=0, stdout=head + "\n", stderr="")
        if "/git/trees/" in path:
            tree = [{"path": rel, "type": "blob", "sha": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}
                    for rel, data in contents.items()]
            return SimpleNamespace(returncode=0, stdout=json.dumps({"sha": head, "truncated": False, "tree": tree}),
                                   stderr="")
        rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(contents[rel]).decode("ascii") + "\n", stderr="")

    return run


def _target(tmp_path: Path, with_code: bool = False) -> Path:
    """A committed target on the old core files with its monitor's project.ts; ``with_code`` also gives it this
    tree's ``tools/agentops`` so the default step runner can start the updated code in a fresh process."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                 ("config", "core.autocrlf", "false"), ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", "var/\nartifacts/\n__pycache__/\n")
    settings = json.loads((REPO_ROOT / ORCH_JSON).read_text(encoding="utf-8"))  # every key the code requires
    settings.update({"update_rule": "word", "home": "owner/home", "repo": REPO, "core_version": OLD_VERSION,
                     "monitor": {"plugin_id": "target-monitor"}})
    _write(root, ORCH_JSON, json.dumps(settings, indent=2) + "\n")
    _write(root, MANIFEST, "".join(f"{rel}\n" for rel in [MANIFEST, *HOME_BASE]))
    for rel, text in HOME_BASE.items():
        _write(root, rel, text)
    _write(root, PROJECT, PROJECT_OLD)
    if with_code:
        for folder in ("tools", "scripts"):
            shutil.copytree(REPO_ROOT / folder, root / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        _write(root, MANIFEST, "".join(f"{rel}\n" for rel in [MANIFEST, *HOME_BASE]))
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "target with a monitor").returncode == 0
    assert _git(root, "push", "-u", "origin", "main").returncode == 0
    return root


def _update(root: Path, step_runner, home_files: dict[str, str] = HOME_NEW):
    return run_core_update(root, gh_runner=_fake_gh(home_files, NEW_VERSION), live_check=lambda: False,
                           step_runner=step_runner, monitor_runner=lambda _root, _source: None, lock_wait_s=5.0)


def test_the_old_process_appends_nothing_a_step_runner_that_runs_no_steps_leaves_project_ts(tmp_path: Path) -> None:
    root = _target(tmp_path)

    _update(root, lambda _root: [])  # what an older updater's step runner amounts to for this step

    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8")
    assert project_exports.missing_names(root / MONITOR_FOLDER / "hooks") == ["COLLECTOR_FAST"]


def test_the_updated_codes_steps_append_the_export_into_the_one_commit(tmp_path: Path) -> None:
    root = _target(tmp_path)

    result = _update(root, run_update_steps)

    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8") + DECLARATION.encode("utf-8")
    assert project_exports.missing_names(root / MONITOR_FOLDER / "hooks") == []
    assert PROJECT in result.changed
    stat = _git(root, "show", "--stat", "--format=", "HEAD").stdout
    assert PROJECT in stat and REGISTER in stat and ORCH_JSON in stat  # one commit holds the export and the core files
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_run_update_steps_writes_nothing_when_no_name_is_missing_or_no_file(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _write(root, REGISTER, OLD_REGISTER)  # nothing imported that project.ts lacks
    assert run_update_steps(root) == [core_update.PATTERNS_ENTRY]
    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8")

    (root / PROJECT).unlink()
    _write(root, REGISTER, NEW_REGISTER)
    assert run_update_steps(root) == []
    assert not (root / PROJECT).exists()


def test_a_fresh_process_of_the_new_files_does_the_append_and_the_same_commit_holds_it(tmp_path: Path) -> None:
    root = _target(tmp_path, with_code=True)

    _update(root, None)  # the default step runner: `python -m tools.agentops core update-steps` in the target

    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8") + DECLARATION.encode("utf-8")
    assert PROJECT in _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()


def test_a_dirty_project_ts_in_the_fresh_process_refuses_and_puts_everything_back(tmp_path: Path) -> None:
    root = _target(tmp_path, with_code=True)
    edited = PROJECT_OLD + "// my uncommitted line\n"
    _write(root, PROJECT, edited)

    with pytest.raises(CoreUpdateRefused, match="uncommitted change") as raised:
        _update(root, None)

    assert PROJECT in raised.value.reason
    assert (root / PROJECT).read_bytes() == edited.encode("utf-8")
    assert (root / REGISTER).read_text(encoding="utf-8") == OLD_REGISTER
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == OLD_VERSION


def test_a_name_the_stub_lacks_in_the_fresh_process_fails_the_update_and_undoes_the_copy(tmp_path: Path) -> None:
    root = _target(tmp_path, with_code=True)
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    register = "import { MOD_DIR, NO_SUCH_NAME } from './project'\n"

    with pytest.raises(CoreUpdateError, match="NO_SUCH_NAME") as raised:
        _update(root, None, {**HOME_NEW, REGISTER: register})

    assert not isinstance(raised.value, CoreUpdateRefused)
    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8")
    assert (root / REGISTER).read_text(encoding="utf-8") == OLD_REGISTER
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == OLD_VERSION
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
