"""#235 unit 1 (log): every agent pytest run appends one JSON line to `pytest_runs.jsonl` in `machine_dir()`.

The seams: a real pytest run in a worktree-like folder (`wt-861-u2`, so the ticket is read from its name), and the
bare `pytest` executable run on the worktree's own pytest.ini with PYTHONPATH unset (the plugin is loaded by
tests/conftest.py's `pytest_plugins`, after `pythonpath = .` applies). The guard (`AGENTOPS_NO_RUNLOG=1`, set by merge,
line check, prequeue, the veto and the map build) writes nothing, and a log failure never fails the run."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import test_select
from tools.agentops.pytest_runs_plugin import NO_RUNLOG_ENV, issue_of, with_no_runlog

ROOT = Path(__file__).resolve().parents[1]
# the one trivial, spawn-free test the bare-pytest invocations inside tests/ (the plugin loads from tests/conftest.py)
PROBE = f"{ROOT / 'tests' / 'test_issue235_log.py'}::test_with_no_runlog_adds_the_guard_to_any_env"
KEYS = {"start", "end", "repo", "worktree", "ticket", "selected", "passed", "failed"}
TWENTY = "".join(f"def test_t{i:02d}():\n    assert True\n" for i in range(20))
FIVE_FAILS = "".join(f"def test_t{i:02d}():\n    assert {i != 5}\n" for i in range(20))
_BIN = Path(sys.executable).parent / ("Scripts" if os.name == "nt" else "")  # the bare `pytest` next to this python
PYTEST_EXE = _BIN / ("pytest.exe" if os.name == "nt" else "pytest")


def _clean_env(machine: Path, **extra: str) -> dict[str, str]:
    """The outer run's env without PYTEST_* (no outer addopts) and without PYTHONPATH and the guard."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k not in ("PYTHONPATH", NO_RUNLOG_ENV)}
    env["AGENTOPS_MACHINE_DIR"] = str(machine)
    env.update(extra)
    return env


def _run(tmp_path: Path, files: dict[str, str], targets: list[str], machine: Path,
         options: list[str] | None = None, **extra: str):
    """Bare `pytest` on a worktree-like folder `wt-861-u2` (its own empty pytest.ini; the plugin named with `-p`).
    The cwd is the worktree, never a temp folder; PYTHONPATH is the worktree so the `-p` import resolves."""
    folder = tmp_path / "wt-861-u2"
    folder.mkdir(exist_ok=True)
    (folder / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name, body in files.items():
        (folder / name).write_text(body, encoding="utf-8")
    env = _clean_env(machine, PYTHONPATH=str(ROOT), **extra)
    proc = subprocess.run([str(PYTEST_EXE), "-c", str(folder / "pytest.ini"), "--rootdir", str(folder),
                           "-p", "tools.agentops.pytest_runs_plugin", *(options or []),
                           *[str(folder / t) for t in targets]],
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    return proc, machine / "pytest_runs.jsonl"


def _one_record(log: Path) -> dict:
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def test_bare_pytest_loads_the_plugin_without_pythonpath(tmp_path: Path):
    """The worktree's own pytest.ini, bare `pytest`, PYTHONPATH unset: the plugin still loads and logs one line.
    The inner run starts a second interpreter and imports tests/conftest.py, 3 s on an idle PC and 33 s measured while
    ~65 python processes ran, so the bound is 600 s (a hang, not a slow start, is what it catches)."""
    assert PYTEST_EXE.is_file(), PYTEST_EXE
    machine = tmp_path / "machine"
    proc = subprocess.run([str(PYTEST_EXE), "-c", str(ROOT / "pytest.ini"), "--rootdir", str(ROOT), "-q", "-p", "no:cacheprovider", PROBE],
                          cwd=str(ROOT), env=_clean_env(machine), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
    rec = _one_record(machine / "pytest_runs.jsonl")
    assert (rec["worktree"], rec["selected"], rec["failed"]) == (ROOT.name, 1, 0)


def test_one_line_with_all_keys_for_a_twenty_test_run(tmp_path: Path):
    proc, log = _run(tmp_path, {"test_fake.py": TWENTY}, ["test_fake.py"], tmp_path / "machine",
                     options=["-p", "no:cacheprovider", "-q"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rec = _one_record(log)
    assert set(rec) == KEYS
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rec["start"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rec["end"])
    assert rec["worktree"] == "wt-861-u2"
    assert rec["repo"] == "wt-861-u2"  # not a git checkout: the rootdir folder name
    assert rec["ticket"] == 861
    assert (rec["selected"], rec["passed"], rec["failed"]) == (20, 20, 0)


def test_selected_is_all_collected_when_maxfail_stops_the_run(tmp_path: Path):
    proc, log = _run(tmp_path, {"test_fake.py": FIVE_FAILS}, ["test_fake.py"], tmp_path / "machine",
                     options=["-x", "-q"])
    assert proc.returncode != 0
    rec = _one_record(log)
    assert rec["selected"] == 20  # collected, not the 6 that ran before -x stopped
    assert (rec["passed"], rec["failed"]) == (5, 1)


def test_a_collection_error_counts_as_failed(tmp_path: Path):
    files = {"test_bad.py": "import no_such_module_235\n", "test_ok.py": "".join(
        f"def test_ok{i}():\n    assert True\n" for i in range(3))}
    proc, log = _run(tmp_path, files, ["test_bad.py", "test_ok.py"], tmp_path / "machine",
                     options=["--continue-on-collection-errors", "-q"])
    rec = _one_record(log)
    assert rec["failed"] >= 1
    assert (rec["selected"], rec["passed"]) == (3, 3)


def test_guard_writes_no_line(tmp_path: Path):
    proc, log = _run(tmp_path, {"test_fake.py": TWENTY}, ["test_fake.py"], tmp_path / "machine",
                     options=["-q"], **{NO_RUNLOG_ENV: "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not log.exists()


def test_unwritable_log_never_fails_the_run(tmp_path: Path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # the machine dir is a file: the append cannot succeed
    proc, _log = _run(tmp_path, {"test_fake.py": TWENTY}, ["test_fake.py"], blocker, options=["-q"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "20 passed" in proc.stdout


@pytest.mark.parametrize("name, number", [
    ("wt-i235-u1", 235), ("wt-861-u2", 861), ("i235-u1", 235), ("scratch", None), ("2026-10-19-smoke", None),
])
def test_issue_number_reads_the_folder_or_branch_name(name, number):
    assert issue_of(name) == number


def test_with_no_runlog_adds_the_guard_to_any_env():
    assert with_no_runlog({"A": "1"}) == {"A": "1", NO_RUNLOG_ENV: "1"}
    assert with_no_runlog(None) == {NO_RUNLOG_ENV: "1"}


def test_build_map_run_carries_the_guard(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_t.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "c1"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    seen: dict = {}

    def fake_run(cmd, *, cwd, env, bound):  # the map build's one suite run: record its env, report red
        seen["env"] = dict(env)
        return subprocess.CompletedProcess(cmd, 1, "", "red")

    monkeypatch.setattr(test_select, "_run_bounded", fake_run)
    with pytest.raises(test_select.MapBuildError):
        test_select.build_map(repo, sha, tmp_path / "mdir", suite_cmd="pytest")
    assert seen["env"][NO_RUNLOG_ENV] == "1"
