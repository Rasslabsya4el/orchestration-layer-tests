"""#352 unit 4 (row 5): a direct ``pytest`` call by an agent in a worktree, of a selection the rule sends to GitHub, runs on
GitHub or is refused with the form to use; a short selection stays on the PC.

Seams: ``test_run.route_selection`` / ``test_run.run_test_run`` with a fake ``Seams`` (settings, clock, collect, Deps,
dirty tree, untracked files, GitHub send, PC run are all injected in tests a-d, l-o), and the worker guard's ``run(payload)``
with its decision seam ``worker_process_guard._decide`` patched in tests a-c. Tests g-h run the guard's real decision
path (``GUARD_SEAMS`` injects only settings, clock, a fixed busy PC line and the GitHub plan) over a tmp git repo and a
tmp durations datafile; test i is the pure reader of the pytest words; tests j-k cover untracked files, j with the
fake seam and k with real git. Tests e, f, m, n, o start a child process in tmp_path (a tiny test, or a ``pytest.py``
that sleeps for the bounds). No network; the only disk is tmp_path.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.agentops import machine_berths, merge_github, test_run
from tools.agentops.util import no_window_flags
from tools.agentops.github_suite import GithubSuiteResult
from tools.agentops.place_decision import Deps

HOOKS_DIR = Path(__file__).resolve().parents[1] / "tools" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
import worker_process_guard as guard  # noqa: E402

PLAN = merge_github.Plan(remote="https://example.invalid/copy.git", own_repo=False, repo_slug="o/r", repo_name="r")
ON = {"github_suite": True, "github_start_seconds_linux": 90, "github_start_seconds_windows": 150}
IDS = [f"tests/test_x.py::t{i}" for i in range(10)]
NOW = 1_000_000.0
COMMAND = "python -m pytest tests/test_x.py"


def fake_deps(run: float, wait: float = 1800.0, needs: tuple = ()) -> Deps:
    return Deps(
        plan_for=lambda: (PLAN, ""),
        pc_run_seconds=lambda ids: run,
        pc_wait_seconds=lambda now: wait,
        needs_pc=lambda ids: [i for i in ids if i in needs],
    )


def fake_seams(run: float, *, github=None, pc_run=None, dirty: bool = False, wait: float = 1800.0,
               untracked=None) -> test_run.Seams:
    return test_run.Seams(
        load_settings=lambda: dict(ON),
        clock=lambda: NOW,
        collect=lambda cwd, args, timeout_s: list(IDS),
        deps=fake_deps(run, wait),
        dirty=lambda cwd: dirty,
        untracked=untracked or (lambda cwd, args: []),
        head=lambda cwd: "abc1234567890",
        pc_only_files=lambda cwd: [],
        github_run=github,
        pc_run=pc_run,
    )


def payload(tmp_path: Path, command: str = COMMAND) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "agent_id": "a", "agent_type": "worker",
            "cwd": str(tmp_path)}


@pytest.fixture(autouse=True)
def hook_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / "guard.log"))


# -- (a) a selection decided GitHub: the guard refuses the direct call with the form; test run takes GitHub -------------

def test_a_guard_refuses_direct_pytest_of_a_github_selection_with_the_form(tmp_path, monkeypatch):
    route = test_run.route_selection(["tests/test_x.py"], tmp_path, fake_seams(600.0))
    assert route.where == "github"
    monkeypatch.setattr(guard, "_decide", lambda args, cwd: route)
    out = guard.run(payload(tmp_path))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not reason.startswith("permission guard, red line")
    assert reason.startswith("process guard: a long pytest selection runs through the decision point; "
                             "use python -m tools.agentops test run tests/test_x.py")


def test_a_test_run_takes_the_github_path_and_prints_it(tmp_path, capsys):
    sent = []

    def fake_github(plan, **kw):
        sent.append(list(kw["ids"]))
        return GithubSuiteResult(status="green")

    def no_pc(args, cwd):
        raise AssertionError("the PC must not run a GitHub selection")

    rc = test_run.run_test_run(["tests/test_x.py"], cwd=tmp_path,
                               seams=fake_seams(600.0, github=fake_github, pc_run=no_pc))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0].startswith("test run: GitHub (GitHub ")
    assert sent == [IDS]
    assert "passed on GitHub: 10 ids" in out


def test_a_github_refusal_falls_back_to_the_pc_in_one_line(tmp_path, capsys):
    ran = []

    def refused(plan, **kw):
        return GithubSuiteResult(status="fallback", reason="machines did not start")

    def pc(args, cwd):
        ran.append(list(args))
        return 0

    rc = test_run.run_test_run(["tests/test_x.py"], cwd=tmp_path,
                               seams=fake_seams(600.0, github=refused, pc_run=pc))
    out = capsys.readouterr().out
    assert rc == 0
    assert ran == [["tests/test_x.py"]]
    assert "test run: GitHub fallback to PC (machines did not start)" in out


# -- (b) a short selection: the guard allows it; test run runs the PC and says so first ------------------------------

def test_b_short_selection_is_allowed_by_the_guard(tmp_path, monkeypatch):
    route = test_run.route_selection(["tests/test_x.py"], tmp_path, fake_seams(40.0))
    assert route.where == "pc"
    monkeypatch.setattr(guard, "_decide", lambda args, cwd: route)
    assert guard.run(payload(tmp_path)) is None


def test_b_test_run_of_a_short_selection_runs_the_pc_with_the_line_first(tmp_path, capsys):
    ran = []

    def pc(args, cwd):
        ran.append(list(args))
        return 0

    def no_github(plan, **kw):
        raise AssertionError("a short run never goes to GitHub")

    rc = test_run.run_test_run(["tests/test_x.py"], cwd=tmp_path,
                               seams=fake_seams(40.0, github=no_github, pc_run=pc))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert ran == [["tests/test_x.py"]]
    assert out[0] == "test run: PC (run 40 s < floor 2 min)"


# -- (c) a failure inside the decision: the call is allowed ---------------------------------------------------------

def test_c_guard_failure_inside_the_decision_allows_the_call(tmp_path, monkeypatch):
    def broken(args, cwd):
        raise RuntimeError("durations unreadable")

    monkeypatch.setattr(guard, "_decide", broken)
    assert guard.run(payload(tmp_path)) is None


def test_c_a_decision_past_the_bound_allows_the_call(tmp_path, monkeypatch):
    def slow(args, cwd):
        time.sleep(2.0)
        return None

    monkeypatch.setattr(guard, "_decide", slow)
    monkeypatch.setattr(guard, "DECIDE_BOUND_S", 0.2)
    assert guard.run(payload(tmp_path)) is None


# -- the selection the guard reads: the pytest words of a chained command, quoted words kept whole ------------------

def test_guard_reads_the_pytest_selection_after_a_chain(tmp_path, monkeypatch):
    seen = []

    def spy(args, cwd):
        seen.append(list(args))
        return None

    monkeypatch.setattr(guard, "_decide", spy)
    guard.run(payload(tmp_path, 'echo start && python -m pytest tests/test_x.py -k "a and b" -q'))
    assert seen == [["tests/test_x.py", "-k", "a and b", "-q"]]


def test_guard_does_not_read_a_mention_of_pytest(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(guard, "_decide", lambda args, cwd: seen.append(args) or None)
    assert guard.run(payload(tmp_path, "grep -rn pytest tests/")) is None
    assert seen == []


# -- the CLI words: pytest's own leading option (-q) reaches the run, not argparse's extras -----------------------------

def test_d_leading_dash_q_reaches_test_run_through_the_cli(tmp_path, monkeypatch):
    from tools.agentops import cli

    seen = []
    monkeypatch.setattr(test_run, "run_test_run", lambda args, cwd, seams=None: seen.append(list(args)) or 0)
    rc = cli.main(["test", "run", "-q", "tests/test_issue352_place_decision.py"])
    assert rc == 0
    assert seen == [["-q", "tests/test_issue352_place_decision.py"]]


def test_d_trailing_dash_q_and_help_keep_their_meaning(tmp_path, monkeypatch, capsys):
    from tools.agentops import cli

    seen = []
    monkeypatch.setattr(test_run, "run_test_run", lambda args, cwd, seams=None: seen.append(list(args)) or 0)
    cli.main(["test", "run", "tests/test_issue352_place_decision.py", "-q"])
    assert seen == [["tests/test_issue352_place_decision.py", "-q"]]
    with pytest.raises(SystemExit) as stop:
        cli.main(["test", "run", "--help"])
    assert stop.value.code == 0
    assert "pytest's own words" in capsys.readouterr().out


# -- the selection's own verbosity does not change the collect; the PC run shows pytest's output -----------------------

def _tiny_selection(tmp_path: Path) -> None:
    (tmp_path / "test_tiny.py").write_text("def test_one():\n    pass\n", encoding="utf-8")


@pytest.mark.parametrize("words", [["-q"], ["-v"], ["--quiet"]])
def test_e_collect_ignores_the_verbosity_the_caller_gave(tmp_path, words):
    _tiny_selection(tmp_path)
    assert test_run.collect_args(tmp_path, [*words, "test_tiny.py"], 60) == ["test_tiny.py::test_one"]


def test_e_the_pc_run_shows_pytest_output(tmp_path, capfd):
    _tiny_selection(tmp_path)
    rc = test_run._run_pc(["-q", "test_tiny.py"], tmp_path)
    assert rc == 0
    assert "1 passed" in capfd.readouterr().out


def test_f_the_place_line_comes_before_the_pc_output_in_a_redirected_run(tmp_path):
    # the real CLI, its stdout redirected to a file as an agent's shell does: the place line (buffered in Python)
    # must be flushed before pytest's own output, which the child writes straight to the file descriptor
    _tiny_selection(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "out.txt"
    with log.open("w", encoding="utf-8") as out:
        rc = subprocess.run([sys.executable, "-m", "tools.agentops", "test", "run", "-q", "test_tiny.py"],
                            cwd=str(tmp_path), stdout=out, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONPATH": str(repo)}, creationflags=no_window_flags()).returncode
    lines = log.read_text(encoding="utf-8").splitlines()
    assert rc == 0
    assert lines[0].startswith("test run: PC (")
    assert "1 passed" in lines[-1]


# -- (g)-(k) the guard's real decision path: ids from the stored durations, a tmp git repo, a fixed busy PC line -------

def _git_repo(path: Path) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "tests" / "x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    git = ["git", "-C", str(path)]
    for words in (["init", "-q"], ["add", "tests/x.py"],
                  ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run([*git, *words], check=True, capture_output=True, creationflags=no_window_flags())
    return path


def _durations(path: Path, rows: dict) -> Path:
    """A testmon-shaped datafile: the table ``test_execution`` read by ``suite_plan_plugin.stored_durations``."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE test_execution (test_name TEXT, duration REAL)")
    con.executemany("INSERT INTO test_execution VALUES (?, ?)", list(rows.items()))
    con.commit()
    con.close()
    return path


def real_guard_seams(**kw) -> test_run.Seams:
    """The guard's real decision: settings, clock, Deps (a fixed busy PC line, no GitHub plan read) injected; the ids
    come from the stored durations, so ``collect`` must never run."""
    def no_collect(cwd, args, timeout_s):
        raise AssertionError("the guard's decision collects nothing: its ids come from the stored durations")

    deps = Deps(plan_for=lambda: (PLAN, ""), pc_wait_seconds=lambda now: 1800.0, needs_pc=lambda ids: [])
    return test_run.Seams(load_settings=lambda: dict(ON), clock=lambda: NOW, collect=no_collect, deps=deps, **kw)


def test_g_real_guard_refuses_a_long_selection_read_from_the_stored_durations(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    durations = _durations(tmp_path / "durations.sqlite", {f"tests/x.py::t{i}": 60.0 for i in range(10)})
    monkeypatch.setenv("SUITE_DURATIONS_FILE", str(durations))
    monkeypatch.setattr(guard, "GUARD_SEAMS", real_guard_seams(), raising=False)
    out = guard.run(payload(repo, "python -m pytest tests/x.py"))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("process guard:")
    assert "python -m tools.agentops test run tests/x.py" in reason


def test_h_real_guard_allows_a_short_selection_an_unknown_argument_and_bare_pytest(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    durations = _durations(tmp_path / "durations.sqlite", {"tests/x.py::t0": 10.0, "tests/short.py::t0": 10.0})
    monkeypatch.setenv("SUITE_DURATIONS_FILE", str(durations))
    monkeypatch.setattr(guard, "GUARD_SEAMS", real_guard_seams(), raising=False)
    assert guard.run(payload(repo, "python -m pytest tests/short.py")) is None          # 10 s: under the floor
    assert guard.run(payload(repo, "python -m pytest tests/nothing_stored.py")) is None  # matches nothing: unknown
    assert guard.run(payload(repo, "python -m pytest")) is None                         # bare pytest
    assert guard.run(payload(repo, "python -m pytest -q")) is None                      # options only


def test_i_pytest_selection_drops_redirections_and_the_pipe_tail():
    assert guard.pytest_selection("python -m pytest tests/x.py 2>&1 | tail") == ["tests/x.py"]
    assert guard.pytest_selection("python -m pytest tests/x.py >out.txt") == ["tests/x.py"]
    assert guard.pytest_selection("python -m pytest tests/x.py 2>/dev/null -q") == ["tests/x.py", "-q"]
    assert guard.pytest_selection('python -m pytest tests/x.py -k "a and b" > log 2>&1') == \
        ["tests/x.py", "-k", "a and b"]


# -- (j)-(k) untracked files under the selected path: GitHub would test the commit, not the tree -> the PC ---------------

def test_j_untracked_file_under_the_selection_sends_the_run_to_the_pc(tmp_path):
    seams = fake_seams(600.0, untracked=lambda cwd, args: ["tests/test_new.py"])
    route = test_run.route_selection(["tests/test_x.py"], tmp_path, seams)
    assert route.where == "pc"
    assert route.reason.startswith("untracked files under the selection")
    assert "tests/test_new.py" in route.reason


def test_k_real_git_untracked_file_under_a_selected_folder_goes_to_the_pc(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "tests" / "y_new.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    seams = fake_seams(600.0)
    seams.untracked = None  # the real git check
    seams.dirty = None      # the real tracked-changes check
    under = test_run.route_selection(["tests/"], repo, seams)
    assert under.where == "pc"
    assert "tests/y_new.py" in under.reason
    outside = test_run.route_selection(["tests/x.py"], repo, seams)
    assert outside.where == "github"  # the untracked file is not under the selection


# -- (l) GitHub did not collect one id: that id on the PC, the rest again on GitHub; never the whole selection on the PC --

def test_l_one_not_collected_id_runs_on_the_pc_and_the_rest_again_on_github(tmp_path, capsys):
    sent, pc_calls = [], []
    stuck = IDS[3]

    def github(plan, **kw):
        sent.append(list(kw["ids"]))
        if len(sent) == 1:
            return GithubSuiteResult(status="red", not_collected=stuck)
        return GithubSuiteResult(status="green")

    def pc(args, cwd):
        pc_calls.append(list(args))
        return 0

    rc = test_run.run_test_run(["tests/test_x.py"], cwd=tmp_path, seams=fake_seams(600.0, github=github, pc_run=pc))
    out = capsys.readouterr().out
    assert rc == 0
    assert pc_calls == [[stuck]]                      # one id on the PC; the whole selection never ran there
    assert sent == [IDS, [i for i in IDS if i != stuck]]
    assert f"not collected on GitHub: {stuck}" in out
    assert "passed on GitHub: 9 ids" in out


# -- (m)-(o) bounded waits: a PC run past its bound is killed, exits 1 and says so in one line -----------------------------

def _sleeping_pytest(tmp_path: Path, seconds: float = 30.0) -> None:
    """``python -m pytest`` in tmp_path imports this file first (the cwd leads sys.path): a child that sleeps."""
    (tmp_path / "pytest.py").write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")


def test_m_a_pc_run_past_its_bound_is_killed_exits_1_and_says_so(tmp_path, capsys):
    _sleeping_pytest(tmp_path)
    started = time.monotonic()
    rc = test_run._run_pc(["-q", "x.py"], tmp_path, timeout_s=0.2)
    out = capsys.readouterr().out
    assert rc == 1
    assert time.monotonic() - started < 20
    assert out.count("\n") == 1 and out.startswith("test run: PC run killed")


def test_n_run_test_run_bounds_the_pc_run_by_the_setting(tmp_path, capsys):
    _sleeping_pytest(tmp_path)
    seams = fake_seams(40.0)  # a short selection: the PC runs it, with the real _run_pc
    seams.load_settings = lambda: dict(ON, test_run_timeout_minutes=0.005)  # 0.3 s
    rc = test_run.run_test_run(["-q", "x.py"], cwd=tmp_path, seams=seams)
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert out[0] == "test run: PC (run 40 s < floor 2 min)"
    assert out[1].startswith("test run: PC run killed")


def test_o_the_pc_part_beside_github_is_joined_within_the_bound(tmp_path, capsys):
    ran = []

    def slow_pc(args, cwd):
        ran.append(list(args))
        time.sleep(1.0)
        return 0

    def green(plan, **kw):
        return GithubSuiteResult(status="green")

    seams = fake_seams(600.0, github=green, pc_run=slow_pc)
    seams.load_settings = lambda: dict(ON, test_run_timeout_minutes=0.0033)  # 0.2 s
    seams.deps = Deps(plan_for=lambda: (PLAN, ""), pc_run_seconds=lambda ids: 600.0,
                      pc_wait_seconds=lambda now: 1800.0, needs_pc=lambda ids: [IDS[0]])
    started = time.monotonic()
    rc = test_run.run_test_run(["tests/test_x.py"], cwd=tmp_path, seams=seams)
    out = capsys.readouterr().out
    assert rc == 1
    assert time.monotonic() - started < 0.9  # not the sleeping PC part's whole second
    assert "PC part: 1 ids, failed" in out
