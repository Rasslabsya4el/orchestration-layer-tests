"""#195 unit 2 (map-output-file): a map build saves its full output to ``<mdir>/build-<sha12>-<hex>.out`` (red or green),
the raised ``MapBuildError`` and the ``map-run`` line name that file, and the last five files are kept.

Seams: ``build_map`` (``tools.agentops.test_select``), the ``map-run`` CLI (``tools.agentops.cli.main``) and
``map_run.run_map_run`` (the one ``map failed`` line)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import core_settings, merge as merge_module, map_run
from tools.agentops.cli import main
from tools.agentops.test_select import (BUILD_OUTPUT_GLOB, BUILD_OUTPUT_KEEP, MapBuildError, build_map, map_dir,
                                        read_meta)

SUITE = f'"{sys.executable}" -m pytest tests -q'
#: exits 0 and writes no testmon datafile: a build that stops after the output is saved (the retention test)
FAST_SUITE = f'"{sys.executable}" -c "pass"'
PRINTED_LINES = 60


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(path, *, red=False):
    """A tmp git repo of three modules and three test files; a red one fails ``test_b``, which prints PRINTED_LINES
    lines first, so the failing output is longer than the 40 lines the exception keeps."""
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "__init__.py").write_text("")
    for name, val in (("a", 1), ("b", 2), ("c", 3)):
        (path / "pkg" / f"{name}.py").write_text(f"def {name}():\n    return {val}\n")
    (path / "tests" / "test_a.py").write_text("from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n")
    if red:
        (path / "tests" / "test_b.py").write_text(
            "from pkg.b import b\n\ndef test_b():\n"
            f"    for i in range({PRINTED_LINES}):\n        print(f'line {{i:02d}}')\n"
            "    assert b() == 3\n")
    else:
        (path / "tests" / "test_b.py").write_text("from pkg.b import b\n\ndef test_b():\n    assert b() == 2\n")
    (path / "tests" / "test_c.py").write_text(
        "import pytest\nfrom pkg.c import c\n\n@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_c(x):\n    assert c() == 3\n")
    (path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c1")
    return _git(path, "rev-parse", "HEAD")


def _outputs(mdir: Path) -> list[Path]:
    return sorted(mdir.glob(BUILD_OUTPUT_GLOB))


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


def _output_path_of(line: str) -> Path:
    match = re.search(r" \| output (.+)$", line)
    assert match, line
    return Path(match.group(1))


def test_red_run_saves_the_full_output_and_the_error_names_it(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo, red=True)
    mdir = map_dir(tmp_path / "main")
    with pytest.raises(MapBuildError) as exc:
        build_map(repo, sha, mdir, SUITE)
    files = _outputs(mdir)
    assert len(files) == 1 and files[0].name.startswith(f"build-{sha[:12]}-"), files
    text = files[0].read_text(encoding="utf-8")
    assert "line 00" in text and f"line {PRINTED_LINES - 1:02d}" in text  # all of it, not the last 40 lines
    assert "FAILED tests/test_b.py::test_b" in text
    assert f"full output: {files[0].resolve()}" in str(exc.value), str(exc.value)
    assert read_meta(mdir) is None and not (mdir / "testmondata").exists()


def test_green_run_saves_the_output_too(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    files = _outputs(mdir)
    assert len(files) == 1, files
    assert "passed" in files[0].read_text(encoding="utf-8")
    assert read_meta(mdir)["map_commit"] == sha


def test_red_map_run_line_names_the_output_file(tmp_path, full_run_env):
    repo = tmp_path / "repo"
    sha = _repo(repo, red=True)
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc != 0
    lines = _log_lines(full_run_env)
    assert len(lines) == 1, lines
    assert re.fullmatch(rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| safety run: red tests/test_b\.py::test_b"
                        rf" \| {sha[:12]} \| output .+", lines[0]), lines[0]
    out = _output_path_of(lines[0])
    assert out.is_file() and "line 00" in out.read_text(encoding="utf-8")
    assert out.parent == map_dir(repo).resolve()


def test_green_map_run_built_line_names_the_output_file(tmp_path, full_run_env):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc == 0
    lines = _log_lines(full_run_env)
    assert len(lines) == 2, lines  # the built line, then the safety run's green line
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| map built \| .*testmondata \| "
                        rf"{sha[:12]} \| 4 tests \| output .+", lines[0]), lines[0]
    assert _output_path_of(lines[0]).is_file()
    assert lines[1].endswith(f"safety run: green | {sha[:12]}")


def test_failed_map_run_line_names_the_output_file_when_one_was_written(tmp_path, full_run_env, monkeypatch):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    out = map_dir(repo).resolve() / f"build-{sha[:12]}-test.out"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("suite output\n", encoding="utf-8")

    def no_datafile(*args, **kwargs):
        raise MapBuildError("suite was green but testmon wrote no datafile (is pytest-testmon installed?)", output=out)

    monkeypatch.setattr(map_run, "build_map", no_datafile)
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc != 0
    lines = _log_lines(full_run_env)
    assert len(lines) == 1, lines
    assert lines[0].endswith(f"map failed | {sha[:12]} | suite was green but testmon wrote no datafile "
                             f"(is pytest-testmon installed?) | output {out}"), lines[0]


def test_twenty_one_builds_keep_the_last_twenty_output_files(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    made = []
    for _ in range(BUILD_OUTPUT_KEEP + 1):
        before = set(_outputs(mdir))
        with pytest.raises(MapBuildError):  # FAST_SUITE writes no datafile; the output is saved all the same
            build_map(repo, sha, mdir, FAST_SUITE)
        made.append((set(_outputs(mdir)) - before).pop())  # the one file this build wrote
    files = _outputs(mdir)
    assert len(files) == BUILD_OUTPUT_KEEP == 20, files
    assert made[-1] in files  # the newest build's file is always kept
    assert made[0] not in files  # the oldest one is deleted


def test_a_failure_after_the_write_still_names_the_output_file(tmp_path, full_run_env, monkeypatch):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    def unreadable(path):
        raise OSError("testmon datafile unreadable")

    monkeypatch.setattr("tools.agentops.test_select.load_testmon", unreadable)
    with pytest.raises(OSError) as exc:
        build_map(repo, sha, map_dir(tmp_path / "main"), SUITE)
    assert exc.value.output.is_file() and "passed" in exc.value.output.read_text(encoding="utf-8")
    rc = main(["map-run", "--commit", sha, "--cwd", str(repo), "--detach"])
    assert rc != 0
    lines = _log_lines(full_run_env)
    assert len(lines) == 1, lines
    out = _output_path_of(lines[0])
    assert out.is_file() and out.parent == map_dir(repo).resolve()
    assert lines[0].endswith(f"map failed | {sha[:12]} | testmon datafile unreadable | output {out}"), lines[0]
