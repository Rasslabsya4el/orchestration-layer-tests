"""#343 unit 3: the map build on a GitHub runner (``scripts/ci_map_build.py``, ``.github/workflows/map.yml``).

The script builds the map over the public test copy of a checkout (tests the settings' ``test_copy_exclude`` keeps
out are absent from the map), writes ``meta.json`` with the keys the PC's ``select_for_diff`` reads (``built_at``,
``sha``, ``tests``, ``green``, ``map_commit``, ``last_green_commit``, ``total``), and leaves exactly the artifact folder
``testmondata``, ``reads/`` and ``meta.json``. A red build keeps ``meta.json`` with ``green`` false and exits non-zero;
a git call that hangs is a red build too. The workflow is a ``workflow_dispatch`` whose upload step is named
``test-map``, runs ``if: always()``, and uploads one artifact of that name.

Seams: the script's command line (subprocess, the artifact folder it leaves), the PC reader ``select_for_diff`` over
that folder, and the module's ``_git`` / ``main`` with a fake ``subprocess.run`` for the hung git call.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci_map_build.py"
WORKFLOW = REPO / ".github" / "workflows" / "map.yml"
PLUGIN = REPO / "tools" / "agentops" / "suite_reads_plugin.py"
META_KEYS = {"built_at", "sha", "tests", "green", "map_commit", "last_green_commit", "total"}
GREEN_ENTRIES = {"testmondata", "reads", "meta.json"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _project(root: Path, tests: dict[str, str], excluded: list[str]) -> None:
    """A tiny git project: the given test files, the reads plugin the map build needs, and a settings file whose
    ``test_copy_exclude`` names ``excluded``. Committed, so the build sees a HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, text in tests.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "tools" / "agentops").mkdir(parents=True, exist_ok=True)
    shutil.copy(PLUGIN, root / "tools" / "agentops" / "suite_reads_plugin.py")
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"test_copy_exclude": excluded}), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "tiny project")


def _build(root: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--out", str(out)],
                          capture_output=True, text=True, timeout=600)


def _meta(out: Path) -> dict:
    return json.loads((out / "meta.json").read_text(encoding="utf-8"))


def _script():
    """The script as a module, for the in-process tests of ``_git`` and ``main``."""
    spec = importlib.util.spec_from_file_location("ci_map_build", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GREEN_TESTS = {
    "tests/test_small.py": "def test_ok():\n    assert 1 + 1 == 2\n",
    "tests/test_excluded.py": "def test_hidden():\n    assert True\n",
}


def test_green_tiny_project_builds_the_three_entries(tmp_path):
    proj = tmp_path / "proj"
    _project(proj, GREEN_TESTS, excluded=["tests/test_excluded.py"])
    out = tmp_path / "test-map"
    run = _build(proj, out)
    assert run.returncode == 0, run.stdout + run.stderr
    assert set(os.listdir(out)) == GREEN_ENTRIES
    assert (out / "reads").is_dir() and any((out / "reads").glob("reads-*.json"))
    meta = _meta(out)
    assert set(meta) == META_KEYS
    head = _git(proj, "rev-parse", "HEAD")
    assert meta["green"] is True
    assert meta["sha"] == head
    assert meta["map_commit"] == head
    assert meta["last_green_commit"] == head
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["built_at"])  # ISO, UTC
    assert meta["tests"] == 1  # the test the settings keep out of the public copy is absent from the map
    assert meta["total"] == meta["tests"]


def test_pc_selection_accepts_the_staged_folder(tmp_path):
    from tools.agentops.test_select import select_for_diff

    proj = tmp_path / "proj"
    _project(proj, GREEN_TESTS, excluded=["tests/test_excluded.py"])
    out = tmp_path / "test-map"
    assert _build(proj, out).returncode == 0
    base = _git(proj, "rev-parse", "HEAD")  # the map's commit: an ancestor of the head tested below
    (proj / "tests" / "test_small.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    _git(proj, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-am", "edit")
    head = _git(proj, "rev-parse", "HEAD")
    sel = select_for_diff(proj, base, head, out)
    assert not sel.full, sel.reason  # not the "no map" / "map holds 0 tests" full-suite fallback
    assert sel.map_commit == base


def test_red_tiny_project_keeps_meta_green_false(tmp_path):
    proj = tmp_path / "proj"
    _project(proj, {"tests/test_red.py": "def test_bad():\n    assert False\n"}, excluded=[])
    out = tmp_path / "test-map"
    run = _build(proj, out)
    assert run.returncode != 0
    assert set(os.listdir(out)) == {"meta.json"}  # a red build writes no map, only its meta
    meta = _meta(out)
    head = _git(proj, "rev-parse", "HEAD")
    assert set(meta) == META_KEYS
    assert meta["green"] is False
    assert meta["sha"] == head
    assert meta["map_commit"] == head
    assert meta["last_green_commit"] == ""  # no green run before it
    assert meta["tests"] == 0 and meta["total"] == 0


def test_git_call_has_a_bound(tmp_path, monkeypatch):
    mod = _script()
    seen = {}

    def hung(cmd, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", hung)
    with pytest.raises(mod.GitTimeout):
        mod._git(tmp_path, "rev-parse", "HEAD")
    assert seen["timeout"] == mod.GIT_TIMEOUT_MINUTES * 60


def test_hung_git_is_a_red_build_with_its_meta(tmp_path, monkeypatch):
    mod = _script()

    def hung(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", hung)
    out = tmp_path / "test-map"
    rc = mod.main(["--root", str(tmp_path), "--out", str(out), "--work", str(tmp_path / "work")])
    assert rc == 1
    assert set(os.listdir(out)) == {"meta.json"}
    meta = _meta(out)
    assert set(meta) == META_KEYS
    assert meta["green"] is False and meta["total"] == 0


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    jobs = _workflow()["jobs"]
    return [step for job in jobs.values() for step in job.get("steps", [])]


def test_workflow_is_a_dispatch_with_a_ref_input():
    doc = _workflow()
    on = doc.get("on", doc.get(True))  # PyYAML reads the bare key ``on`` as True
    assert "workflow_dispatch" in on
    assert "ref" in on["workflow_dispatch"]["inputs"]


def test_workflow_runs_the_build_script():
    assert any("scripts/ci_map_build.py" in str(step.get("run", "")) for step in _steps())


def test_upload_step_is_named_test_map_and_always_runs():
    uploads = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["name"] == "test-map"
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "test-map"


def test_job_timeout_is_above_the_map_build_bound():
    from tools.agentops.core_settings import DEFAULTS, load

    bound = load().get("map_build_bound_minutes", DEFAULTS["map_build_bound_minutes"])
    # the build's own bound stops first; the rest of the job (runner start, upload) comes on top of it
    assert _workflow()["jobs"]["map"]["timeout-minutes"] > bound
