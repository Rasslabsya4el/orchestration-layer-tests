"""The closer and the other core callers read and close ISSUES in the tickets repo (setting `tickets_repo`).

A sample settings file names `owner/work` as the tickets repo and `owner/code` as the code repo. A recording fake gh
runner (no live call) shows that every issue call carries `owner/work` and that none carries `owner/code`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops import core_settings  # noqa: E402
from tools.agentops.close_issue import run_close  # noqa: E402

TICKETS = "owner/work"
CODE = "owner/code"
CREATED = "2026-10-10T09:00:00Z"
EDITED = "2026-10-15T12:00:00Z"


def _write_settings(tmp_path: Path, **extra) -> Path:
    data = {key: "x" for key in core_settings.REQUIRED_KEYS}
    data["repo"] = CODE
    data.update(extra)
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def sample_settings(tmp_path, monkeypatch):
    """The installation's settings file replaced by a sample: tickets in owner/work, code in owner/code."""
    path = _write_settings(tmp_path, tickets_repo=TICKETS)
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


def _done(args, stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh", *args], rc, stdout, "")


class RecordingGh:
    """Records every gh call as (args, stdin); answers the closer's graphql read and `issue close` with an open issue
    that has no comments. Any other call fails the test at once."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if args[:2] == ["api", "graphql"]:
            issue = {"id": "I_kwDOFAKE323", "state": "OPEN", "createdAt": CREATED, "lastEditedAt": EDITED,
                     "body": "", "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}
            return _done(args, json.dumps({"data": {"repository": {"issue": issue}}}))
        if args[:2] == ["issue", "close"]:
            return _done(args, "closed\n")
        raise AssertionError(f"unexpected gh call: {args}")

    def issue_calls(self) -> list[tuple[list[str], str | None]]:
        return list(self.calls)

    def mentions(self, text: str) -> bool:
        return any(text in " ".join(args) or text in (stdin or "") for args, stdin in self.calls)


def _closing(tmp_path: Path, text: str = "Closing: the work is merged.\n") -> Path:
    path = tmp_path / "closing.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_closer_closes_in_the_tickets_repo_and_never_calls_the_code_repo(tmp_path, sample_settings):
    gh = RecordingGh()
    rc = run_close(58, _closing(tmp_path), runner=gh)

    assert rc == 0
    closes = [args for args, _ in gh.calls if args[:2] == ["issue", "close"]]
    assert len(closes) == 1
    assert closes[0][closes[0].index("--repo") + 1] == TICKETS
    graphql = [json.loads(stdin)["variables"] for args, stdin in gh.calls if args[:2] == ["api", "graphql"]]
    assert graphql and all(v["owner"] == "owner" and v["name"] == "work" for v in graphql)
    assert not gh.mentions(CODE)


def test_a_repo_given_by_the_flag_still_wins(tmp_path, sample_settings):
    gh = RecordingGh()
    rc = run_close(58, _closing(tmp_path), repo="owner/other", runner=gh)

    assert rc == 0
    closes = [args for args, _ in gh.calls if args[:2] == ["issue", "close"]]
    assert closes[0][closes[0].index("--repo") + 1] == "owner/other"
    assert not gh.mentions(CODE)


# -- the other core callers: every issue call names the tickets repo, never the code repo -----------------------

class Recorder:
    """A stand-in for `run_gh` in one module: records each call's arguments and answers with fixed stdout."""

    def __init__(self, stdout: str = "", rc: int = 0) -> None:
        self.stdout = stdout
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, args, *a, **kw):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(["gh", *args], self.rc, self.stdout, "")


def _repo_flag(args: list[str]) -> str | None:
    for flag in ("-R", "--repo"):
        if flag in args:
            return args[args.index(flag) + 1]
    return None


def test_loop_stop_cli_reads_comments_and_closed_issues_in_the_tickets_repo(sample_settings, monkeypatch):
    from datetime import datetime, timezone

    from tools.agentops import loop_stop_cli

    rec = Recorder("[]")
    monkeypatch.setattr(loop_stop_cli, "run_gh", rec)
    loop_stop_cli.fetch_comments(58)
    loop_stop_cli.fetch_closed(14, datetime(2026, 10, 23, tzinfo=timezone.utc))

    assert any("repos/owner/work/issues/58/comments" in " ".join(c) for c in rec.calls)
    assert _repo_flag(rec.calls[1]) == TICKETS
    assert not any(CODE in " ".join(c) for c in rec.calls)


def test_logic_audit_until_looks_up_the_issue_in_the_tickets_repo(sample_settings, monkeypatch, tmp_path):
    from tools.agentops import logic_audit_until

    rec = Recorder('{"state": "CLOSED"}')
    monkeypatch.setattr(logic_audit_until, "run_gh", rec)
    assert logic_audit_until._gh_issue_state(tmp_path)(58) == "CLOSED"

    assert _repo_flag(rec.calls[0]) == TICKETS
    assert not any(CODE in " ".join(c) for c in rec.calls)


def test_worktree_issue_state_reads_the_issue_in_the_tickets_repo(sample_settings, monkeypatch, tmp_path):
    from tools.agentops import worktree

    rec = Recorder('{"state": "CLOSED"}')
    monkeypatch.setattr(worktree, "run_gh", rec)
    assert worktree.gh_issue_state(58, tmp_path) == "CLOSED"

    assert _repo_flag(rec.calls[0]) == TICKETS
    assert not any(CODE in " ".join(c) for c in rec.calls)


def test_merge_issue_state_reads_the_issue_in_the_tickets_repo(sample_settings, monkeypatch):
    from tools.agentops import merge

    rec = Recorder('{"state": "CLOSED", "stateReason": "COMPLETED"}')
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(merge, "run_gh", rec)
    assert merge.read_issue_state(58) == ("CLOSED", "COMPLETED")

    assert _repo_flag(rec.calls[0]) == TICKETS
    assert not any(CODE in " ".join(c) for c in rec.calls)


def test_provisional_gh_writes_default_to_the_tickets_repo(sample_settings, monkeypatch):
    import importlib
    provisional = importlib.import_module("tools.agentops.provisional")  # a local module: loaded by importlib only

    rec = Recorder("[]")
    monkeypatch.setattr(provisional, "run_gh", rec)
    gh = provisional.GhCli(repo=TICKETS)
    gh.list_provisional_issues()
    gh.comment(58, "note")
    gh.close(58)

    assert all(_repo_flag(c) == TICKETS for c in rec.calls)
    assert not any(CODE in " ".join(c) for c in rec.calls)
    assert core_settings.tickets_repo() == TICKETS
