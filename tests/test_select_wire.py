"""#41 unit 2: the post-landing full run builds the test map at the landed commit and logs it.

Seams: the ``map-run`` CLI (``tools.agentops.cli.main``), ``run_merge`` / ``run_commit`` (the spawn after a landing),
and ``merge._plan_selection`` (a selection that reads the map the full run built)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import core_settings, merge as merge_module
from tools.agentops.cli import main
from tools.agentops.merge import run_commit, run_merge
from tools.agentops.test_select import map_dir, read_meta

SUITE = f'"{sys.executable}" -m pytest tests -q'
PYTEST_VERSION = f'"{sys.executable}" -m pytest --version'


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(path, *, red=False):
    """A tmp git repo: three modules, three test files (test_c parametrized in two: four tests)."""
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "__init__.py").write_text("")
    for name, val in (("a", 1), ("b", 2), ("c", 3)):
        (path / "pkg" / f"{name}.py").write_text(f"def {name}():\n    return {val}\n")
    (path / "tests" / "test_a.py").write_text("from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n")
    (path / "tests" / "test_b.py").write_text(
        f"from pkg.b import b\n\ndef test_b():\n    assert b() == {3 if red else 2}\n")
    (path / "tests" / "test_c.py").write_text(
        "import pytest\nfrom pkg.c import c\n\n@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_c(x):\n    assert c() == 3\n")
    (path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c1")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def full_run_env(tmp_path, monkeypatch):
    """The merge log in tmp, the machine not held, the merge suite a plain pytest command."""
    log = tmp_path / "merge.log"
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(log))
    monkeypatch.setattr("tools.agentops.map_run.machine_holds", lambda: False)
    real_load = core_settings.load  # the extension loader reads the real keys too: only merge_suite is overridden
    monkeypatch.setattr(core_settings, "load", lambda path=None: {**real_load(path), "merge_suite": SUITE})
    return log


def _log_lines(log: Path) -> list[str]:
    """The merge-log lines of the run, without the place line (#343: asserted in test_issue343_map_run_place)."""
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [line for line in lines if " | map-run | map-build on " not in line]


def test_53_map_run_is_the_core_subcommand_and_full_run_is_gone():
    """#53 unit 3a: the core's subcommand is ``map-run`` (no target names it); ``full-run`` is the target's, not the core's."""
    with pytest.raises(SystemExit) as shown:
        main(["map-run", "--help"])
    assert shown.value.code == 0
    with pytest.raises(SystemExit) as gone:
        main(["full-run", "--help"])
    assert gone.value.code == 2


def test_a_full_run_builds_the_map_and_logs_one_built_line(tmp_path, full_run_env):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc == 0
    mdir = map_dir(repo)
    meta = read_meta(mdir)
    assert meta is not None and meta["map_commit"] == sha and meta["total"] == 4
    lines = _log_lines(full_run_env)
    assert len(lines) == 2, lines  # #195: the built line, then the safety run's green line
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| map built \| .*testmondata \| "
                        rf"{sha[:12]} \| 4 tests \| output .+", lines[0]), lines[0]
    assert re.fullmatch(rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| safety run: green \| {sha[:12]}",
                        lines[1]), lines[1]


def test_b_a_red_suite_logs_map_failed_and_writes_no_meta(tmp_path, full_run_env):
    repo = tmp_path / "repo"
    sha = _repo(repo, red=True)
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc != 0
    assert read_meta(map_dir(repo)) is None
    lines = _log_lines(full_run_env)
    assert len(lines) == 1, lines
    assert re.fullmatch(rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| safety run: red tests/test_b\.py::test_b \| {sha[:12]} \| output .+",
                        lines[0]), lines[0]  # #195: a red suite is the safety run's red line, with its failing ids


def _world(tmp_path: Path):
    """A bare origin whose main holds a small package and its tests; returns (bare, checkout, lock)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "pkg").mkdir()
    (seed / "tests").mkdir()
    (seed / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (seed / "tests" / "test_a.py").write_text("from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _branch(tmp_path: Path, bare: Path, name: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / "pkg" / "a.py").write_text("def a():\n    return 2\n")
    _git(worker, "commit", "-q", "-am", "change a")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


@pytest.fixture
def landing_env(tmp_path, monkeypatch):
    """The merge's own paths in tmp, the selection on, the spawn faked (recorded, not started)."""
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    monkeypatch.setattr(merge_module, "_plan_selection",
                        lambda cwd, base, sha, run_cmd, out: (run_cmd, "selected 1 of 4 (map 00000000)", False))
    calls: list[tuple] = []
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: calls.append((cwd, sha)) or "")
    return calls


def test_c_a_selected_landing_spawns_the_full_run(tmp_path, landing_env):
    bare, checkout, lock = _world(tmp_path)
    ref = _branch(tmp_path, bare, "feat")
    result = run_merge(ref, PYTEST_VERSION, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.pushed, result
    main_sha = _git(bare, "rev-parse", "main")
    assert len(landing_env) == 1, landing_env
    assert landing_env[0][1] == main_sha


def test_e_a_green_commit_gate_that_pushes_spawns_the_full_run(tmp_path, landing_env):
    bare, checkout, lock = _world(tmp_path)
    (checkout / "pkg" / "a.py").write_text("def a():\n    return 3\n")
    result = run_commit(["pkg/a.py"], "change a", cwd=checkout, lock_path=lock, lock_timeout=60,
                        suite_cmd=PYTEST_VERSION, suite_output_dir=tmp_path / "suite_out",
                        merge_log_path=tmp_path / "merge.log")
    assert result.ok and result.pushed, result
    main_sha = _git(bare, "rev-parse", "main")
    assert len(landing_env) == 1, landing_env
    assert landing_env[0][1] == main_sha


def test_d_a_selection_after_the_full_run_reads_the_new_map(tmp_path, full_run_env, monkeypatch):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo = tmp_path / "repo"
    sha1 = _repo(repo)
    assert main(["map-run", "--commit", sha1, "--cwd", str(repo), "--detach"]) == 0
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # touched\n")
    _git(repo, "commit", "-q", "-am", "c2")
    sha2 = _git(repo, "rev-parse", "HEAD")
    run_cmd = f'"{sys.executable}" -m pytest tests -q'
    cmd, line, skip = merge_module._plan_selection(repo, sha1, sha2, run_cmd, tmp_path / "out.txt")
    assert skip is False and '"@' in cmd, (cmd, line)
    # the line: ``selected N of T tests (map <sha8>)``; with K tests added since the map (#47 unit B)
    # it reads ``selected N of T tests in the map (+K added since, map <sha8>)``
    m = re.fullmatch(r"selected (\d+) of (\d+) (?:tests )?(?:in the map )?"
                     r"\((?:\+\d+ added since, )?map ([0-9a-f]{8})\)", line)
    assert m, line
    assert int(m.group(1)) < int(m.group(2)) == 4
    assert m.group(3) == sha1[:8]
