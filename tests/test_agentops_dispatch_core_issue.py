"""tools/agentops/dispatch.py's no-checkpoint path (issue #263, unit 7): a session ended holding
an issue with no closing or parking checkpoint writes a core issue at the home, deduped against an
already-open one naming the same defect. No network: a fake `gh` records every call the
dispatcher's default writer makes through `core_issue.write_core_issue` /
`core_issue.find_open_core_issue`; a plain in-memory recorder proves the writer seam is called and
never lets an exception out of the dispatcher."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops.dispatch import ORCH_DOC, Dispatcher
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events, _issue
from tests._area_rule import _no_area_rule  # noqa: F401

HOME = "someone/home-repo"
REPO = "someone/target-repo"

SETTINGS = {
    "repo": REPO,
    "home": HOME,
    "core_version": "abc123abc123",
    "update_rule": "word",
}


def _manifest_path(tmp_path: Path) -> Path:
    path = tmp_path / "core_manifest.txt"
    path.write_text(f"{ORCH_DOC}\ntools/agentops/dispatch.py\n", encoding="utf-8")
    return path


def _result(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeCoreGh:
    """A small stateful fake of `gh` for the home tracker: enough of `issue list/create/comment`
    and the `api` sub-issue calls that `write_core_issue`/`find_open_core_issue` make, so the
    dedup ("same defect twice") is proven at the gh level rather than assumed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.issues: dict[int, dict] = {}
        self.next_number = 501
        self.comments: list[tuple[int, str]] = []
        self.parent_number: int | None = None

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        if args[:2] == ["repo", "view"]:
            return _result(stdout=REPO + "\n")
        if args[:2] == ["label", "create"]:
            return _result(stdout="")
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            fields = args[args.index("--json") + 1]
            if label == "core-issue":
                rows = [i for i in self.issues.values() if "core-issue" in i["labels"]]
            else:  # core-epic
                rows = [i for i in self.issues.values() if "core-epic" in i["labels"]]
            if fields == "number,title":
                out = [{"number": r["number"], "title": r["title"]} for r in rows]
            else:
                out = [{"number": r["number"], "title": r["title"], "body": r["body"]} for r in rows]
            return _result(stdout=json.dumps(out))
        if args[:2] == ["issue", "create"]:
            title = args[args.index("--title") + 1]
            body = args[args.index("--body") + 1]
            labels = {args[i + 1] for i, a in enumerate(args) if a == "--label"}
            number = self.next_number
            self.next_number += 1
            self.issues[number] = {"number": number, "title": title, "body": body,
                                   "labels": labels, "id": number * 100, "state": "open",
                                   "closed_at": None}
            return _result(stdout=f"https://github.com/{HOME}/issues/{number}\n")
        if args[:2] == ["issue", "comment"]:
            number = int(args[2])
            body = args[args.index("--body") + 1]
            self.comments.append((number, body))
            return _result(stdout="")
        if args[:1] == ["api"]:
            if args[1:3] == ["-X", "POST"]:
                return _result(stdout="")
            if args[1:3] == ["-X", "DELETE"]:
                return _result(stdout="")
            if "--jq" in args and args[args.index("--jq") + 1] == ".id":
                number = int(args[1].rsplit("/", 1)[-1])
                return _result(stdout=f"{self.issues[number]['id']}\n")
            if "sub_issues" in args[1]:
                return _result(stdout=json.dumps([]))
        raise AssertionError(f"unexpected gh call: {args}")


def _dispatcher(tmp_path: Path, *, claude: FakeClaude, gh: FakeGh, core_gh: FakeCoreGh) -> Dispatcher:
    manifest_path = _manifest_path(tmp_path)

    def writer(**kwargs):
        from tools.agentops.core_issue import find_open_core_issue, write_core_issue
        same = find_open_core_issue(kind=kwargs["kind"], what=kwargs["what"], gh_runner=core_gh,
                                    settings=SETTINGS, cwd=tmp_path, )
        write_core_issue(gh_runner=core_gh, settings=SETTINGS, cwd=tmp_path,
                         manifest_path=manifest_path, same=same, **kwargs)

    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      core_issue_writer=writer)


def _end_one_no_checkpoint(tmp_path: Path, claude: FakeClaude, gh: FakeGh, d: Dispatcher,
                           number: int) -> None:
    gh._candidates = [_issue(number, number)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(number)
    claude.set_state(live_id, state="done", status="idle")
    d.tick()


def test_a_session_ended_with_no_checkpoint_twice_in_one_repo_writes_one_core_issue(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    core_gh = FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)

    _end_one_no_checkpoint(tmp_path, claude, gh, d, 9)
    _end_one_no_checkpoint(tmp_path, claude, gh, d, 9)

    create_calls = [c for c in core_gh.calls if c[:2] == ["issue", "create"]
                    and "core-issue" in c]
    assert len(create_calls) == 1
    assert len(core_gh.comments) == 1


def test_a_writer_that_raises_never_stops_the_existing_comment_or_the_dispatcher(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()

    def raising_writer(**kwargs):
        raise RuntimeError("boom")

    d = Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                  state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                  core_issue_writer=raising_writer)

    gh._candidates = [_issue(9, 9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()  # must not raise

    assert report.outcomes == {9: "no_checkpoint"}
    assert [n for n, _ in gh.comments] == [9]
    assert gh.removed_assignees == [9]


# -- #832: an issue whose last comment is the ending session's own checkpoint is released ------------

def _end_one_with_last_comment(claude, gh, d, number: int, body: str) -> str:
    gh._candidates = [_issue(number, number)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(number)
    gh.comment_body[number] = body.replace("<id>", live_id)
    claude.set_state(live_id, state="done", status="idle")
    return d.tick().outcomes[number]


def test_832_last_comment_names_the_session_releases_without_comment_or_core_issue(tmp_path):
    claude, gh, core_gh = FakeClaude(), FakeGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)

    assert _end_one_with_last_comment(claude, gh, d, 9, "ORCH <id>\nwaiting on a run") == "released"

    assert gh.removed_assignees == [9] and gh.comments == []
    assert gh.issues[9].labels == ("ready-for-agent",) and gh.issues[9].assignees == ()
    assert gh.added_labels == [] and gh.removed_labels == []
    assert core_gh.calls == []
    ends = [e for e in _events(tmp_path) if e["kind"] == "orch_end"]
    assert [(e["issue"], e["outcome"]) for e in ends] == [(9, "released")]


def test_832_last_comment_by_someone_else_stays_no_checkpoint(tmp_path):
    claude, gh, core_gh = FakeClaude(), FakeGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)

    assert _end_one_with_last_comment(claude, gh, d, 9, "a question from someone else") == "no_checkpoint"

    assert [n for n, _ in gh.comments] == [9] and gh.removed_assignees == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)


def test_832_three_released_endings_do_not_label_needs_info(tmp_path):
    claude, gh, core_gh = FakeClaude(), FakeGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)

    for _ in range(3):
        assert _end_one_with_last_comment(claude, gh, d, 9, "ORCH <id>\nwaiting") == "released"

    assert gh.added_labels == [] and gh.comments == [] and core_gh.calls == []
