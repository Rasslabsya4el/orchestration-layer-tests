"""#157 unit 2: the collector's `fast` payload carries `version`. In the home repo (`repo` == `home`) it is the commit
count of HEAD in the project root; in a target it is `core_version_count` from config/orchestration.json; any other
case, or a failure, is None and never a crash. The collector is loaded as tests/test_issue114_collector.py loads it;
the outside calls of `fast` (agents, merge line, machine counters) are stubbed, so no process of the heavy tool runs."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_FOUND = sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))
assert len(_FOUND) == 1, f"the collector is expected in exactly one mod, found {_FOUND}"
spec = importlib.util.spec_from_file_location("collect157", _FOUND[0])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _rig(tmp_path: Path, config: dict, commits: int = 0) -> Path:
    """A project folder holding `config/orchestration.json`; `commits` > 0 makes it a git repo with that many commits."""
    rig = tmp_path / "rig"
    (rig / "config").mkdir(parents=True)
    (rig / "config" / "orchestration.json").write_text(json.dumps(config), encoding="utf-8")
    if commits:
        git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=str(rig), check=True, capture_output=True,
                       creationflags=NO_WINDOW)
        for n in range(commits):
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", f"c{n}"], cwd=str(rig), check=True,
                           capture_output=True, creationflags=NO_WINDOW)
    return rig


def _fast(monkeypatch: pytest.MonkeyPatch, rig: Path) -> dict:
    monkeypatch.setattr(collect, "REPO", rig)
    monkeypatch.setattr(collect, "agents", lambda: [])
    monkeypatch.setattr(collect, "merge_status", lambda: {"missing": True})
    monkeypatch.setattr(collect, "machine", lambda *a, **k: {})
    return collect.fast()


def test_home_repo_version_is_the_commit_count_of_head(tmp_path, monkeypatch) -> None:
    rig = _rig(tmp_path, {"repo": "o/r", "home": "o/r"}, commits=3)
    assert _fast(monkeypatch, rig)["version"] == 3


def test_target_version_is_the_core_version_count_of_the_config(tmp_path, monkeypatch) -> None:
    rig = _rig(tmp_path, {"repo": "o/t", "home": "o/r", "core_version_count": 41})
    assert _fast(monkeypatch, rig)["version"] == 41


def test_target_without_a_count_is_none_not_a_crash(tmp_path, monkeypatch) -> None:
    rig = _rig(tmp_path, {"repo": "o/t", "home": "o/r"})
    out = _fast(monkeypatch, rig)
    assert out["version"] is None
    assert [k for k in out if k.endswith("_error")] == []


def test_home_repo_that_is_not_a_git_repo_is_none_not_a_crash(tmp_path, monkeypatch) -> None:
    rig = _rig(tmp_path, {"repo": "o/r", "home": "o/r"})  # no git repository in the folder
    out = _fast(monkeypatch, rig)
    assert out["version"] is None
    assert [k for k in out if k.endswith("_error")] == []
