"""#425 unit 4: the dispatcher runs the mirror pass every tick (`Dispatcher._sweep_mirrors`).

Each case drives the real `Dispatcher.tick()` over a real `GhCli` whose runner is a stateful fake: a listed repo's issues,
the tickets repo's issues (mirrors) with their comments, and every write the pass or the offer makes. The setting is read
through the real `core_settings.mirror_repos()`, with `core_settings.load` pointed at the list a case chooses. No network,
no real gh."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock
from tools.agentops import closed_by_commit, core_settings, mirror_close, mirror_pass
from tools.agentops.dispatch import Dispatcher, GhCli, TickReport, _Current
from tools.agentops.mirror_close import HELD_MARK
from tools.agentops.mirror_pass import MIRROR_LABEL, source_line

TICKETS = "owner/tickets"
SRC = "owner/oneshot"
SRC_URL = f"https://github.com/{SRC}/issues/7"


def _cp(args: list[str], stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


def _flag(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args[:-1]):
        if a in names:
            return args[i + 1]
    return None


def _values(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]


def _issue(number: int, title: str, labels: list[str], body: str = "the body", state: str = "OPEN",
           url: str | None = None) -> dict:
    return {"number": number, "title": title, "body": body, "state": state, "closedAt": None,
            "closed_at": None, "closed_by": None,
            "labels": [{"name": name} for name in labels], "createdAt": "2026-10-29T00:00:00Z",
            "updatedAt": "2026-10-29T00:00:00Z", "comments": [], "blockedBy": [], "assignees": [],
            "url": url or f"https://github.com/{TICKETS}/issues/{number}"}


class TickGh:
    """The runner under the dispatcher's `GhCli`. `sources` maps a listed repo to its issues (open and closed, the shape
    `gh` gives); the tickets repo's issues are the mirrors, each with its comments. A create appends one OPEN issue, so a
    later call over the same fake sees it. Every call is recorded; an unknown call answers rc 1."""

    def __init__(self, sources: dict[str, list[dict]] | None = None) -> None:
        self.sources = {repo: list(issues) for repo, issues in (sources or {}).items()}
        self.tickets: list[dict] = []
        self.next_number = 900
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append((list(args), stdin))
        if args[:2] == ["label", "create"]:
            return _cp(args)
        if args[:2] == ["issue", "create"]:
            return self._create(args, stdin or "")
        if args[:2] == ["issue", "list"]:
            return self._list(args)
        if args[:2] == ["issue", "view"]:
            return self._view(args)
        if args[:2] in (["issue", "comment"], ["issue", "close"]):
            return self._write(args)
        if args[:1] == ["api"]:
            return self._api(args)
        return _cp(args, rc=1, stderr="unexpected call")

    def _ticket(self, number: int) -> dict | None:
        return next((t for t in self.tickets if t["number"] == number), None)

    def _create(self, args: list[str], body: str) -> subprocess.CompletedProcess:
        number = self.next_number
        self.next_number += 1
        self.tickets.append(_issue(number, _flag(args, "--title") or "", _values(args, "--label"), body=body))
        return _cp(args, stdout=f"https://github.com/{TICKETS}/issues/{number}\n")

    def _list(self, args: list[str]) -> subprocess.CompletedProcess:
        repo = _flag(args, "--repo", "-R")
        label = _flag(args, "--label")
        state = (_flag(args, "--state") or "open").lower()
        if repo == TICKETS:
            pool = self.tickets
        elif repo in self.sources:
            pool = self.sources[repo]
        else:
            return _cp(args, rc=1, stderr=f"GraphQL: Could not resolve to a Repository named {repo}")
        found = [i for i in pool if label is None or label in [x["name"] for x in i["labels"]]]
        if state != "all":
            found = [i for i in found if str(i["state"]).lower() == state]
        return _cp(args, stdout=json.dumps(found))

    def _view(self, args: list[str]) -> subprocess.CompletedProcess:
        mirror = self._ticket(int(args[2]))
        if mirror is None:
            return _cp(args, rc=1, stderr="could not find issue")
        return _cp(args, stdout=json.dumps({"comments": mirror["comments"]}))

    def _write(self, args: list[str]) -> subprocess.CompletedProcess:
        mirror = self._ticket(int(args[2]))
        if mirror is None:
            return _cp(args, rc=1, stderr="could not find issue")
        if args[1] == "comment":
            mirror["comments"].append({"body": _flag(args, "--body") or "", "url": "u"})
        else:
            mirror["state"] = "CLOSED"
            mirror["closedAt"] = "2026-10-29T12:00:00Z"
            if _flag(args, "--comment"):
                mirror["comments"].append({"body": _flag(args, "--comment"), "url": "u"})
        return _cp(args)

    def _api(self, args: list[str]) -> subprocess.CompletedProcess:
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)", args[1]) if len(args) > 1 else None
        if match is None:
            if len(args) > 1 and args[1].endswith("/events"):
                return _cp(args, stdout="[]")
            return _cp(args, rc=1, stderr="unexpected api call")
        for issue in self.sources.get(match.group(1), []):
            if issue["number"] == int(match.group(2)):
                return _cp(args, stdout=json.dumps(issue))
        return _cp(args, rc=1, stderr="HTTP 404")

    def pass_calls(self) -> list[list[str]]:
        """The calls the mirror pass makes: any that names a listed repo, the mirror label or an issue create."""
        return [args for args, _ in self.calls
                if SRC in args or MIRROR_LABEL in args or args[:2] == ["issue", "create"]]

    def creates(self) -> list[list[str]]:
        return [args for args, _ in self.calls if args[:2] == ["issue", "create"]]


def _setting(monkeypatch, repos: list[str]) -> None:
    """The real `mirror_repos()` over the real settings file, with the one key set to `repos`."""
    real_load = core_settings.load
    monkeypatch.setattr(core_settings, "load", lambda path=None: {**real_load(path), "mirror_repos": list(repos)})


def _source() -> dict:
    return _issue(7, "the source title", ["ready-for-agent"], body="the source body", url=SRC_URL)


def _dispatcher(tmp_path: Path, gh: TickGh, *, dry_run: bool = False,
                claude: FakeClaude | None = None) -> tuple[Dispatcher, list[str]]:
    lines: list[str] = []
    d = Dispatcher(claude=claude or FakeClaude(), gh=GhCli(repo=TICKETS, runner=gh, clock=FakeClock()),
                   max_hours=4.0, events_file=tmp_path / "events.jsonl", orch_offer_max=99,
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=FakeClock(), dry_run=dry_run, log=lines.append)
    return d, lines


def _three_ticks(monkeypatch, root: Path, *, pass_on: bool) -> list[tuple[list[str], str | None]]:
    root.mkdir(parents=True)
    _setting(monkeypatch, [])
    if not pass_on:
        monkeypatch.setattr(Dispatcher, "_sweep_mirrors", lambda self: None)
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(root, gh)
    for _ in range(3):
        d.tick()
    return gh.calls


def test_empty_setting_three_ticks_make_exactly_the_calls_of_a_tick_without_the_pass(monkeypatch, tmp_path):
    with_pass = _three_ticks(monkeypatch, tmp_path / "with", pass_on=True)
    without_pass = _three_ticks(monkeypatch, tmp_path / "without", pass_on=False)
    assert [args for args, _ in with_pass] == [args for args, _ in without_pass]
    assert not any(SRC in args for args, _ in with_pass)


def test_empty_setting_three_ticks_open_no_mirror(monkeypatch, tmp_path):
    _setting(monkeypatch, [])
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(tmp_path, gh)
    for _ in range(3):
        d.tick()
    assert gh.pass_calls() == []
    assert gh.tickets == []


def test_one_ready_source_gets_exactly_one_mirror_in_one_tick(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    gh = TickGh({SRC: [_source()]})
    d, lines = _dispatcher(tmp_path, gh)
    d.tick()
    creates = gh.creates()
    assert len(creates) == 1
    args = creates[0]
    assert _flag(args, "--repo") == TICKETS
    assert _values(args, "--label") == ["ready-for-agent", MIRROR_LABEL]
    assert _flag(args, "--title") == "oneshot#7: the source title"
    assert not any(line.startswith("mirror ") for line in lines)


def test_three_more_ticks_open_no_second_mirror(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(tmp_path, gh)
    d.tick()
    for _ in range(3):
        d.tick()
    assert len(gh.creates()) == 1
    assert len(gh.tickets) == 1


def test_after_one_tick_the_offer_read_already_contains_the_new_mirror(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(tmp_path, gh)
    d.tick()
    mirror = gh.tickets[0]["number"]
    assert mirror in [issue.number for issue in d.gh.ready_for_agent_issues()]


def test_the_pass_runs_after_the_closed_by_commit_sweep_and_before_the_close_sync(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    order: list[str] = []
    real_sweep = closed_by_commit.sweep
    real_create = mirror_pass.create_mirrors
    real_sync = mirror_close.sync_closes

    def sweep(**kw):
        order.append("sweep")
        return real_sweep(**kw)

    def create(**kw):
        order.append("create")
        return real_create(**kw)

    def sync(**kw):
        order.append("sync")
        return real_sync(**kw)

    monkeypatch.setattr(closed_by_commit, "sweep", sweep)
    monkeypatch.setattr(mirror_pass, "create_mirrors", create)
    monkeypatch.setattr(mirror_close, "sync_closes", sync)
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(tmp_path, gh)
    d.tick()
    assert order == ["sweep", "create", "sync"]


def test_a_create_failure_is_one_log_line_and_the_close_sync_still_runs(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    order: list[str] = []

    def boom(**kw):
        order.append("create")
        raise RuntimeError("boom")

    def sync(**kw):
        order.append("sync")
        return []

    monkeypatch.setattr(mirror_pass, "create_mirrors", boom)
    monkeypatch.setattr(mirror_close, "sync_closes", sync)
    gh = TickGh({SRC: [_source()]})
    d, lines = _dispatcher(tmp_path, gh)
    report = d.tick()
    assert isinstance(report, TickReport)
    assert order == ["create", "sync"]
    assert [line for line in lines if line.startswith("mirror ")] == ["mirror create failed: RuntimeError: boom"]


def test_a_close_sync_failure_is_one_log_line_and_the_create_still_ran(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])

    def boom(**kw):
        raise RuntimeError("boom2")

    monkeypatch.setattr(mirror_close, "sync_closes", boom)
    gh = TickGh({SRC: [_source()]})
    d, lines = _dispatcher(tmp_path, gh)
    d.tick()
    assert len(gh.creates()) == 1
    assert [line for line in lines if line.startswith("mirror ")] == ["mirror close sync failed: RuntimeError: boom2"]


def test_a_closed_source_held_by_a_live_session_waits_one_marker_then_closes_after_the_hold(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    closed = _issue(7, "the source title", ["ready-for-agent"], body="the source body", state="CLOSED", url=SRC_URL)
    closed.update(closed_at="2026-10-29T10:00:00Z", closed_by={"login": "owner"})
    gh = TickGh({SRC: [closed]})
    gh.tickets.append(_issue(900, "oneshot#7: the source title", ["ready-for-agent", MIRROR_LABEL],
                             body=source_line(SRC_URL) + "\n\nthe source body"))
    claude = FakeClaude()
    claude.add_session("sess-live", name="orch #900", state="working", status="busy", sessionId="uuid-live")
    d, _ = _dispatcher(tmp_path, gh, claude=claude)
    d.sessions.append(_Current(session="sess-live", started_at=0.0, held={900}))
    mirror = gh.tickets[0]

    d.tick()
    d.tick()
    assert len([c for c in mirror["comments"] if c["body"].startswith(HELD_MARK)]) == 1
    assert mirror["state"] == "OPEN"
    assert not any(args[:2] == ["issue", "close"] for args, _ in gh.calls)

    d.sessions[0].held.discard(900)
    d.tick()
    assert mirror["state"] == "CLOSED"
    closes = [args for args, _ in gh.calls if args[:2] == ["issue", "close"]]
    assert len(closes) == 1 and closes[0][2] == "900"


def test_held_callable_is_true_for_a_mirror_held_by_a_live_session_only(tmp_path):
    gh = TickGh()
    d, _ = _dispatcher(tmp_path, gh)
    d.sessions.append(_Current(session="live-1", started_at=0.0, held={41}))
    assert d._mirror_held(41) is True
    assert d._mirror_held(42) is False
    d.ended_sessions.add("live-1")
    assert d._mirror_held(41) is False


def test_dry_run_makes_no_pass_call(monkeypatch, tmp_path):
    _setting(monkeypatch, [SRC])
    gh = TickGh({SRC: [_source()]})
    d, _ = _dispatcher(tmp_path, gh, dry_run=True)
    d.tick()
    assert gh.pass_calls() == []
    assert gh.tickets == []
