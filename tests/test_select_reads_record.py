"""#195 unit 1: the merge-suite run records, per test id, the repo files it opened (``suite_reads_plugin``), and
``build_map`` keeps the dump next to the testmon map (replaced on a green run only)."""
import os
import shutil
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from tools.agentops.test_select import BUILD_OUTPUT_GLOB, build_map, load_reads, map_dir
from tools.agentops.util import nested_env, no_window_flags

HOME = Path(__file__).resolve().parents[1]
PLUGIN_REL = Path("tools") / "agentops" / "suite_reads_plugin.py"
SUITE = f'"{sys.executable}" -m pytest tests -q'

TESTS = '''\
import os
import pytest

OUTSIDE = {outside!r}

@pytest.fixture
def fx():
    with open("docs/agents/fixture.md", encoding="utf-8") as fh:
        fh.read()

def test_a():
    with open("docs/agents/a.md", encoding="utf-8") as fh:
        assert fh.read() == "a"

def test_b():
    assert 1 == 1

def test_c():
    with open("helper.py", encoding="utf-8") as fh:
        fh.read()
    with open(OUTSIDE, encoding="utf-8") as fh:
        fh.read()

def test_d(fx):
    assert sorted(os.listdir("docs/agents"))
'''


def _project(path: Path, outside: Path, *, plugin_copy: bool = False) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "docs" / "agents").mkdir(parents=True)
    (path / "docs" / "agents" / "a.md").write_text("a", encoding="utf-8")
    (path / "docs" / "agents" / "fixture.md").write_text("f", encoding="utf-8")
    (path / "helper.py").write_text("X = 1\n", encoding="utf-8")
    outside.write_text("o", encoding="utf-8")
    (path / "tests" / "test_x.py").write_text(TESTS.format(outside=str(outside)), encoding="utf-8")
    (path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n", encoding="utf-8")
    if plugin_copy:
        (path / PLUGIN_REL).parent.mkdir(parents=True)
        shutil.copy(HOME / PLUGIN_REL, path / PLUGIN_REL)
    return path


def _run(project: Path, reads_dir: Path | None, *extra: str):
    env = nested_env()  # #195: no outer build's PYTEST_ADDOPTS / SUITE_READS_DIR / TESTMON_DATAFILE
    env["PYTHONPATH"] = str(HOME)
    if reads_dir is not None:
        env["SUITE_READS_DIR"] = str(reads_dir)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "tools.agentops.suite_reads_plugin",
                           "-p", "no:cacheprovider", *extra], cwd=str(project), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", creationflags=no_window_flags(),
                          timeout=120)


def test_dump_attributes_repo_files_to_the_test_that_opened_them(tmp_path):
    project = _project(tmp_path / "proj", tmp_path / "outside.txt")
    reads = tmp_path / "reads"
    run = _run(project, reads)
    assert run.returncode == 0, run.stdout + run.stderr
    files, dirs = load_reads(str(reads))
    x = "tests/test_x.py::"
    assert files["docs/agents/a.md"] == {x + "test_a"}
    assert files["docs/agents/fixture.md"] == {x + "test_d"}       # a fixture's open is its test's
    assert dirs["docs/agents"] == {x + "test_d"}


def test_python_files_and_paths_outside_the_repo_are_not_recorded(tmp_path):
    project = _project(tmp_path / "proj", tmp_path / "outside.txt")
    reads = tmp_path / "reads"
    assert _run(project, reads).returncode == 0
    files, _ = load_reads(str(reads))
    assert "helper.py" not in files and "tests/test_x.py" not in files
    assert not any(f.endswith((".py", ".pyc")) or ".git/" in f or "__pycache__" in f for f in files)
    assert not any("outside" in f for f in files)
    assert set(files) == {"docs/agents/a.md", "docs/agents/fixture.md"}


def test_without_the_env_var_nothing_is_written(tmp_path):
    project = _project(tmp_path / "proj", tmp_path / "outside.txt")
    assert _run(project, None).returncode == 0
    assert not list(tmp_path.glob("**/reads-*.json"))


def test_each_xdist_worker_writes_its_own_dump(tmp_path):
    pytest.importorskip("xdist")
    project = _project(tmp_path / "proj", tmp_path / "outside.txt")
    reads = tmp_path / "reads"
    run = _run(project, reads, "-n", "2")
    assert run.returncode == 0, run.stdout + run.stderr
    dumps = sorted(p.name for p in reads.glob("reads-*.json"))
    assert len(dumps) >= 1 and len(set(dumps)) == len(dumps)
    files, _ = load_reads(str(reads))
    assert files["docs/agents/a.md"] == {"tests/test_x.py::test_a"}


# --- build_map keeps the dump next to the map -------------------------------------------------------------------

def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, timeout=120,
                          creationflags=no_window_flags()).stdout.strip()


def _names(mdir: Path) -> list[str]:
    """The map dir's entries, the saved build outputs left out (#195: ``_outputs`` counts those)."""
    return sorted(p.name for p in mdir.iterdir() if not fnmatchcase(p.name, BUILD_OUTPUT_GLOB))


def _outputs(mdir: Path) -> list[str]:
    """The saved build outputs ``build-*.out`` in the map dir (#195)."""
    return [p.name for p in mdir.iterdir() if fnmatchcase(p.name, BUILD_OUTPUT_GLOB)]


def _repo(path: Path, *, red: bool = False, plugin: bool = True) -> str:
    _project(path, path.parent / f"{path.name}-outside.txt", plugin_copy=plugin)
    (path / "tests" / "test_x.py").write_text(
        'def test_a():\n    open("docs/agents/a.md").read()\n\ndef test_b():\n    assert %s\n' % (not red),
        encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c1")
    return _git(path, "rev-parse", "HEAD")


def test_green_build_replaces_the_reads_dir(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    (mdir / "reads").mkdir(parents=True)
    (mdir / "reads" / "reads-old.json").write_text('{"files": {"t": ["old.md"]}, "dirs": {}}', encoding="utf-8")
    build_map(repo, sha, mdir, SUITE)
    files, _ = load_reads(str(mdir / "reads"))
    assert files == {"docs/agents/a.md": {"tests/test_x.py::test_a"}}
    assert _names(mdir) == ["meta.json", "reads", "testmondata"]   # no temp left
    assert len(_outputs(mdir)) == 1                                  # the build's saved output (#195)


def test_red_build_leaves_the_existing_reads_dir_untouched(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo, red=True)
    mdir = map_dir(tmp_path / "main")
    (mdir / "reads").mkdir(parents=True)
    old = '{"files": {"t": ["old.md"]}, "dirs": {}}'
    (mdir / "reads" / "reads-old.json").write_text(old, encoding="utf-8")
    with pytest.raises(Exception, match="map not written"):
        build_map(repo, sha, mdir, SUITE)
    assert [p.name for p in (mdir / "reads").iterdir()] == ["reads-old.json"]
    assert (mdir / "reads" / "reads-old.json").read_text(encoding="utf-8") == old
    assert _names(mdir) == ["reads"]                                                        # temp dir removed
    assert len(_outputs(mdir)) == 1                                                         # the failed build's output (#195)


def test_green_build_without_the_plugin_removes_the_old_reads_dir(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo, plugin=False)
    mdir = map_dir(tmp_path / "main")
    (mdir / "reads").mkdir(parents=True)
    (mdir / "reads" / "reads-old.json").write_text('{"files": {"t": ["old.md"]}, "dirs": {}}', encoding="utf-8")
    build_map(repo, sha, mdir, SUITE)
    assert _names(mdir) == ["meta.json", "testmondata"]
    assert len(_outputs(mdir)) == 1                                  # the build's saved output (#195)
