"""Every core call about an ISSUE from the dispatcher and its helpers goes to the tickets repo.

The sample settings file says `tickets_repo` = owner/work and `repo` = owner/code. Each module's gh seam
records its argv: an issue call names owner/work and never owner/code. Calls about the code repo are not
asserted here (they keep `repo`). No real `gh`, no real clock, no network: the settings path is monkeypatched
and every gh runner is a recording fake."""

from __future__ import annotations

import importlib
import json
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone

from tools.agentops import core_settings
from tools.agentops import areas, brain_notice, dispatch, merge_priority, parked_label, parked_return, streams, sweep  # noqa: F401

autopilot = importlib.import_module("tools.agentops.autopilot")  # a local module: loaded by importlib only
from tools.agentops.dispatch import GhCli
from tools.agentops.parked_return import GhRunner, ParkedReturn

CODE = "owner/code"
WORK = "owner/work"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _point_settings(monkeypatch, tmp_path):
    """A sample settings file: tickets_repo = WORK, repo = CODE; every other required key is a stand-in."""
    path = tmp_path / "orchestration.json"
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data.update({"repo": CODE, "tickets_repo": WORK, "areas": ["area:orchestration"]})
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


class Rec:
    """A recording gh runner at the `(args, stdin)` seam; `answer` is a stdout text or `args -> stdout`."""

    def __init__(self, answer=""):
        self.calls: list[list[str]] = []
        self.answer = answer

    def __call__(self, args, stdin=None, **_kw):
        self.calls.append(list(args))
        out = self.answer(args) if callable(self.answer) else self.answer
        return subprocess.CompletedProcess(args, 0, out, "")


def _assert_work_only(calls):
    assert calls, "no gh call was recorded"
    for args in calls:
        text = " ".join(args)
        assert CODE not in text, f"an issue call went to the code repo: {text}"
        assert WORK in text, f"an issue call did not name the tickets repo: {text}"


def test_dispatch_gh_issue_calls_name_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    ready = json.dumps([{"number": 7, "title": "t", "createdAt": "2026-10-01T00:00:00Z", "labels": [],
                         "comments": [], "blockedBy": {"nodes": []}, "body": ""}])
    rec = Rec(lambda args: ready if args[:2] == ["issue", "list"] else "{}")
    gh = GhCli(runner=rec)
    assert gh.repo == WORK
    offered = [i.number for i in gh.ready_for_agent_issues()]
    gh.parent_issue(7)
    gh.last_comment_at(7)
    gh.comment(7, "x")
    gh.add_label(7, "parked")
    assert offered == [7]
    _assert_work_only(rec.calls)
    assert dispatch._default_stream_keeper().repo == WORK


def test_autopilot_issue_reads_name_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    rec = Rec("[]")
    gh = GhCli(runner=rec)
    # the autopilot's reads are `GhCli` methods only once the local module is registered; call them directly
    # with the GhCli as `self`, so the test does not depend on what else loaded in this process.
    assert autopilot._GhAutopilot.autopilot_goals(gh) == []
    assert autopilot._GhAutopilot.child_issues(gh, 5) == []
    _assert_work_only(rec.calls)


def test_areas_check_reads_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def fake_run_gh(args, input=None, text=True, **_kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, b"[]", b"")

    monkeypatch.setattr(areas, "run_gh", fake_run_gh)
    assert areas.run_areas(Namespace(areas_command="check")) == 0
    _assert_work_only(calls)


def test_sweep_list_reads_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    rec = Rec("[]")
    monkeypatch.setattr(sweep, "run_gh", rec)
    assert sweep.run_sweep(Namespace(sweep_command="list"), now=NOW) == 0
    _assert_work_only(rec.calls)


def test_streams_read_and_close_name_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    graph = json.dumps({"data": {"search": {"nodes": [{"number": 3, "subIssues": {"nodes": [{
        "number": 5, "title": "Stream: a", "state": "OPEN",
        "subIssues": {"nodes": [{"number": 9, "state": "CLOSED", "closedAt": "2026-10-01T00:00:00Z"}]}}]}}]}}})
    rec = Rec(lambda args: graph if args[0] == "api" else "")
    monkeypatch.setattr(streams, "run_gh", rec)
    keeper = dispatch._default_stream_keeper()
    assert keeper.repo == WORK
    keeper._log = lambda m: None
    keeper.tick()
    assert any(a[:2] == ["issue", "close"] for a in rec.calls)
    _assert_work_only(rec.calls)


def test_parked_return_home_check_and_issue_calls_use_their_repos(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    issue = json.dumps([{"number": 7, "body": "Parked: 1 h after core abc1234 reaches owner/code",
                         "labels": [{"name": "parked"}]}])
    rec = Rec(lambda args: issue if args[:2] == ["issue", "list"] else "")
    # the home (CODE) holds its own core: its hours count from the sha's own commit (1 h ago, so not yet),
    # never from the target-core reach a day old (which would wrongly flip the ticket).
    run = ParkedReturn(WORK, GhRunner(WORK, rec), now=lambda: NOW, log=lambda s: None,
                       target_core=lambda r: ("abc1234ffff", NOW - timedelta(days=1)),
                       is_ancestor=lambda a, b: True, commit_time=lambda sha: NOW - timedelta(minutes=30),
                       home=CODE)
    assert run.run_once() == []
    _assert_work_only(rec.calls)


def test_parked_label_defaults_to_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    rec = Rec("[]")
    assert parked_label.main(["--dry-run"], runner=rec) == 0
    _assert_work_only(rec.calls)


def test_merge_priority_reads_name_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def text_runner(args):
        # merge_priority's runner answers the stdout text itself, not a CompletedProcess
        calls.append(list(args))
        return '{"blockedBy":{"nodes":[]}}' if args[0] == "issue" else "[]"

    assert merge_priority.gh_blockers(5, runner=text_runner) == []
    assert merge_priority.gh_marks(5, runner=text_runner) == {}
    _assert_work_only(calls)


def test_brain_notice_label_read_names_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    seen: list[tuple[int, str]] = []

    def reader(issue, repo):
        seen.append((issue, repo))
        return ()

    rc, _title = brain_notice.run_brain_chat_command(5, read_labels=reader)
    assert rc == 0
    assert seen == [(5, WORK)]


def test_unblocker_prompt_names_the_tickets_repo(tmp_path, monkeypatch):
    _point_settings(monkeypatch, tmp_path)
    text = " ".join(dispatch.build_unblocker_command(7))
    assert f"({WORK}#7)" in text, f"the unblocker prompt did not name the tickets repo: {text}"
    assert f"({CODE}#7)" not in text, f"the unblocker prompt named the code repo: {text}"
