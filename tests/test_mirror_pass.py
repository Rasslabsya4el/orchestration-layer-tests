"""#425 unit 1: the dispatcher's mirror pass (tools/agentops/mirror_pass.py).

Each case runs `create_mirrors` over a fake `gh` runner that keeps state: a create adds a mirror that the next call
sees. No network, no real `gh`, no write against a real ticket."""

from __future__ import annotations

import json
import subprocess

from tools.agentops.mirror_pass import (
    MIRROR_LABEL,
    create_mirrors,
    mirror_title,
    parse_source_url,
    source_url_of,
)

TICKETS = "owner/tickets"
ORCH_IT = "owner/orch-it"
GONE = "owner/gone"
STDERR_TEXT = "GraphQL: Could not resolve to a Repository with the name 'owner/gone'.\nsecond line"


def _cp(args: list[str], stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


def _url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _src(repo: str, number: int, title: str, body: str, labels: list[str]) -> dict:
    return {"number": number, "title": title, "body": body, "labels": [{"name": n} for n in labels],
            "url": _url(repo, number)}


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _labels(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "--label"]


class FakeGh:
    """The injected `gh`. `issues` maps a listed repo to its open issues; `mirrors` is the tickets repo's mirror issues
    (`state` OPEN or CLOSED); a repo in `unreadable` answers every list with rc 1. A create appends to `mirrors`, so a
    later call over the same fake sees it. Every call is recorded."""

    def __init__(self, issues: dict[str, list[dict]] | None = None, mirrors: list[dict] | None = None,
                 unreadable: tuple[str, ...] = ()) -> None:
        self.issues = issues or {}
        self.mirrors = list(mirrors or [])
        self.unreadable = set(unreadable)
        self.calls: list[tuple[list[str], str | None]] = []
        self.next_number = 900

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append((list(args), stdin))
        if args[:2] == ["label", "create"]:
            return _cp(args)
        if args[:2] == ["issue", "create"]:
            return self._create(args, stdin or "")
        if args[:2] == ["issue", "list"]:
            return self._list(args)
        return _cp(args, rc=1, stderr="unexpected call")

    def _list(self, args: list[str]) -> subprocess.CompletedProcess:
        repo = _flag(args, "--repo")
        if repo in self.unreadable:
            return _cp(args, rc=1, stderr=STDERR_TEXT)
        if repo == TICKETS and _flag(args, "--label") == MIRROR_LABEL:
            state = _flag(args, "--state")
            found = [m for m in self.mirrors if state == "all" or m["state"] == "OPEN"]
            return _cp(args, stdout=json.dumps([{"number": m["number"], "body": m["body"]} for m in found]))
        label = _flag(args, "--label")
        found = [i for i in self.issues.get(repo, []) if label in [x["name"] for x in i["labels"]]]
        return _cp(args, stdout=json.dumps(found))

    def _create(self, args: list[str], body: str) -> subprocess.CompletedProcess:
        number = self.next_number
        self.next_number += 1
        self.mirrors.append({"number": number, "body": body, "state": "OPEN", "title": _flag(args, "--title"),
                             "labels": _labels(args)})
        return _cp(args, stdout=f"https://github.com/{TICKETS}/issues/{number}\n")


def _run(gh: FakeGh, repos: list[str], lines: list[str]) -> list[tuple[str, int]]:
    return create_mirrors(tickets_repo=TICKETS, mirror_repos=repos, runner=gh, log=lines.append)


def _creates(gh: FakeGh) -> list[tuple[list[str], str]]:
    return [(a, s) for a, s in gh.calls if a[:2] == ["issue", "create"]]


def test_empty_setting_makes_no_call():
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 1, "main is red", "body", ["ready-for-agent"])]})
    result = create_mirrors(tickets_repo=TICKETS, mirror_repos=[], runner=gh, log=lambda text: None)
    assert result == []
    assert gh.calls == []


def test_three_kinds_of_source_each_get_one_mirror_with_its_labels_title_and_first_line():
    """A process's red-main issue (ready), a process's report (needs-triage), a hand-opened ready issue with an
    unrelated title: each gets exactly one mirror; the sources are not changed."""
    gh = FakeGh(issues={ORCH_IT: [
        _src(ORCH_IT, 1, "main is red", "Red main body", ["ready-for-agent"]),
        _src(ORCH_IT, 2, "Skills report", "Report body", ["needs-triage"]),
        _src(ORCH_IT, 3, "Please look at this", "Typed by hand", ["ready-for-agent"]),
    ]})
    lines: list[str] = []
    result = _run(gh, [ORCH_IT], lines)

    assert result == [(_url(ORCH_IT, 1), 900), (_url(ORCH_IT, 2), 901), (_url(ORCH_IT, 3), 902)]
    creates = _creates(gh)
    assert len(creates) == 3
    expected = [
        ("orch-it#1: main is red", ["ready-for-agent", MIRROR_LABEL], _url(ORCH_IT, 1)),
        ("orch-it#2: Skills report", ["needs-triage", MIRROR_LABEL], _url(ORCH_IT, 2)),
        ("orch-it#3: Please look at this", ["ready-for-agent", MIRROR_LABEL], _url(ORCH_IT, 3)),
    ]
    for (args, stdin), (title, labels, url) in zip(creates, expected):
        assert _flag(args, "--repo") == TICKETS
        assert _flag(args, "--title") == title
        assert _labels(args) == labels
        assert stdin.split("\n", 1)[0] == f"Mirror of {url}; the source holds its history and comments."
    assert lines == [f"mirror: {_url(ORCH_IT, n)} -> #{m}" for n, m in ((1, 900), (2, 901), (3, 902))]
    assert not any(a[:2] == ["issue", "edit"] for a, _ in gh.calls)


def test_second_call_over_the_same_state_creates_nothing():
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 1, "main is red", "body", ["ready-for-agent"])]})
    assert _run(gh, [ORCH_IT], []) == [(_url(ORCH_IT, 1), 900)]

    gh.calls.clear()
    assert _run(gh, [ORCH_IT], []) == []
    assert _creates(gh) == []
    assert not any(a[:2] == ["label", "create"] for a, _ in gh.calls)  # no label call when nothing is created


def test_closed_mirror_still_blocks_a_duplicate():
    """The known sources are read over every state: a mirror already closed keeps its source from a second mirror."""
    mirror = {"number": 40, "state": "CLOSED", "title": "orch-it#1: main is red", "labels": ["ready-for-agent", "mirror"],
              "body": f"Mirror of {_url(ORCH_IT, 1)}; the source holds its history and comments.\n\nRed main body"}
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 1, "main is red", "Red main body", ["ready-for-agent"])]},
                mirrors=[mirror])
    assert _run(gh, [ORCH_IT], []) == []
    assert _creates(gh) == []
    known_read = next(a for a, _ in gh.calls if a[:2] == ["issue", "list"] and _flag(a, "--repo") == TICKETS)
    assert _flag(known_read, "--state") == "all"


def test_unreadable_repo_is_logged_once_and_the_other_repo_is_still_mirrored():
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 1, "main is red", "body", ["ready-for-agent"])]}, unreadable=(GONE,))
    lines: list[str] = []
    result = _run(gh, [GONE, ORCH_IT], lines)

    assert result == [(_url(ORCH_IT, 1), 900)]
    cannot_read = [line for line in lines if "cannot read" in line]
    assert cannot_read == [f"mirror: cannot read {GONE}: {STDERR_TEXT.splitlines()[0]}"]


def test_unreadable_tickets_repo_opens_nothing_and_says_so():
    """Without the known sources no mirror may be opened: a second one of an existing pair could not be told apart."""
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 1, "main is red", "body", ["ready-for-agent"])]}, unreadable=(TICKETS,))
    lines: list[str] = []
    assert _run(gh, [ORCH_IT], lines) == []
    assert _creates(gh) == []
    assert [line for line in lines if "cannot read" in line] == [f"mirror: cannot read {TICKETS}: {STDERR_TEXT.splitlines()[0]}"]


def test_source_carrying_both_labels_is_mirrored_once_with_ready_for_agent():
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 7, "both labels", "body", ["ready-for-agent", "needs-triage"])]})
    assert _run(gh, [ORCH_IT], []) == [(_url(ORCH_IT, 7), 900)]
    creates = _creates(gh)
    assert len(creates) == 1
    assert _labels(creates[0][0]) == ["ready-for-agent", MIRROR_LABEL]


def test_body_is_verbatim_after_the_first_line():
    body = "## Goal\n\nKeep it:\n\n```\nx = 1\n```\n\n- a  \n- b\n\n"
    gh = FakeGh(issues={ORCH_IT: [_src(ORCH_IT, 5, "verbatim", body, ["needs-triage"])]})
    _run(gh, [ORCH_IT], [])
    (_, stdin), = _creates(gh)
    assert stdin == ("Mirror of https://github.com/owner/orch-it/issues/5; the source holds its history and comments.\n\n"
                     + body)


def test_source_url_of_reads_only_the_first_line():
    url = "https://github.com/owner/orch-it/issues/1"
    assert source_url_of(f"Mirror of {url}; the source holds its history and comments.\n\nbody") == url
    assert source_url_of(f"intro\nMirror of {url}; the source holds its history and comments.") is None
    assert source_url_of("Please see https://github.com/owner/orch-it/issues/1\n") is None
    assert source_url_of("") is None


def test_parse_source_url_and_mirror_title_name_the_source():
    assert parse_source_url("https://github.com/owner/name/issues/7") == ("owner/name", 7)
    assert mirror_title("owner/orch-it", 7, "Skills report") == "orch-it#7: Skills report"
