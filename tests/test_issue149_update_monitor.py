"""#149 unit 3: a core update that brings a changed monitor file into a target installs that target's monitor.

``core_update._write_and_commit`` calls ``monitor_install.install_after`` once the update's commit has landed, with
the paths the update changed or removed. The runner is injected (``monitor_runner``), so no real ``mods install``
runs here. Real git on a tmp target (a bare origin and a clone); the home is a fake gh runner. The helpers are this
file's own copy of the #49 ones: a manifest file may not import a test module outside the manifest (core check, I)."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.agentops import core_check
from tools.agentops.core_update import SNAPSHOT_ENTRY, run_core_update

MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
OLD_FILES = {"docs/agents/one.md": "one v1\n", "docs/agents/two.md": "two v1\n"}
NEW_HOME_FILES = {"docs/agents/one.md": "one v2\n", "docs/agents/two.md": "two v1\n",
                  "docs/agents/three.md": "three v2 (new)\n"}
MONITOR_FOLDER = f"mods/{core_check.MONITOR_LITERAL}"
HOOK = f"{MONITOR_FOLDER}/hooks/x.tsx"
OLD_HOOK = "hook v1\n"
NEW_HOOK = "hook v2\n"
PLUGIN_ID = "orch-monitor"


class FakeMonitorRunner:
    """Records each call (root, source) and raises ``error`` when one is given."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.error = error

    def __call__(self, root: Path, source: str) -> None:
        self.calls.append((root, source))
        if self.error is not None:
            raise self.error


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest_text(files: dict[str, str]) -> str:
    return "".join(f"{rel}\n" for rel in [MANIFEST, *files])


def _blob_sha(data: bytes) -> str:
    """The sha git gives a blob of ``data``: what the home's tree lists (#148), computed without a subprocess."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _fake_gh(home_files: dict[str, str]):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = _manifest_text(home_files).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            return SimpleNamespace(returncode=0, stdout=NEW_VERSION + "\n", stderr="")
        if "/git/trees/" in path:  # #148: the home's whole tree in one call, every blob with its git sha
            assert path.endswith(f"{NEW_VERSION}?recursive=1"), path
            body = {"sha": NEW_VERSION, "truncated": False,
                    "tree": [{"path": "docs", "type": "tree", "sha": "0" * 40}]
                    + [{"path": rel, "type": "blob", "sha": _blob_sha(data)} for rel, data in contents.items()]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
        body = base64.b64encode(contents[rel]).decode("ascii")
        return SimpleNamespace(returncode=0, stdout=body + "\n", stderr="")

    return run


def _monitor_target(tmp_path: Path) -> Path:
    """A target with a monitor: its settings name the plugin, its literal monitor folder holds a file, and all of it
    is committed (a dirty settings file would refuse the update)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("config", "core.autocrlf", "false"),
                 ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", "var/\nartifacts/\n")
    _write(root, ORCH_JSON, json.dumps({"update_rule": "word", "home": "owner/home", "core_version": OLD_VERSION,
                                        "monitor": {"plugin_id": PLUGIN_ID}}, indent=2) + "\n")
    _write(root, MANIFEST, _manifest_text({**OLD_FILES, HOOK: OLD_HOOK}))
    for rel, text in {**OLD_FILES, HOOK: OLD_HOOK}.items():
        _write(root, rel, text)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "target with a monitor").returncode == 0
    assert _git(root, "push", "-u", "origin", "main").returncode == 0
    return root


def _version(root: Path) -> str:
    return json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"]


def _update(root: Path, home_files: dict[str, str], runner: FakeMonitorRunner):
    return run_core_update(root, gh_runner=_fake_gh(home_files), live_check=lambda: False,
                           step_runner=lambda _root: [], monitor_runner=runner, lock_wait_s=5.0)


def test_update_that_changes_the_monitor_installs_it_once_under_the_target_root(tmp_path: Path) -> None:
    root = _monitor_target(tmp_path)
    runner = FakeMonitorRunner()

    result = _update(root, {**NEW_HOME_FILES, HOOK: NEW_HOOK}, runner)

    assert HOOK in result.changed
    assert _version(root) == NEW_VERSION
    assert runner.calls == [(root, core_check.MONITOR_LITERAL)]
    assert result.message.endswith("monitor installed")


def test_update_that_changes_no_monitor_file_does_not_install(tmp_path: Path) -> None:
    root = _monitor_target(tmp_path)
    runner = FakeMonitorRunner()

    result = _update(root, {**NEW_HOME_FILES, HOOK: OLD_HOOK}, runner)

    assert result.changed and HOOK not in result.changed
    assert _version(root) == NEW_VERSION
    assert runner.calls == []
    assert "monitor" not in result.message


def test_failed_install_never_undoes_the_update_and_is_named_in_its_message(tmp_path: Path) -> None:
    root = _monitor_target(tmp_path)
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    runner = FakeMonitorRunner(error=RuntimeError("boom"))

    result = _update(root, {**NEW_HOME_FILES, HOOK: NEW_HOOK}, runner)

    assert len(runner.calls) == 1
    assert result.message.endswith("monitor install failed: boom")
    assert _version(root) == NEW_VERSION
    assert (root / HOOK).read_text(encoding="utf-8") == NEW_HOOK
    assert (root / "docs" / "agents" / "three.md").read_text(encoding="utf-8") == "three v2 (new)\n"
    assert _git(root, "rev-parse", "HEAD").stdout.strip() != head_before  # the update's commit landed
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / SNAPSHOT_ENTRY).exists()  # the snapshot finally ran as on a clean update
