"""#41 unit 1: ``build_map`` writes the map the selector reads (testmondata + meta.json) from a green full run."""
import subprocess
import sys

import pytest

from tools.agentops.test_select import MapBuildError, build_map, load_testmon, map_dir, read_meta, select_for_diff

SUITE = f'"{sys.executable}" -m pytest tests -q'


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(path, *, red=False):
    """A tmp git repo: three modules, three test files (one test each, test_c's is parametrized in two)."""
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


def test_green_run_writes_map_and_meta(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    assert (mdir / "testmondata").is_file()
    meta = read_meta(mdir)
    assert meta["map_commit"] == sha and meta["last_green_commit"] == sha
    assert meta["total"] == 4 and meta["built_at"]
    deps, tests = load_testmon(str(mdir / "testmondata"))
    assert tests == {"tests/test_a.py::test_a", "tests/test_b.py::test_b",
                     "tests/test_c.py::test_c[1]", "tests/test_c.py::test_c[2]"}
    assert deps["pkg/a.py"] == {"tests/test_a.py::test_a"}
    # no temp file left; the build's full output is the one other file (#195)
    assert sorted(p.name for p in mdir.iterdir() if not p.name.startswith("build-")) == ["meta.json", "testmondata"]
    assert len(list(mdir.glob("build-*.out"))) == 1


def test_red_run_writes_nothing_and_raises(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo, red=True)
    mdir = map_dir(tmp_path / "main")
    with pytest.raises(MapBuildError) as exc:
        build_map(repo, sha, mdir, SUITE)
    assert "test_b" in str(exc.value)
    assert read_meta(mdir) is None and not (mdir / "testmondata").exists()


def test_red_run_leaves_an_existing_map_untouched(tmp_path):
    good = tmp_path / "good"
    good_sha = _repo(good)
    mdir = map_dir(tmp_path / "main")
    build_map(good, good_sha, mdir, SUITE)
    before = ((mdir / "testmondata").read_bytes(), read_meta(mdir))
    bad = tmp_path / "bad"
    bad_sha = _repo(bad, red=True)
    with pytest.raises(MapBuildError):
        build_map(bad, bad_sha, mdir, SUITE)
    assert ((mdir / "testmondata").read_bytes(), read_meta(mdir)) == before


def test_suite_dir_not_at_sha_is_refused(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # later\n")
    _git(repo, "commit", "-q", "-am", "c2")
    mdir = map_dir(tmp_path / "main")
    with pytest.raises(MapBuildError, match="not at"):
        build_map(repo, sha, mdir, SUITE)
    assert read_meta(mdir) is None


def test_selection_after_build_picks_only_the_touched_modules_tests(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # touched\n")
    _git(repo, "commit", "-q", "-am", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, sha, head, mdir)
    assert not sel.full, sel.line
    assert sel.ids == ["tests/test_a.py::test_a"]
    assert sel.total == 4 and sel.map_commit == sha
    assert sel.line == f"selected 1 of 4 tests (map {sha[:8]})"
