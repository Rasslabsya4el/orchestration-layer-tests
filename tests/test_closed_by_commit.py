"""#422 unit 3: the dispatcher's net for a ticket closed by a commit or a PR.

Seams: `is_closed_by_commit(events)` (pure, over real GitHub event dicts copied into `tests/fixtures/closed_by_commit/`)
and `sweep(...)` with an injected fake `gh` runner. No network, no real `gh`, no write against a real ticket."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops import closed_by_commit as cbc
from tools.agentops.closed_by_commit import is_closed_by_commit, sweep
from tests._area_rule import _no_area_rule  # noqa: F401  -- autouse: the one-area rule off for the dispatcher tests

FIXTURES = Path(__file__).parent / "fixtures" / "closed_by_commit"
REPO = "example-owner/orchestration-layer"
#: #411's closing commit lives in the orch-it repo (its event's `commit_url`), not in the home repo.
SHA_411 = "37fa8122958936e3e4fd951656c9b638d482e8dc"
ORCH_IT = "example-owner/orch-it"
NOW = datetime(2026, 10, 29, 15, 0, 0, tzinfo=timezone.utc)


def _events(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cp(args: list[str], stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


class FakeGh:
    """The injected `gh` runner. `closed` is the answer of the closed-issue list (`number`, `closedAt`);
    `events` maps an issue number to its events (a list, or None for a failed read); `reopen_rc` and
    `comment_rc` answer the two writes. Every call is recorded; nothing is sent anywhere."""

    def __init__(self, closed: list[dict], events: dict[int, list | None], reopen_rc: int = 0,
                 comment_rc: int = 0, pulls: dict[str, list | None] | None = None) -> None:
        self.closed = closed
        self.events = events
        self.reopen_rc = reopen_rc
        self.comment_rc = comment_rc
        #: `commits/<sha>/pulls` answers by sha: a list of PRs (none = `[]`), or None for a failed read.
        self.pulls = pulls or {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append((list(args), stdin))
        if args[:2] == ["issue", "list"]:
            return _cp(args, stdout=json.dumps(self.closed))
        if args[0] == "api" and "/commits/" in args[-1]:
            sha = args[-1].split("/commits/")[1].split("/")[0]
            answer = self.pulls.get(sha, [])
            if answer is None:
                return _cp(args, rc=1, stderr="gh: HTTP 502")
            return _cp(args, stdout=json.dumps(answer))
        if args[0] == "api":
            number = int(args[-1].split("/issues/")[1].split("/")[0])
            events = self.events.get(number)
            if events is None:
                return _cp(args, rc=1, stderr="gh: HTTP 502")
            return _cp(args, stdout=json.dumps(events))
        if args[:2] == ["issue", "reopen"]:
            return _cp(args, rc=self.reopen_rc, stderr="" if not self.reopen_rc else "gh: HTTP 500")
        if args[:2] == ["issue", "comment"]:
            return _cp(args, rc=self.comment_rc)
        return _cp(args, rc=1, stderr="unexpected call")

    def writes(self) -> list[tuple[list[str], str | None]]:
        return [(a, s) for a, s in self.calls if a[:2] in (["issue", "reopen"], ["issue", "comment"])]

    def reads_of(self, number: int) -> int:
        return sum(1 for a, _ in self.calls if a[0] == "api" and f"/issues/{number}/" in a[-1])


def _closed(number: int, closed_at: str) -> dict:
    return {"number": number, "closedAt": closed_at}


# -- is_closed_by_commit: the pure rule over the events -----------------------------------------------------------

def test_closed_by_commit_event_names_its_commit():
    """#411's real closed event (14:14:09Z, commit 37fa812) is a close by a commit."""
    assert is_closed_by_commit(_events("issue411_closed_by_commit.json")) == SHA_411


def test_reopened_after_the_commit_close_is_left_alone():
    """#411 as it stands now: the closed event is followed by its `reopened` event, so it is not closed by a commit."""
    assert is_closed_by_commit(_events("issue411_closed_then_reopened.json")) is None


def test_close_step_close_has_null_commit_is_left_alone():
    """#403: its closed event has no commit_id (the close step, or a close in the web UI, looks the same)."""
    assert is_closed_by_commit(_events("issue403_closed_null_commit.json")) is None


def test_web_ui_close_null_commit_is_left_alone():
    """#410: a closed event with null commit_id, the negative fixture for a close with no commit."""
    assert is_closed_by_commit(_events("issue410_closed_null_commit.json")) is None


def test_no_events_and_empty_commit_are_not_a_commit_close():
    assert is_closed_by_commit([]) is None
    closed_empty = [{"event": "closed", "commit_id": ""}]
    assert is_closed_by_commit(closed_empty) is None


def test_only_the_last_close_counts():
    """A commit close, reopened, then closed again by the close step: the last close has no commit, so none."""
    events = _events("issue411_closed_then_reopened.json") + [
        {"event": "closed", "commit_id": None, "created_at": "2026-10-29T15:10:00Z"}]
    assert is_closed_by_commit(events) is None


# -- sweep: the one cycle ----------------------------------------------------------------------------------------

def test_commit_closed_ticket_is_reopened_with_a_comment_naming_the_commit():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")})
    notices: list[tuple[int, str]] = []
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW,
                   runner=gh, notify=lambda n, text: notices.append((n, text)))

    assert result.reopened == {411: SHA_411}
    writes = gh.writes()
    assert (["issue", "reopen", "411", "-R", REPO], None) in writes
    comments = [(a, s) for a, s in writes if a[:2] == ["issue", "comment"]]
    assert len(comments) == 1
    body = comments[0][1]
    assert body.splitlines()[0] == "<!-- reopened-by-net -->"
    assert SHA_411 in body
    assert f"https://github.com/{ORCH_IT}/commit/{SHA_411}" in body  # the event's own commit_url, not the home repo
    assert f"https://github.com/{REPO}/commit/" not in body
    assert "closing step" in body
    assert [n for n, _ in notices] == [411]
    assert SHA_411 in notices[0][1]


def test_close_step_close_is_left_alone():
    gh = FakeGh(closed=[_closed(403, "2026-10-26T10:31:38Z")],
                events={403: _events("issue403_closed_null_commit.json")})
    result = sweep(repo=REPO, since=datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    assert result.reopened == {}
    assert gh.writes() == []


def test_web_ui_close_with_null_commit_is_left_alone():
    gh = FakeGh(closed=[_closed(410, "2026-10-29T11:13:58Z")],
                events={410: _events("issue410_closed_null_commit.json")})
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 11, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    assert result.reopened == {}
    assert gh.writes() == []


def test_issue_reopened_after_the_commit_close_is_left_alone():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")],
                events={411: _events("issue411_closed_then_reopened.json")})
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    assert result.reopened == {}
    assert gh.writes() == []


def test_sweep_reads_only_issues_closed_since_the_last_cycle_and_moves_the_time():
    """#411 closed before the last cycle is not read; #410 closed after it is; the time moves to this cycle's now."""
    since = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z"), _closed(410, "2026-10-29T11:13:58Z")],
                events={411: _events("issue411_closed_by_commit.json"),
                        410: _events("issue410_closed_null_commit.json")})
    # a list answer may include an issue closed before `since` (the search is at date precision); the sweep drops it
    result = sweep(repo=REPO, since=since, now=NOW, runner=gh)

    assert gh.reads_of(410) == 0
    assert gh.reads_of(411) == 1
    assert result.since == NOW
    list_call = next(a for a, _ in gh.calls if a[:2] == ["issue", "list"])
    assert "closed" in list_call and "--search" in list_call
    assert f"closed:>={since.isoformat(timespec='seconds')}" in list_call


def test_first_cycle_without_a_last_time_reads_nothing_and_starts_the_clock():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")})
    result = sweep(repo=REPO, since=None, now=NOW, runner=gh)
    assert gh.calls == []
    assert result.reopened == {}
    assert result.since == NOW


def test_failed_list_keeps_the_last_time_and_writes_nothing():
    since = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

    def failing(args, stdin=None):
        return _cp(args, rc=1, stderr="gh: HTTP 502")

    result = sweep(repo=REPO, since=since, now=NOW, runner=failing)
    assert result.since == since
    assert result.reopened == {}


def test_failed_reopen_moves_the_time_back_so_the_next_cycle_retries():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")},
                reopen_rc=1)
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    assert result.reopened == {}
    assert result.since == datetime(2026, 10, 29, 14, 14, 9, tzinfo=timezone.utc)


def test_failed_events_read_moves_the_time_back():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: None})
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    assert result.reopened == {}
    assert result.since == datetime(2026, 10, 29, 14, 14, 9, tzinfo=timezone.utc)


def test_paged_events_are_read_from_concatenated_pages():
    """`gh api --paginate` prints one JSON array per page, back to back; the sweep reads all of them."""
    first = _events("issue411_closed_then_reopened.json")
    second = [{"event": "closed", "commit_id": SHA_411, "created_at": "2026-10-29T15:05:00Z"}]
    text = json.dumps(first) + json.dumps(second)

    def paged(args, stdin=None):
        if args[0] == "api":
            return _cp(args, stdout=text)
        return _cp(args, stdout=json.dumps([_closed(411, "2026-10-29T15:05:00Z")]))

    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=paged)
    assert result.reopened == {411: SHA_411}


def test_commit_without_a_commit_url_names_only_the_sha_not_the_home_repo():
    """An event with no `commit_url` has no repo of its own: the note names the sha and links nothing."""
    events = [{"event": "closed", "commit_id": SHA_411, "created_at": "2026-10-29T14:14:09Z"}]
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: events})
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    body = next(s for a, s in gh.writes() if a[:2] == ["issue", "comment"])
    assert result.reopened == {411: SHA_411}
    assert SHA_411 in body
    assert "github.com" not in body


# -- the pull request that merged the closing commit is named (fix 1) -----------------------------------------------

def test_commit_of_a_pull_request_merge_names_the_pull_request_in_the_note_and_the_notice():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")},
                pulls={SHA_411: [{"number": 119, "merged_at": "2026-10-29T14:14:00Z"}]})
    notices: list[str] = []
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh,
                   notify=lambda n, text: notices.append(text))

    assert result.reopened == {411: SHA_411}
    # the lookup goes through the injected runner, on the repo of the event's commit_url
    assert (["api", f"repos/{ORCH_IT}/commits/{SHA_411}/pulls"], None) in gh.calls
    body = next(s for a, s in gh.writes() if a[:2] == ["issue", "comment"])
    assert "pull request #119" in body
    assert f"https://github.com/{ORCH_IT}/pull/119" in body
    assert f"https://github.com/{ORCH_IT}/commit/{SHA_411}" in body
    assert "#119" in notices[0] and f"https://github.com/{ORCH_IT}/pull/119" in notices[0]


def test_commit_with_no_pull_request_names_only_the_commit():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")},
                pulls={SHA_411: []})
    notices: list[str] = []
    sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh,
          notify=lambda n, text: notices.append(text))
    body = next(s for a, s in gh.writes() if a[:2] == ["issue", "comment"])
    assert "pull request #" not in body
    assert f"https://github.com/{ORCH_IT}/commit/{SHA_411}" in body
    assert "pull request #" not in notices[0]


def test_failed_pull_request_lookup_still_reopens_and_names_only_the_commit():
    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")},
                pulls={SHA_411: None})
    result = sweep(repo=REPO, since=datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc), now=NOW, runner=gh)
    body = next(s for a, s in gh.writes() if a[:2] == ["issue", "comment"])
    assert result.reopened == {411: SHA_411}
    assert "pull request #" not in body
    assert SHA_411 in body


# -- the 200-issue cap: a full list keeps the excess for the next cycle (fix 3) -----------------------------------

def test_a_full_list_is_logged_and_since_stops_at_the_oldest_close_read():
    """200 closes in one list (the limit): the cycle logs it and moves `since` only to the oldest close it read, so the
    excess closed after that is read next cycle; it never jumps to now."""
    since = datetime(2026, 10, 29, 13, 0, 0, tzinfo=timezone.utc)
    closed = [_closed(n, (since + timedelta(seconds=n)).isoformat().replace("+00:00", "Z")) for n in range(1, 201)]
    gh = FakeGh(closed=closed, events={n: _events("issue403_closed_null_commit.json") for n in range(1, 201)})
    lines: list[str] = []
    result = sweep(repo=REPO, since=since, now=NOW, runner=gh, log=lines.append)

    assert result.since == since + timedelta(seconds=1)
    assert result.since != NOW
    assert any("200" in line and "next cycle" in line for line in lines)
    list_call = next(a for a, _ in gh.calls if a[:2] == ["issue", "list"])
    assert list_call[list_call.index("--limit") + 1] == "200"


def test_a_short_list_still_moves_since_to_now():
    since = datetime(2026, 10, 29, 13, 0, 0, tzinfo=timezone.utc)
    closed = [_closed(n, (since + timedelta(seconds=n)).isoformat().replace("+00:00", "Z")) for n in range(1, 11)]
    gh = FakeGh(closed=closed, events={n: _events("issue403_closed_null_commit.json") for n in range(1, 11)})
    lines: list[str] = []
    result = sweep(repo=REPO, since=since, now=NOW, runner=gh, log=lines.append)
    assert result.since == NOW
    assert not any("next cycle" in line for line in lines)


# -- the dispatcher's wiring: one call per cycle, the time kept in the state file ---------------------------------

def test_dispatcher_cycle_reopens_notifies_and_keeps_the_read_time_in_the_state_file(tmp_path):
    from tools.agentops.dispatch import GhCli  # the wiring under test
    from tests.test_agentops_dispatch import FakeClaude, FakeClock, _dispatcher

    gh = FakeGh(closed=[_closed(411, "2026-10-29T14:14:09Z")], events={411: _events("issue411_closed_by_commit.json")})
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=GhCli(runner=gh), clock=FakeClock())
    d.log = lambda text: None
    d._closed_since = datetime(2026, 10, 29, 14, 0, 0, tzinfo=timezone.utc)

    d._sweep_closed_by_commit()

    assert (["issue", "reopen", "411", "-R", d.gh.repo], None) in gh.writes()
    assert any(SHA_411 in message for _, message in claude.waked)
    assert d._closed_since is not None and d._closed_since > datetime(2026, 10, 29, 14, 14, 9, tzinfo=timezone.utc)

    d._write_state()
    stored = json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8"))
    assert stored[cbc.SINCE_KEY] == d._closed_since.isoformat(timespec="seconds")
    # the state file keeps whole seconds: a reload reads a little earlier, never later (no gap)
    assert cbc.load_since(tmp_path / "orch_session.json") == d._closed_since.replace(microsecond=0)


def test_the_notice_never_wakes_a_finishing_orch_session(tmp_path):
    """GLOSSARY 'Finishing ORCH': never woken, never offered. The notice skips it; a live ORCH still gets it."""
    from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh as DispatchGh, _dispatcher, _issue

    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=DispatchGh(candidates=[_issue(1)]), clock=FakeClock())
    d.log = lambda text: None
    d.tick()  # one live ORCH session, not finishing
    session = d.sessions[0]
    target = session.name or session.session
    assert session.finishing is False

    d._notify_closed_by_commit(411, "closed by commit: notice")
    assert any(t == target for t, _ in claude.waked)  # not finishing: woken

    claude.waked.clear()
    session.finishing = True
    d._notify_closed_by_commit(411, "closed by commit: notice")
    assert not any(t == target for t, _ in claude.waked)  # finishing: never woken
