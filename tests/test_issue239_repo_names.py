"""#239: the heavy-run line names each repo by its GitHub name, the owner's repo order matches a name whole or up to a
dash, `repo-order set` refuses a name that matches no repo of this one or its targets, and a take line records the
wait in minutes (acceptance rows 1-3). FakeClock git repos in a temp dir; the clock is injected."""

import json
import subprocess
import time

import pytest

from tools.agentops import cli
from tools.agentops import machine_berths as ms
from tools.agentops import repo_identity as rid
from tools.agentops import repo_order as ro
from tools.agentops.util import no_window_flags

START_10_00 = time.mktime((2026, 10, 19, 10, 0, 0, 0, 0, -1))
MIN = 60.0
OWNER_ORDER = ["orchestration-layer", "Beta", "Gamma", "Delta-service"]
TARGETS = [
    {"repo": "acme/Delta-service", "folder": "D:/work/delta-service-v2"},
    {"repo": "acme/Beta-panel-dashboard", "folder": "D:/work/Beta panel dashboard"},
    {"repo": "acme/Gamma-ledger-agent", "folder": "D:/work/Gamma ledger agent"},
]


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def always_alive(pid, started):
    return True


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                   creationflags=no_window_flags())


def make_repo(path, origin=None):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init")
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    return path


def make_worktree(main, name):
    path = main / ".claude" / "worktrees" / name
    path.parent.mkdir(parents=True)
    git(main, "worktree", "add", "-q", str(path), "-b", name)
    return path


@pytest.fixture
def repos(tmp_path):
    """The folders of this PC: origins in `acme/...` form, one folder name that differs, a linked worktree of
    the Gamma repo, and one repo with no origin at all (with its own linked worktree)."""
    gh = "https://github.com/acme/"
    orch = make_repo(tmp_path / "orchestration-layer", gh + "orchestration-layer.git")
    beta = make_repo(tmp_path / "Beta", gh + "Beta-panel-dashboard.git")
    gamma = make_repo(tmp_path / "Gamma ledger agent", gh + "Gamma-ledger-agent.git")
    gamma_wt = make_worktree(gamma, "wt-49-i4")
    delta = make_repo(tmp_path / "delta-service-v2", gh + "Delta-service.git")
    plain = make_repo(tmp_path / "plain")
    plain_wt = make_worktree(plain, "wt-plain")
    return {"orch": orch, "beta": beta, "gamma": gamma, "gamma_wt": gamma_wt, "delta": delta,
            "plain": plain, "plain_wt": plain_wt}


def _take_rows(mdir):
    path = mdir / "heavy_runs.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row["action"] == "take"]


def test_row1_each_run_is_named_by_its_github_repo(repos):
    assert rid.repo_name(repos["orch"]) == "orchestration-layer"
    assert rid.repo_name(repos["beta"]) == "Beta-panel-dashboard"
    assert rid.repo_name(repos["gamma"]) == "Gamma-ledger-agent"
    assert rid.repo_name(repos["delta"]) == "Delta-service"
    assert rid.repo_name(repos["gamma_wt"]) == "Gamma-ledger-agent"  # a linked worktree: the main repo's origin


def test_row1_a_repo_without_origin_is_named_by_its_main_folder(repos):
    assert rid.repo_name(repos["plain"]) == "plain"
    assert rid.repo_name(repos["plain_wt"]) == "plain"  # never the linked worktree's folder name


def test_row1_the_order_ranks_each_repo_by_its_github_name(repos):
    ro.set_order(OWNER_ORDER)
    assert ro.rank(rid.repo_name(repos["orch"])) == (0, 4)
    assert ro.rank(rid.repo_name(repos["beta"])) == (1, 4)
    assert ro.rank(rid.repo_name(repos["gamma"])) == (2, 4)
    assert ro.rank(rid.repo_name(repos["delta"])) == (3, 4)
    assert ro.rank(rid.repo_name(repos["gamma_wt"])) == (2, 4)  # the worktree's run ranks as its Gamma repo


def test_row1_the_order_matches_case_folded_and_up_to_a_dash_only(repos):
    ro.set_order(["gamma"])
    assert ro.rank("Gamma-ledger-agent") == (0, 1)
    assert ro.rank("Gammafinances") == (1, 1)  # no dash: not the same name's start
    assert ro.rank("GAMMA-LEDGER-AGENT") == (0, 1)


def test_row1_the_line_logs_a_run_under_its_github_repo_name(repos, mdir):
    berth = ms.acquire(repos["gamma_wt"], "wiring", 1000, berths=1, is_live=always_alive, clock=FakeClock(START_10_00))
    berth.release()
    takes = _take_rows(mdir)
    assert len(takes) == 1
    assert takes[0]["repo"] == "Gamma-ledger-agent"


def _config_root(tmp_path, repos):
    """This repo for the CLI: its own origin and the owner's targets, as config/orchestration.json names them."""
    root = repos["orch"]
    (root / "config").mkdir()
    (root / "config" / "orchestration.json").write_text(json.dumps({"targets": TARGETS}), encoding="utf-8")
    return root


def test_row2_set_refuses_a_name_that_matches_no_repo(tmp_path, repos, monkeypatch, capsys):
    monkeypatch.setattr(ro, "THIS_REPO", _config_root(tmp_path, repos))
    ro.set_order(["orchestration-layer", "Beta"])
    capsys.readouterr()

    assert cli.main(["repo-order", "set", "Gammaz"]) != 0
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1
    assert "Gammaz" in err_lines[0]
    assert ro.get() == ["orchestration-layer", "Beta"]  # unchanged


def test_row2_set_refuses_two_unknown_names_in_one_line(tmp_path, repos, monkeypatch, capsys):
    monkeypatch.setattr(ro, "THIS_REPO", _config_root(tmp_path, repos))
    ro.set_order(["orchestration-layer", "Beta"])
    capsys.readouterr()

    assert cli.main(["repo-order", "set", "Gammaz", "Servicex"]) != 0
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1
    assert "'Gammaz'" in err_lines[0] and "'Servicex'" in err_lines[0]
    assert ro.get() == ["orchestration-layer", "Beta"]  # unchanged


def test_row2_set_accepts_a_name_that_matches_a_target(tmp_path, repos, monkeypatch, capsys):
    monkeypatch.setattr(ro, "THIS_REPO", _config_root(tmp_path, repos))
    assert cli.main(["repo-order", "set", "Gamma"]) == 0
    assert ro.get() == ["Gamma"]


def test_row2_show_prints_each_name_with_the_repo_it_matched(tmp_path, repos, monkeypatch, capsys):
    monkeypatch.setattr(ro, "THIS_REPO", _config_root(tmp_path, repos))
    assert cli.main(["repo-order", "set", *OWNER_ORDER]) == 0
    capsys.readouterr()
    assert cli.main(["repo-order", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "orchestration-layer -> orchestration-layer",
        "Beta -> Beta-panel-dashboard",
        "Gamma -> Gamma-ledger-agent",
        "Delta-service -> Delta-service",
    ]


def test_row3_a_take_that_waited_from_10_00_to_10_07_logs_waited_min_7_0(mdir):
    clock = FakeClock(START_10_00)
    first = ms.acquire("holder-repo", "first", 1000, berths=1, is_live=always_alive, clock=clock, poll=0.005)

    def sleep(_seconds):
        clock.t = START_10_00 + 7 * MIN  # the second waiter looks again at 10:07; the first run is over by then
        first.release()

    second = ms.acquire("waiting-repo", "second", 1000, berths=1, is_live=always_alive, clock=clock, poll=0.005,
                        sleep=sleep)
    second.release()
    takes = {row["run"]: row for row in _take_rows(mdir)}
    assert takes["first"]["waited_min"] == 0.0
    assert takes["second"]["waited_min"] == 7.0
