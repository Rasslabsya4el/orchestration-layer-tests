"""Every issue call of the report modules goes to the tickets repo.

Sample settings: ``tickets_repo`` = ``owner/work``, ``repo`` = ``owner/code``. Each module's gh runner records the
args; an issue call must name ``owner/work`` and never ``owner/code``. Without the setting, the default is the repo.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_settings, core_table, losses, outcomes_report, recount, retro_cmd, retro_comments, rework

WORK = "owner/work"
CODE = "owner/code"


def _write_settings(path: Path, **extra) -> Path:
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["repo"] = CODE
    data.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def sample(tmp_path, monkeypatch):
    """The installation's settings file: tickets_repo set to the work repo, repo the code repo."""
    path = _write_settings(tmp_path / "config" / "orchestration.json", tickets_repo=WORK)
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """No tickets_repo key: the tickets repo is the repo itself."""
    path = _write_settings(tmp_path / "config" / "orchestration.json")
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


def _repo_flag(args: list[str]) -> str | None:
    return args[args.index("--repo") + 1] if "--repo" in args else None


def _gh_reply(calls: list, stdout: str = "[]"):
    def fake(args, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)
    return fake


# -- recount: `gh issue list --state closed` -------------------------------------------------

def test_recount_lists_closed_issues_of_the_tickets_repo(sample, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(recount, "run_gh", _gh_reply(calls))
    assert recount.find_candidates(date(2026, 10, 1)) == []
    assert len(calls) == 1
    assert _repo_flag(calls[0]) == WORK
    assert not any(CODE in arg for arg in calls[0])


def test_recount_without_the_setting_lists_the_repo_itself(plain, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(recount, "run_gh", _gh_reply(calls))
    recount.find_candidates(date(2026, 10, 1))
    assert _repo_flag(calls[0]) == CODE


# -- outcomes report: `gh issue list --state all` ------------------------------------------

def test_outcomes_issue_list_goes_to_the_tickets_repo(sample, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(outcomes_report, "run_gh", _gh_reply(calls))
    labels, error = outcomes_report.read_issue_list()
    assert error is None and labels == {}
    assert _repo_flag(calls[0]) == WORK
    assert not any(CODE in arg for arg in calls[0])


# -- losses: the REST closed-issue read ------------------------------------------------------

def _window():
    return losses.Window(since=datetime(2026, 10, 1, tzinfo=timezone.utc),
                         until=datetime(2026, 10, 8, tzinfo=timezone.utc))


def test_losses_closed_issues_read_the_tickets_repo_of_the_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write_settings(root / "config" / "orchestration.json", tickets_repo=WORK)
    seen: list[str] = []
    monkeypatch.setattr(losses, "closed_issues_rest", lambda slug, window, gh=None: seen.append(slug) or [])
    losses.default_closed_lister(losses.Repo("repo", root, tmp_path), _window())
    assert seen == [WORK]


def test_losses_without_the_setting_reads_the_repo_itself(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _write_settings(root / "config" / "orchestration.json")
    seen: list[str] = []
    monkeypatch.setattr(losses, "closed_issues_rest", lambda slug, window, gh=None: seen.append(slug) or [])
    losses.default_closed_lister(losses.Repo("repo", root, tmp_path), _window())
    assert seen == [CODE]


# -- rework: `issue list` and the `issues/events` read ---------------------------------------

def test_rework_default_repo_is_the_tickets_repo(sample, tmp_path):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return ""

    rework.run_rework("2026-09-26T00:00:00Z", "2026-10-10T00:00:00Z", runner=runner, root=tmp_path,
                      turns=[], prices={})
    assert len(calls) == 2  # one issue list, one events read
    for args in calls:
        text = " ".join(args)
        assert WORK in text and CODE not in text


def test_rework_explicit_repo_still_wins(sample, tmp_path):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return ""

    rework.run_rework("2026-09-26T00:00:00Z", "2026-10-10T00:00:00Z", repo="owner/other", runner=runner,
                      root=tmp_path, turns=[], prices={})
    assert all("owner/other" in " ".join(args) for args in calls)


# -- retro-sources: the graphql issue reads ---------------------------------------------------

def test_retro_sources_reads_the_tickets_repo(sample, tmp_path, monkeypatch):
    # the table-1 and file-failure reads touch no issue; they are stubbed so the test needs no core-block file
    monkeypatch.setattr(retro_cmd.retro_sources, "role_rows", lambda *a, **k: [])
    monkeypatch.setattr(retro_cmd.retro_sources, "file_failures", lambda *a, **k: [])
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return ""

    out: list[str] = []
    rc = retro_cmd.run_retro_sources(argparse.Namespace(since="2026-10-01", until="2026-10-10"),
                                     root=sample.parents[1], runner=runner, out=out.append)
    assert rc == 0
    assert calls
    for args in calls:
        text = " ".join(args)
        assert f"repo:{WORK}" in text and CODE not in text
    assert f"repo {WORK}" in out[0]


def test_comment_failures_default_repo_is_the_tickets_repo(sample):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return ""

    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 10, tzinfo=timezone.utc)
    rows = retro_comments.comment_failures(None, since, until, runner)
    assert len(rows) == 4
    assert calls
    for args in calls:
        text = " ".join(args)
        assert f"repo:{WORK}" in text and CODE not in text


def test_comment_failures_explicit_repo_still_wins(sample):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return ""

    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 10, tzinfo=timezone.utc)
    retro_comments.comment_failures("owner/other", since, until, runner)
    assert calls and all("repo:owner/other" in " ".join(args) for args in calls)


# -- core table: the timeline reads; the commit read stays on the code repo -----------------

def _table_root(tmp_path: Path, **extra) -> Path:
    root = tmp_path / "repo"
    _write_settings(root / "config" / "orchestration.json", **extra)
    runs = root / "artifacts" / "runs"
    runs.mkdir(parents=True)
    (runs / "events.jsonl").write_text(
        json.dumps({"kind": "orch_end", "outcome": "closed", "issue": 5}) + "\n", encoding="utf-8")
    return root


def _table_runner(calls: list):
    timeline = [
        {"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-01T00:00:00Z"},
        {"event": "closed", "created_at": "2026-10-02T00:00:00Z", "commit_id": "abc"},
    ]

    def runner(args):
        calls.append(list(args))
        if args[0] == "api" and "/timeline" in args[1]:
            return json.dumps(timeline)
        if args[0] == "api" and "/commits/" in args[1]:
            return "2026-10-02T00:00:00Z\n"
        return "[]"

    return runner


def test_core_table_issue_reads_go_to_the_tickets_repo(tmp_path):
    root = _table_root(tmp_path, tickets_repo=WORK)
    calls: list[list[str]] = []
    core_table.compute_repo_row(root, turns=[], closed_issues=None, prices={},
                                since=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                until=datetime(2026, 10, 9, tzinfo=timezone.utc),
                                gh_runner=_table_runner(calls))
    timeline = [c for c in calls if c[0] == "api" and "/timeline" in c[1]]
    closed_list = [c for c in calls if c[:2] == ["issue", "list"]]
    commits = [c for c in calls if c[0] == "api" and "/commits/" in c[1]]
    assert timeline and closed_list and commits
    assert all(c[1] == f"repos/{WORK}/issues/5/timeline" for c in timeline)
    assert all(_repo_flag(c) == WORK for c in closed_list)
    assert all(c[1] == f"repos/{CODE}/commits/abc" for c in commits)  # a code call keeps the code repo
    issue_calls = timeline + closed_list
    assert not any(CODE in " ".join(c) for c in issue_calls)


def test_core_table_without_the_setting_reads_the_repo_itself(tmp_path):
    root = _table_root(tmp_path)
    calls: list[list[str]] = []
    core_table.compute_repo_row(root, turns=[], closed_issues=None, prices={},
                                since=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                until=datetime(2026, 10, 9, tzinfo=timezone.utc),
                                gh_runner=_table_runner(calls))
    timeline = [c for c in calls if c[0] == "api" and "/timeline" in c[1]]
    assert timeline and all(c[1] == f"repos/{CODE}/issues/5/timeline" for c in timeline)
