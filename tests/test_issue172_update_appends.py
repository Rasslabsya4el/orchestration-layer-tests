"""#172 unit 2 (acceptance row 3): ``core update`` appends to a target's monitor ``hooks/project.ts`` each export the
core's hooks import that the file lacks, taken from the stub with the target's placeholders filled; every existing byte
stays. Real git on a tmp target (a bare origin and a clone); the home is a fake gh runner; the monitor install is a
fake runner. The helpers are this file's own copy of the #149 ones (a manifest file may not import a test module)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_check, port
from tools.agentops.core_update import CoreUpdateError, CoreUpdateRefused, run_core_update, run_update_steps

MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
THIRD_VERSION = "c" * 40
REPO = "owner/Target-Repo"
PLUGIN_ID = "target-monitor"
MONITOR_FOLDER = f"mods/{core_check.MONITOR_LITERAL}"
REGISTER = f"{MONITOR_FOLDER}/hooks/register.tsx"
PROJECT = f"{MONITOR_FOLDER}/hooks/project.ts"
STUB = "config/core_templates/monitor_project.ts.stub"
OLD_REGISTER = "import { MOD_DIR } from './project'\n"
NEW_REGISTER = "import { MOD_DIR, COLLECTOR_FAST } from './project'\n"
OLD_STUB = "export const MOD_DIR = '{mod_dir}'\n"
NEW_STUB = (OLD_STUB
            + "// the fast payload an always-running collector writes\n"
            + "export const COLLECTOR_FAST = `${MOD_DIR}/bin/.cache/collector-fast.json`\n"
            + "export const REPO_NAME = '{repo_slug}'\n")
PROJECT_OLD = "// the target's own\nexport const MOD_DIR = 'X:/mods/target-monitor'\nexport const REPO_NAME = 'owner/Target-Repo'\n"
HOME_BASE = {REGISTER: OLD_REGISTER, STUB: OLD_STUB, "docs/agents/one.md": "one v1\n"}
HOME_NEW = {REGISTER: NEW_REGISTER, STUB: NEW_STUB, "docs/agents/one.md": "one v1\n"}


class FakeMonitorRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, root: Path, source: str) -> None:
        self.calls.append((root, source))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, data: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _manifest_text(files: dict[str, str]) -> str:
    return "".join(f"{rel}\n" for rel in [MANIFEST, *files])


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _fake_gh(home_files: dict[str, str], head: str):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = _manifest_text(home_files).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            return SimpleNamespace(returncode=0, stdout=head + "\n", stderr="")
        if "/git/trees/" in path:
            body = {"sha": head, "truncated": False,
                    "tree": [{"path": rel, "type": "blob", "sha": _blob_sha(data)} for rel, data in contents.items()]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(contents[rel]).decode("ascii") + "\n", stderr="")

    return run


def _target(tmp_path: Path, project: str | bytes | None = PROJECT_OLD) -> Path:
    """A committed target holding the old core files and, unless ``project`` is None, its monitor's project.ts."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("config", "core.autocrlf", "false"),
                 ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", "var/\nartifacts/\n")
    _write(root, ORCH_JSON, json.dumps({"update_rule": "word", "home": "owner/home", "repo": REPO,
                                        "core_version": OLD_VERSION,
                                        "monitor": {"plugin_id": PLUGIN_ID}}, indent=2) + "\n")
    _write(root, MANIFEST, _manifest_text(HOME_BASE))
    for rel, text in HOME_BASE.items():
        _write(root, rel, text)
    if project is not None:
        _write(root, PROJECT, project)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "target with a monitor").returncode == 0
    assert _git(root, "push", "-u", "origin", "main").returncode == 0
    return root


def _update(root: Path, home_files: dict[str, str], head: str = NEW_VERSION, runner: FakeMonitorRunner | None = None):
    return run_core_update(root, gh_runner=_fake_gh(home_files, head), live_check=lambda: False,
                           step_runner=run_update_steps, monitor_runner=runner or FakeMonitorRunner(),
                           lock_wait_s=5.0)


def _declaration() -> str:
    return ("// the fast payload an always-running collector writes\n"
            f"export const COLLECTOR_FAST = `${{MOD_DIR}}/bin/.cache/collector-fast.json`\n")


def test_update_appends_exactly_the_missing_export_and_keeps_every_old_byte(tmp_path: Path) -> None:
    root = _target(tmp_path)
    runner = FakeMonitorRunner()

    result = _update(root, HOME_NEW, runner=runner)

    old = PROJECT_OLD.encode("utf-8")
    new = (root / PROJECT).read_bytes()
    assert new == old + _declaration().encode("utf-8")
    assert PROJECT in result.changed
    assert runner.calls == [(root, core_check.MONITOR_LITERAL)]  # the changed project.ts is installed
    assert PROJECT in _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()  # the update's own commit
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_appended_declaration_has_the_targets_placeholders_filled(tmp_path: Path) -> None:
    stub = NEW_STUB + "export const WHERE = '{mod_dir}/where'\n"
    root = _target(tmp_path)
    mod_dir = port._install_folder(root, REPO).as_posix()
    register = "import { MOD_DIR, COLLECTOR_FAST, WHERE } from './project'\n"

    _update(root, {**HOME_NEW, REGISTER: register, STUB: stub})

    text = (root / PROJECT).read_text(encoding="utf-8")
    assert text.endswith(_declaration() + f"export const WHERE = '{mod_dir}/where'\n")
    assert "{" not in text.replace("${MOD_DIR}", "")


def test_second_update_appends_nothing(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _update(root, HOME_NEW)
    after_first = (root / PROJECT).read_bytes()

    _update(root, {**HOME_NEW, "docs/agents/one.md": "one v2\n"}, head=THIRD_VERSION)

    assert (root / PROJECT).read_bytes() == after_first
    assert (root / "docs/agents/one.md").read_text(encoding="utf-8") == "one v2\n"


def test_target_with_every_name_keeps_project_ts_untouched(tmp_path: Path) -> None:
    complete = PROJECT_OLD + "export const COLLECTOR_FAST = 'mine'\n"
    root = _target(tmp_path, complete)
    os.utime(root / PROJECT, ns=(1_000_000_000, 1_000_000_000))
    stamp = (root / PROJECT).stat().st_mtime_ns

    _update(root, HOME_NEW)

    assert (root / PROJECT).read_bytes() == complete.encode("utf-8")
    assert (root / PROJECT).stat().st_mtime_ns == stamp


def test_project_ts_without_a_final_newline_and_with_crlf_keeps_its_style(tmp_path: Path) -> None:
    old = b"export const MOD_DIR = 'X'\r\nexport const REPO_NAME = 'Y'"
    root = _target(tmp_path, old)

    _update(root, HOME_NEW)

    new = (root / PROJECT).read_bytes()
    assert new.startswith(old + b"\r\n// the fast payload")
    assert new.endswith(b"collector-fast.json`\r\n")
    assert new.count(b"\n") == new.count(b"\r\n")


def test_absent_project_ts_is_not_created(tmp_path: Path) -> None:
    root = _target(tmp_path, None)

    _update(root, HOME_NEW)

    assert not (root / PROJECT).exists()


def test_a_name_the_stub_lacks_fails_the_update_and_leaves_the_tree_as_it_was(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    register = "import { MOD_DIR, NO_SUCH_NAME } from './project'\n"

    with pytest.raises(CoreUpdateError, match="NO_SUCH_NAME"):
        _update(root, {**HOME_NEW, REGISTER: register})

    assert (root / PROJECT).read_bytes() == PROJECT_OLD.encode("utf-8")
    assert (root / REGISTER).read_text(encoding="utf-8") == OLD_REGISTER
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == OLD_VERSION
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_project_ts_with_an_uncommitted_edit_refuses_the_update_and_puts_everything_back(tmp_path: Path) -> None:
    root = _target(tmp_path)
    edited = PROJECT_OLD + "// my uncommitted line\n"
    _write(root, PROJECT, edited)

    with pytest.raises(CoreUpdateRefused, match="uncommitted change"):
        _update(root, HOME_NEW)

    assert (root / PROJECT).read_bytes() == edited.encode("utf-8")
    assert (root / REGISTER).read_text(encoding="utf-8") == OLD_REGISTER
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == OLD_VERSION
