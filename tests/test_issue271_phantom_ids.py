"""#271 unit 1: the failed ids of a GitHub run are only ids of the merge's own outer pytest run.

Fakes only: a fake `gh` replays a `--log-failed` text; the branch-tree check is a fake `Deps` entry.
"""
from __future__ import annotations

import subprocess

from tools.agentops import github_run
from tools.agentops import github_suite as gs
from tools.agentops.test_copy_send import SendResult

REPO = "owner/copy"
COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}
SUMMARY = "=================== short test summary info ==================="


def line(text: str, job: str = "test (1)", step: str = "pytest part") -> str:
    return f"{job}\t{step}\t2026-10-20T12:02:00.1234567Z {text}"


def fake_gh(log: str):
    def gh(args: list[str]) -> str:
        assert args[:2] == ["run", "view"] and "--log-failed" in args
        return log
    return gh


def test_row1_inner_pytest_failed_lines_are_dropped_and_the_outer_summary_ids_kept():
    log = "\n".join([
        line("----------------------------- Captured stdout call -----------------------------"),
        line(SUMMARY),  # an inner pytest's own summary, printed inside a captured-output section
        line("FAILED test_t.py::test_t - assert 0"),
        line("FAILED tests/test_t.py::test_t - assert 0"),
        line("=========================== 1 failed in 0.01s ==========================="),
        line(SUMMARY),  # the outer pytest prints its summary last
        line("FAILED tests/x.py::y - AssertionError"),
        line("1 failed, 40 passed in 100s"),
    ])
    assert github_run.failed_test_ids(REPO, 77, fake_gh(log)) == ["tests/x.py::y"]


def test_inner_ids_are_dropped_per_job_and_a_job_without_a_summary_keeps_its_ids():
    log = "\n".join([
        line("FAILED test_t.py::test_t - inner", job="test (1)"),
        line(SUMMARY, job="test (1)"),
        line("FAILED tests/a.py::one - e", job="test (1)"),
        line("FAILED tests/b.py::two - e", job="test (2)"),  # no summary header in this job: kept as today
        line("FAILED tests/c.py::three", job="test (2)"),
    ])
    assert github_run.failed_test_ids(REPO, 77, fake_gh(log)) == [
        "tests/a.py::one", "tests/b.py::two", "tests/c.py::three"]


class Fakes:
    def __init__(self, in_tree_answer):
        self.in_tree_answer = in_tree_answer
        self.checked: list[tuple] = []
        self.polled = {"run_id": 5, "outcome": "red-test", "parts": [], "minutes_used": 1,
                       "failed_ids": ["tests/test_t.py::test_t", "tests/x.py::y"], "full_recheck": False}

    def send(self, root, branch, remote):
        return SendResult(True, "abc")

    def acquire_machines(self, repo, run, want=None, **kw):
        class Berth:
            machines = 2

            def release(self):
                pass
        return Berth()

    def dispatch(self, repo, branch, parts, *a, **kw):
        return 5

    def poll(self, repo, run_id, *a, **kw):
        return self.polled

    def in_tree(self, root, branch, path):
        self.checked.append((root, branch, path))
        if isinstance(self.in_tree_answer, Exception):
            raise self.in_tree_answer
        return path in self.in_tree_answer


def run(fakes: Fakes) -> gs.GithubSuiteResult:
    deps = gs.Deps(send=fakes.send, acquire_machines=fakes.acquire_machines, dispatch=fakes.dispatch,
                   poll=fakes.poll, record=lambda rec: None, push=lambda *a: None, clock=lambda: 1.0,
                   in_tree=fakes.in_tree)
    return gs.run_github_suite(root="C:/x/repo", branch="b", repo_slug="me/repo", remote=COPY, repo_name="orch",
                               priority=False, settings=SETTINGS, deps=deps)


def test_ids_whose_file_is_not_in_the_branch_tree_are_dropped():
    fakes = Fakes({"tests/x.py"})
    result = run(fakes)
    assert (result.status, result.failed_ids, result.full_recheck) == ("red", ["tests/x.py::y"], False)
    assert {c[2] for c in fakes.checked} == {"tests/test_t.py", "tests/x.py"}
    assert all(c[:2] == ("C:/x/repo", "b") for c in fakes.checked)


def test_a_failed_tree_check_keeps_the_id():
    result = run(Fakes(OSError("git gone")))
    assert result.failed_ids == ["tests/test_t.py::test_t", "tests/x.py::y"]


def test_all_ids_dropped_leaves_an_empty_list_and_the_full_recheck_rule_of_today():
    fakes = Fakes(set())
    result = run(fakes)
    assert (result.status, result.failed_ids, result.full_recheck) == ("red", [], True)


def test_the_default_tree_check_reads_a_real_git_branch(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("def test_y(): pass")
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
    deps = gs.Deps()
    assert deps.in_tree(tmp_path, "main", "tests/x.py") is True
    assert deps.in_tree(tmp_path, "main", "tests/test_t.py") is False
    polled = ["tests/x.py::y", "tests/test_t.py::test_t"]
    assert gs._real_ids(tmp_path, "main", polled, deps.in_tree) == ["tests/x.py::y"]
    assert gs._real_ids(tmp_path, "no-such-branch", polled, deps.in_tree) == polled  # the check raises: ids kept
