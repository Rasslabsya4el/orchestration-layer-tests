"""#254: a line check does not run while its merge suite goes to GitHub.

The check asks ``merge_github.plan_for`` (through the probe the merge hands in) before it starts its suite:
a plan skips the check with no child, no berth and no preparation; no plan, or a probe that raises, runs the
check as before (acceptance rows 1-3 of the issue; row 4 is the live closer's).
"""

import pytest

from tools.agentops import core_settings
from tools.agentops import machine_berths as ms
from tools.agentops import merge
from tools.agentops import merge_github
from tools.agentops import line_check
from tools.agentops.line_check import LineCheck, read_status

REASON = "merge suite on GitHub"
PLAN = merge_github.Plan(remote="https://example.invalid/copy.git", own_repo=False,
                         repo_slug="owner/repo", repo_name="repoB")


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    monkeypatch.setattr(merge, "machine_holds", lambda: False)  # the machine is not in play
    return path


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []

    def no_child(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a check child was started")

    monkeypatch.setattr(line_check.subprocess, "Popen", no_child)
    return calls


def make_check(tmp_path, probe):
    repo = tmp_path / "repoB"
    repo.mkdir(exist_ok=True)
    return LineCheck(repo, tmp_path / "locks" / "merge.lock", "1-1-1", "branch-b", "sha", "pytest tests",
                     berth_poll=0.01, github_plan=probe)


def green_prepare(chk):
    """The preparation of a selection that is empty: no worktree, no child, state green."""
    def prepare_into(prep):
        prep.green_now = True
    chk._prepare_into = prepare_into


def test_row1_a_github_plan_skips_the_check_with_no_child_and_no_berth(tmp_path, popen_calls):
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1)
    asked = []
    chk = make_check(tmp_path, lambda: (asked.append(1), (PLAN, ""))[1])
    try:
        assert chk.poll(holder_live=True) is None
        assert chk.state == "skipped"
        assert chk.message == REASON
        status = read_status(chk.lock_path, "1-1-1")
        assert status["state"] == "skipped"
        assert status["reason"] == REASON
        assert chk._proc is None and chk.child_pid is None
        assert chk._thread is None           # no preparation: no merge commit, no worktree
        assert chk._berth is None
        assert not chk._seat.exists()        # no seat taken
        assert popen_calls == []
        assert [r["repo"] for r in ms.holders()] == ["repoA"]   # only the live holder, no berth row of this check
        assert ms.waiting_rows() == []
        chk.poll(holder_live=True)           # a later poll asks nothing again and changes nothing
        assert chk.state == "skipped"
        assert asked == [1]
    finally:
        holder.release()


def test_row2_no_plan_runs_the_check_as_before(tmp_path, popen_calls):
    asked = []
    chk = make_check(tmp_path, lambda: (asked.append(1), (None, "suite on the PC (off)"))[1])
    green_prepare(chk)
    assert chk.poll(holder_live=True) is None
    assert chk._thread is not None           # the check went on to its preparation
    chk._thread.join(10)
    assert chk.poll(holder_live=True) is None
    assert chk.state == "green"
    assert chk.message == "no test the change can affect"
    assert asked == [1]
    assert popen_calls == []


def test_row3_a_probe_that_raises_runs_the_check_as_before(tmp_path, popen_calls):
    def broken():
        raise RuntimeError("settings unreadable")

    chk = make_check(tmp_path, broken)
    green_prepare(chk)
    assert chk.poll(holder_live=True) is None
    assert chk._thread is not None
    chk._thread.join(10)
    assert chk.poll(holder_live=True) is None
    assert chk.state == "green"
    assert popen_calls == []


def test_the_merge_line_hook_asks_plan_for_with_the_merge_settings_and_cwd(tmp_path, monkeypatch, popen_calls):
    settings = {"github_suite": True, "test_copy_remote": "https://example.invalid/copy.git"}
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: settings)
    seen = []

    def fake_plan(s, root, now=None):
        seen.append((s, root))
        return PLAN, ""

    monkeypatch.setattr(merge_github, "plan_for", fake_plan)
    monkeypatch.setattr(merge, "_resolve_target", lambda *a, **k: ("sha", None, "", ""))
    repo = tmp_path / "repoB"
    repo.mkdir()
    hook = merge._make_line_check_hook(repo, tmp_path / "locks" / "merge.lock", "branch-b", "pytest tests",
                                       remote="origin", main="main", suite_env={}, suite_shared=(),
                                       suite_output_dir=tmp_path)
    hook._build("1-1-1")
    holder = ms.acquire(tmp_path / "repoA", "merge-1", 4251, berths=1)
    try:
        assert hook.chk.poll(holder_live=True) is None
        assert hook.chk.state == "skipped"
        assert hook.chk.message == REASON
        assert seen == [(settings, repo)]
        assert popen_calls == []
        assert ms.waiting_rows() == []
    finally:
        holder.release()


def test_the_hook_asks_no_plan_for_a_suite_that_is_not_pytest(tmp_path, monkeypatch):
    """The merge sends only a pytest suite to GitHub (`_run_suite_placed`), so the check skips only for one."""
    settings = {"github_suite": True, "test_copy_remote": "https://example.invalid/copy.git"}
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: settings)
    monkeypatch.setattr(merge_github, "plan_for", lambda s, root, now=None: (PLAN, ""))
    monkeypatch.setattr(merge, "_resolve_target", lambda *a, **k: ("sha", None, "", ""))
    repo = tmp_path / "repoB"
    repo.mkdir()
    probes = {}
    for cmd in ("pytest tests", "python scripts/run_checks.py"):
        hook = merge._make_line_check_hook(repo, tmp_path / "locks" / "merge.lock", "branch-b", cmd,
                                           remote="origin", main="main", suite_env={}, suite_shared=(),
                                           suite_output_dir=tmp_path)
        hook._build("1-1-1")
        probes[cmd] = hook.chk.github_plan()[0]
    assert probes == {"pytest tests": PLAN, "python scripts/run_checks.py": None}
