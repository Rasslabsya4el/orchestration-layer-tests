"""#195 send-back 1, unit 1 (scrub-nested): a nested run never inherits the outer build's PYTEST_ADDOPTS,
SUITE_READS_DIR or TESTMON_DATAFILE. The outer run sets them as ``build_map`` does; a nested ``build_map`` on a
temp repo (whose checkout has no ``tools`` package) must still succeed and must see none of the outer values."""
import json
import os
import subprocess
import sys

from tools.agentops.test_select import build_map, map_dir, read_meta

OUTER = {
    "PYTEST_ADDOPTS": "-p tools.agentops.suite_reads_plugin",
    "SUITE_READS_DIR": "C:/outer/reads.build-1",
    "TESTMON_DATAFILE": "C:/outer/testmondata.build-1",
}
SUITE = f'"{sys.executable}" -m pytest tests -q'

# the trivial suite of the temp repo: it writes the three variables it sees to the file named by ENV_DUMP
TEST_BODY = '''import json
import os
from pathlib import Path


def test_env():
    seen = {k: os.environ.get(k) for k in ("PYTEST_ADDOPTS", "SUITE_READS_DIR", "TESTMON_DATAFILE")}
    Path(os.environ["ENV_DUMP"]).write_text(json.dumps(seen), encoding="utf-8")
'''


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _trivial_repo(path):
    (path / "tests").mkdir(parents=True)
    (path / "tests" / "test_env.py").write_text(TEST_BODY, encoding="utf-8")
    (path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c1")
    return _git(path, "rev-parse", "HEAD")


def test_nested_build_map_succeeds_and_sees_none_of_the_outer_values(tmp_path, monkeypatch):
    for key, value in OUTER.items():
        monkeypatch.setenv(key, value)  # as build_map sets them for the outer suite
    dump = tmp_path / "seen.json"
    monkeypatch.setenv("ENV_DUMP", str(dump))
    repo = tmp_path / "repo"
    sha = _trivial_repo(repo)
    mdir = map_dir(tmp_path / "main")

    build_map(repo, sha, mdir, SUITE)

    assert read_meta(mdir)["map_commit"] == sha
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert seen["PYTEST_ADDOPTS"] is None  # the temp repo has no plugin file, so build_map adds none
    assert seen["SUITE_READS_DIR"] is None
    assert seen["TESTMON_DATAFILE"] not in (None, OUTER["TESTMON_DATAFILE"])  # its own datafile, not the outer one
    for key, value in OUTER.items():  # build_map leaves the outer process' environment as it was
        assert os.environ[key] == value


def test_nested_env_drops_the_three_build_variables_and_keeps_the_rest(monkeypatch):
    from tools.agentops.util import nested_env
    for key, value in OUTER.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ISSUE195_KEEP", "1")
    env = nested_env({"X": "2"})
    assert not set(OUTER) & set(env)
    assert env["ISSUE195_KEEP"] == "1" and env["X"] == "2"
