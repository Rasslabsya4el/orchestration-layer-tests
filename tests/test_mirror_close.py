"""#425 unit 3: the dispatcher's close sync (tools/agentops/mirror_close.py).

Each case runs `sync_closes` over a fake `gh` that keeps state: a close or a reopen changes the state the next call
reads, and every write is recorded as (verb, ref). No network, no real `gh`, no write against a real ticket."""

from __future__ import annotations

import json
import re
import subprocess

from tools.agentops.mirror_close import CLOSED_MARK, HELD_MARK, REOPENED_MARK, sync_closes

TICKETS = "owner/tickets"
ORCH_IT = "owner/orch-it"
OTHER = "owner/other"
GONE = "owner/gone"
STDERR_TEXT = "HTTP 404: Not Found (https://api.github.com/repos/owner/gone/issues/1)\nsecond line"
T_EARLY = "2026-10-29T09:00:00Z"
T_SOURCE_CLOSED = "2026-10-29T09:30:00Z"
T_MIRROR_CLOSED = "2026-10-29T10:00:00Z"
T_REOPENED = "2026-10-29T10:30:00Z"


def _cp(args: list[str], stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


def _src_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _mirror_url(number: int) -> str:
    return f"https://github.com/{TICKETS}/issues/{number}"


def _mirror(number: int, source: tuple[str, int], state: str = "OPEN", closed_at: str | None = None,
            comments: tuple[dict, ...] = ()) -> dict:
    body = f"Mirror of {_src_url(*source)}; the source holds its history and comments.\n\nThe source's body."
    return {"number": number, "state": state, "closedAt": closed_at, "body": body,
            "comments": [dict(c) for c in comments]}


def _source(state: str = "open", closed_at: str | None = None, closed_by: str | None = None,
            pages: tuple[list[dict], ...] = ()) -> dict:
    """`pages` are the events of `gh api .../events --paginate`, one JSON array per page."""

    return {"state": state, "closed_at": closed_at, "closed_by": {"login": closed_by} if closed_by else None,
            "pages": [list(page) for page in pages], "comments": []}


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _text_of(args: list[str]) -> str | None:
    for flag in ("--comment", "--body"):
        if flag in args:
            return _flag(args, flag)
    return None


def _first(text: str) -> str:
    return text.split("\n", 1)[0]


class FakeGh:
    """The injected `gh`. `mirrors` is a list of the tickets repo's mirror issues; `sources` maps (repo, number) to a
    source's state. `unreadable` lists sources whose read answers rc 1; `fail_writes` lists (verb, ref) writes that
    answer rc 1; `tickets_unreadable` makes the mirror list answer rc 1. A write is recorded in `writes`."""

    def __init__(self, mirrors: list[dict] = (), sources: dict | None = None, unreadable: tuple = (),
                 fail_writes: tuple = (), tickets_unreadable: bool = False) -> None:
        self.mirrors = {m["number"]: m for m in mirrors}
        self.sources = dict(sources or {})
        self.unreadable = set(unreadable)
        self.fail_writes = set(fail_writes)
        self.tickets_unreadable = tickets_unreadable
        self.calls: list[tuple[list[str], str | None]] = []
        self.writes: list[tuple[str, str]] = []
        self.clock = 0

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append((list(args), stdin))
        if args[:2] == ["issue", "list"]:
            return self._list(args)
        if args[:1] == ["api"]:
            return self._api(args)
        if args[:2] == ["issue", "view"]:
            return self._view(args)
        if args[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "reopen"]):
            return self._write(args)
        return _cp(args, rc=1, stderr="unexpected call")

    def _now(self) -> str:
        self.clock += 1
        return f"2026-10-29T12:{self.clock:02d}:00Z"

    def _list(self, args: list[str]) -> subprocess.CompletedProcess:
        if self.tickets_unreadable:
            return _cp(args, rc=1, stderr=STDERR_TEXT)
        found = [{"number": n, "state": m["state"], "closedAt": m["closedAt"], "body": m["body"],
                  "url": _mirror_url(n)} for n, m in self.mirrors.items()]
        return _cp(args, stdout=json.dumps(found))

    def _api(self, args: list[str]) -> subprocess.CompletedProcess:
        match = re.fullmatch(r"repos/(.+)/issues/(\d+)(/events)?", args[1])
        assert match is not None, args
        repo, number = match.group(1), int(match.group(2))
        if (repo, number) in self.unreadable:
            return _cp(args, rc=1, stderr=STDERR_TEXT)
        source = self.sources[(repo, number)]
        if match.group(3):
            return _cp(args, stdout="".join(json.dumps(page) for page in source["pages"]))
        return _cp(args, stdout=json.dumps({"state": source["state"], "closed_at": source["closed_at"],
                                            "closed_by": source["closed_by"]}))

    def _view(self, args: list[str]) -> subprocess.CompletedProcess:
        mirror = self.mirrors[int(args[2])]
        return _cp(args, stdout=json.dumps({"comments": [{"body": c["body"], "url": c["url"]}
                                                         for c in mirror["comments"]]}))

    def _write(self, args: list[str]) -> subprocess.CompletedProcess:
        verb, repo, number = args[1], _flag(args, "--repo"), int(args[2])
        ref = f"{repo}#{number}"
        self.writes.append((verb, ref))
        if (verb, ref) in self.fail_writes:
            return _cp(args, rc=1, stderr="HTTP 403: Resource not accessible\nsecond line")
        now = self._now()
        text = _text_of(args)
        if repo == TICKETS:
            mirror = self.mirrors[number]
            if verb == "close":
                mirror["state"], mirror["closedAt"] = "CLOSED", now
            if verb == "reopen":
                mirror["state"], mirror["closedAt"] = "OPEN", None
            if text is not None:
                mirror["comments"].append({"body": text, "url": f"{_mirror_url(number)}#issuecomment-"
                                                                f"{len(mirror['comments']) + 1}"})
            return _cp(args, stdout=_mirror_url(number) + "\n")
        source = self.sources[(repo, number)]
        if verb == "close":
            source["state"], source["closed_at"], source["closed_by"] = "closed", now, {"login": "fake-user"}
        if text is not None:
            source["comments"].append({"body": text, "url": f"{_src_url(repo, number)}#issuecomment-"
                                                            f"{len(source['comments']) + 1}"})
        return _cp(args, stdout=_src_url(repo, number) + "\n")


def _sync(gh: FakeGh, lines: list[str], held=lambda number: False, repos=(ORCH_IT,)) -> list[str]:
    return sync_closes(tickets_repo=TICKETS, runner=gh, log=lines.append, held=held, mirror_repos=list(repos))


def test_closing_a_mirror_closes_its_source_with_one_comment_naming_the_mirror():
    last = {"body": "Closed: done.", "url": "https://github.com/owner/tickets/issues/41#issuecomment-9"}
    gh = FakeGh(mirrors=[_mirror(41, (ORCH_IT, 1), state="CLOSED", closed_at=T_MIRROR_CLOSED, comments=(last,))],
                sources={(ORCH_IT, 1): _source(state="open")})
    lines: list[str] = []

    assert _sync(gh, lines) == ["closed source owner/orch-it#1"]
    source = gh.sources[(ORCH_IT, 1)]
    assert source["state"] == "closed"
    assert [c["body"] for c in source["comments"]] == [
        f"Closed from the mirror {_mirror_url(41)}. Its closing comment: {last['url']}."]
    assert gh.writes == [("close", "owner/orch-it#1")]
    assert lines == []


def test_closing_a_source_closes_its_mirror_with_one_comment_naming_who_and_when():
    gh = FakeGh(mirrors=[_mirror(42, (ORCH_IT, 2))],
                sources={(ORCH_IT, 2): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice")})
    lines: list[str] = []

    assert _sync(gh, lines) == ["closed mirror #42"]
    mirror = gh.mirrors[42]
    assert mirror["state"] == "CLOSED"
    assert [c["body"] for c in mirror["comments"]] == [
        f"{CLOSED_MARK}\nSource {_src_url(ORCH_IT, 2)} was closed by alice at {T_SOURCE_CLOSED}."]
    assert gh.writes == [("close", "owner/tickets#42")]
    assert lines == []


def test_both_closed_makes_no_write_and_no_comment():
    gh = FakeGh(mirrors=[_mirror(43, (ORCH_IT, 3), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 3): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice")})

    assert _sync(gh, []) == []
    assert gh.writes == []
    assert gh.mirrors[43]["comments"] == []


def test_both_open_makes_no_write():
    gh = FakeGh(mirrors=[_mirror(47, (ORCH_IT, 8))], sources={(ORCH_IT, 8): _source(state="open")})

    assert _sync(gh, []) == []
    assert gh.writes == []


def test_source_closed_while_held_comments_once_over_two_cycles_then_closes_after_the_hold():
    gh = FakeGh(mirrors=[_mirror(44, (ORCH_IT, 4))],
                sources={(ORCH_IT, 4): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice")})
    hold = {"on": True}
    asked: list[int] = []

    def held(number: int) -> bool:
        asked.append(number)
        return hold["on"]

    lines: list[str] = []
    assert _sync(gh, lines, held) == ["waiting on hold #44"]
    assert _sync(gh, lines, held) == []
    mirror = gh.mirrors[44]
    assert mirror["state"] == "OPEN"
    markers = [c["body"] for c in mirror["comments"] if _first(c["body"]) == HELD_MARK]
    assert len(markers) == 1
    assert "alice" in markers[0] and T_SOURCE_CLOSED in markers[0]
    assert gh.writes == [("comment", "owner/tickets#44")]
    assert asked == [44, 44]

    hold["on"] = False
    assert _sync(gh, lines, held) == ["closed mirror #44"]
    assert mirror["state"] == "CLOSED"
    assert _first(mirror["comments"][-1]["body"]) == CLOSED_MARK
    assert len(mirror["comments"]) == 2
    assert lines == []


def test_source_reopened_after_the_mirror_closed_reopens_the_mirror_and_is_not_closed_again():
    pages = ([{"event": "closed", "created_at": T_SOURCE_CLOSED}],
             [{"event": "reopened", "created_at": T_REOPENED}])
    gh = FakeGh(mirrors=[_mirror(45, (ORCH_IT, 5), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 5): _source(state="open", pages=pages)})
    lines: list[str] = []

    assert _sync(gh, lines) == ["reopened mirror #45"]
    mirror = gh.mirrors[45]
    assert mirror["state"] == "OPEN"
    assert mirror["comments"][-1]["body"] == f"{REOPENED_MARK}\nSource {_src_url(ORCH_IT, 5)} was reopened."
    assert gh.sources[(ORCH_IT, 5)]["state"] == "open"
    assert gh.sources[(ORCH_IT, 5)]["comments"] == []

    assert _sync(gh, lines) == []
    assert gh.writes == [("reopen", "owner/tickets#45")]
    assert lines == []


def test_mirror_closed_with_no_reopen_after_its_close_closes_the_source():
    """An earlier reopen of the source does not count: only a reopen later than the mirror's close does."""
    pages = ([{"event": "reopened", "created_at": T_EARLY}],)
    gh = FakeGh(mirrors=[_mirror(46, (ORCH_IT, 6), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 6): _source(state="open", pages=pages)})

    assert _sync(gh, []) == ["closed source owner/orch-it#6"]
    assert gh.sources[(ORCH_IT, 6)]["state"] == "closed"
    assert [c["body"] for c in gh.sources[(ORCH_IT, 6)]["comments"]] == [
        f"Closed from the mirror {_mirror_url(46)}. Its closing comment: {_mirror_url(46)}."]
    assert gh.writes == [("close", "owner/orch-it#6")]


def test_unreadable_source_repo_is_logged_once_and_a_pair_of_another_repo_is_still_synced():
    gh = FakeGh(mirrors=[_mirror(50, (ORCH_IT, 5)),
                         _mirror(51, (OTHER, 7), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 5): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice"),
                         (OTHER, 7): _source(state="open")},
                unreadable=[(ORCH_IT, 5)])
    lines: list[str] = []

    assert _sync(gh, lines, repos=(ORCH_IT, OTHER)) == ["closed source owner/other#7"]
    assert lines == [f"mirror: cannot read {ORCH_IT}: {STDERR_TEXT.splitlines()[0]}"]
    assert gh.mirrors[50]["state"] == "OPEN"
    assert [w for w in gh.writes if w[1].startswith("owner/tickets#50")] == []


def test_mirror_without_a_source_line_is_not_touched():
    gh = FakeGh(mirrors=[
        {"number": 60, "state": "CLOSED", "closedAt": T_MIRROR_CLOSED, "body": "Please look at this\n", "comments": []},
        {"number": 61, "state": "OPEN", "closedAt": None,
         "body": f"intro\nMirror of {_src_url(ORCH_IT, 10)}; the source holds its history and comments.",
         "comments": []},
    ], sources={(ORCH_IT, 10): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice")})
    lines: list[str] = []

    assert _sync(gh, lines) == []
    assert gh.writes == []
    assert [a[:2] for a, _ in gh.calls] == [["issue", "list"]]
    assert lines == []


def test_four_pairs_two_from_each_side_all_end_closed_with_one_comment_each():
    gh = FakeGh(mirrors=[
        _mirror(70, (ORCH_IT, 70), state="CLOSED", closed_at=T_MIRROR_CLOSED),
        _mirror(71, (ORCH_IT, 71), state="CLOSED", closed_at=T_MIRROR_CLOSED),
        _mirror(72, (ORCH_IT, 72)),
        _mirror(73, (ORCH_IT, 73)),
    ], sources={
        (ORCH_IT, 70): _source(state="open"),
        (ORCH_IT, 71): _source(state="open"),
        (ORCH_IT, 72): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="bob"),
        (ORCH_IT, 73): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="carol"),
    })
    lines: list[str] = []

    assert _sync(gh, lines) == ["closed source owner/orch-it#70", "closed source owner/orch-it#71",
                                "closed mirror #72", "closed mirror #73"]
    for n in (70, 71, 72, 73):
        assert gh.sources[(ORCH_IT, n)]["state"] == "closed"
        assert gh.mirrors[n]["state"] == "CLOSED"
    assert len(gh.writes) == 4
    for n in (70, 71):
        assert len(gh.sources[(ORCH_IT, n)]["comments"]) == 1
    for n in (72, 73):
        assert len(gh.mirrors[n]["comments"]) == 1
    assert lines == []

    assert _sync(gh, lines) == []
    assert len(gh.writes) == 4


def test_a_failed_write_is_logged_and_the_cycle_goes_on():
    gh = FakeGh(mirrors=[_mirror(80, (ORCH_IT, 80), state="CLOSED", closed_at=T_MIRROR_CLOSED),
                         _mirror(82, (ORCH_IT, 82))],
                sources={(ORCH_IT, 80): _source(state="open"),
                         (ORCH_IT, 82): _source(state="closed", closed_at=T_SOURCE_CLOSED, closed_by="alice")},
                fail_writes=[("close", "owner/orch-it#80")])
    lines: list[str] = []

    assert _sync(gh, lines) == ["closed mirror #82"]
    assert gh.sources[(ORCH_IT, 80)]["state"] == "open"
    assert lines == [f"mirror: cannot close source {_src_url(ORCH_IT, 80)}: HTTP 403: Resource not accessible"]


def test_unreadable_tickets_repo_changes_nothing_and_says_so():
    gh = FakeGh(mirrors=[_mirror(90, (ORCH_IT, 90), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 90): _source(state="open")}, tickets_unreadable=True)
    lines: list[str] = []

    assert _sync(gh, lines) == []
    assert gh.writes == []
    assert lines == [f"mirror: cannot read {TICKETS}: {STDERR_TEXT.splitlines()[0]}"]


def test_empty_mirror_repos_makes_no_runner_call_at_all():
    gh = FakeGh(mirrors=[_mirror(100, (ORCH_IT, 1), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 1): _source(state="open")})
    lines: list[str] = []

    assert _sync(gh, lines, repos=()) == []
    assert gh.calls == []
    assert gh.writes == []
    assert lines == []


def test_mirror_of_an_unlisted_repo_is_neither_read_nor_written():
    gh = FakeGh(mirrors=[_mirror(101, (OTHER, 1), state="CLOSED", closed_at=T_MIRROR_CLOSED),
                         _mirror(102, (ORCH_IT, 2), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(OTHER, 1): _source(state="open"), (ORCH_IT, 2): _source(state="open")})
    lines: list[str] = []

    assert _sync(gh, lines, repos=(ORCH_IT,)) == ["closed source owner/orch-it#2"]
    assert [a for a, _ in gh.calls if any(OTHER in part for part in a)] == []
    assert gh.sources[(OTHER, 1)]["state"] == "open"
    assert gh.writes == [("close", "owner/orch-it#2")]
    assert lines == []


def _source_reads(gh: FakeGh) -> list[str]:
    """The paths of the source reads (`gh api repos/<repo>/issues/<n>`), not the event reads."""

    return [a[1] for a, _ in gh.calls if a[:1] == ["api"] and not a[1].endswith("/events")]


def test_a_gone_source_skips_only_its_own_pair_and_the_other_pairs_of_its_repo_are_synced():
    gh = FakeGh(mirrors=[_mirror(110, (ORCH_IT, 1)),
                         _mirror(111, (ORCH_IT, 2), state="CLOSED", closed_at=T_MIRROR_CLOSED),
                         _mirror(112, (ORCH_IT, 3), state="CLOSED", closed_at=T_MIRROR_CLOSED)],
                sources={(ORCH_IT, 2): _source(state="open"), (ORCH_IT, 3): _source(state="open")},
                unreadable=[(ORCH_IT, 1)])
    lines: list[str] = []

    assert _sync(gh, lines) == ["closed source owner/orch-it#2", "closed source owner/orch-it#3"]
    assert lines == [f"mirror: cannot read {ORCH_IT}: {STDERR_TEXT.splitlines()[0]}"]
    assert _source_reads(gh) == ["repos/owner/orch-it/issues/1", "repos/owner/orch-it/issues/2",
                                 "repos/owner/orch-it/issues/3"]
    assert gh.mirrors[110]["state"] == "OPEN"
    assert gh.writes == [("close", "owner/orch-it#2"), ("close", "owner/orch-it#3")]


def test_repo_with_only_gone_sources_logs_one_line_and_still_tries_each_pair():
    gh = FakeGh(mirrors=[_mirror(113, (GONE, 1)), _mirror(114, (GONE, 2)), _mirror(115, (GONE, 3))],
                unreadable=[(GONE, 1), (GONE, 2), (GONE, 3)])
    lines: list[str] = []

    assert _sync(gh, lines, repos=(GONE,)) == []
    assert lines == [f"mirror: cannot read {GONE}: {STDERR_TEXT.splitlines()[0]}"]
    assert _source_reads(gh) == ["repos/owner/gone/issues/1", "repos/owner/gone/issues/2",
                                 "repos/owner/gone/issues/3"]
    assert gh.writes == []


def test_closing_comment_links_the_last_mirror_comment_without_a_sync_marker():
    earlier = {"body": "Closed: done.", "url": f"{_mirror_url(120)}#issuecomment-1"}
    marker = {"body": f"{HELD_MARK}\nSource {_src_url(ORCH_IT, 12)} was closed by alice at {T_SOURCE_CLOSED}.",
              "url": f"{_mirror_url(120)}#issuecomment-2"}
    gh = FakeGh(mirrors=[_mirror(120, (ORCH_IT, 12), state="CLOSED", closed_at=T_MIRROR_CLOSED,
                                 comments=(earlier, marker)),
                         _mirror(121, (ORCH_IT, 13), state="CLOSED", closed_at=T_MIRROR_CLOSED,
                                 comments=(marker,))],
                sources={(ORCH_IT, 12): _source(state="open"), (ORCH_IT, 13): _source(state="open")})

    assert _sync(gh, []) == ["closed source owner/orch-it#12", "closed source owner/orch-it#13"]
    assert [c["body"] for c in gh.sources[(ORCH_IT, 12)]["comments"]] == [
        f"Closed from the mirror {_mirror_url(120)}. Its closing comment: {earlier['url']}."]
    assert [c["body"] for c in gh.sources[(ORCH_IT, 13)]["comments"]] == [
        f"Closed from the mirror {_mirror_url(121)}. Its closing comment: {_mirror_url(121)}."]
