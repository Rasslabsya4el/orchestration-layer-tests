"""tools/agentops/parked_return.py (#105, unit 1): a parked ticket returns to work by itself once the event its
`Parked:` line names has happened. Fakes for the gh seam, the clock, the target's core marker and the ancestor
check; one small tmp git repo for the two git defaults (`git_is_ancestor`, `default_target_core`)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.agentops.parked_return import (
    Form,
    GhRunner,
    ParkedReturn,
    _hours_text,
    default_target_core,
    git_is_ancestor,
    parse_parked_line,
)

NOW = datetime(2026, 10, 17, 12, 0)
HOME = "owner/home"
SHA = "a1b2c3d"
MARKER = "9f8e7d6c5b4a"
READY = "ready-for-agent"
PARKED_RETURN = "<!-- parked-return -->"


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FakeGh:
    """The gh seam over a list of issues; `list_parked` reflects the label edits made through it."""

    def __init__(self, issues, states=None, fail_add=False):
        self.issues = {i["number"]: {"number": i["number"], "body": i["body"], "labels": list(i["labels"])}
                       for i in issues}
        self.states = dict(states or {})
        self.fail_add = fail_add
        self.list_calls = 0
        self.ops: list[tuple] = []
        self.comments: list[tuple[int, str]] = []

    def list_parked(self):
        self.list_calls += 1
        return [{"number": i["number"], "body": i["body"], "labels": list(i["labels"])}
                for i in self.issues.values() if "parked" in i["labels"]]

    def issue_state(self, owner_repo, n):
        return self.states.get((owner_repo, n))

    def add_label(self, n, label):
        self.ops.append(("add", n, label))
        if self.fail_add:
            return False
        if label not in self.issues[n]["labels"]:
            self.issues[n]["labels"].append(label)
        return True

    def remove_label(self, n, label):
        self.ops.append(("remove", n, label))
        if label in self.issues[n]["labels"]:
            self.issues[n]["labels"].remove(label)
        return True

    def comment(self, n, body):
        self.ops.append(("comment", n))
        self.comments.append((n, body))
        return True


class World:
    """One parked issue (number 7) with its seams; `pairs` are the (older, newer) ancestry facts."""

    def __init__(self, body, *, labels=("parked",), now=NOW, states=None, cores=None, pairs=(), fail_add=False):
        self.number = 7
        self.gh = FakeGh([{"number": 7, "body": body, "labels": list(labels)}], states=states, fail_add=fail_add)
        self.clock = Clock(now)
        self.cores = dict(cores or {})
        self.pairs = set(pairs)
        self.lines: list[str] = []

    def runner(self) -> ParkedReturn:
        return ParkedReturn(HOME, self.gh, now=self.clock, target_core=lambda r: self.cores.get(r),
                            log=self.lines.append, is_ancestor=lambda older, newer: (older, newer) in self.pairs)

    def labels(self) -> list[str]:
        return list(self.gh.issues[self.number]["labels"])


def _lifecycle(world: World, make_event) -> list:
    """Before the event: nothing written. After it: labels flip (add, then remove), one comment, then no more."""
    pr = world.runner()
    assert pr.run_once() == []
    assert world.gh.ops == [] and world.gh.comments == []
    assert world.labels() == ["parked"]

    make_event(world)
    flips = pr.run_once()
    assert [f.number for f in flips] == [7]
    assert world.gh.ops[:2] == [("add", 7, READY), ("remove", 7, "parked")]
    assert world.labels() == [READY]
    assert len(world.gh.comments) == 1
    number, text = world.gh.comments[0]
    assert number == 7
    assert text.splitlines()[0] == PARKED_RETURN
    assert len(text.splitlines()) == 2

    assert pr.run_once() == []
    assert len(world.gh.comments) == 1
    return flips


def _move(world: World, t: datetime) -> None:
    world.clock.t = t


def _close(world: World, owner_repo: str, n: int) -> None:
    world.gh.states[(owner_repo, n)] = "CLOSED"


# -- parse_parked_line ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ("Parked: until 2026-10-18 20:53", Form(kind="date", at=datetime(2026, 10, 18, 20, 53))),
    ("Parked: until 2026-10-18", Form(kind="date", at=datetime(2026, 10, 18))),
    ("  parked: UNTIL 2026-10-18 20:53.\n\nbody", Form(kind="date", at=datetime(2026, 10, 18, 20, 53))),
    ("Text\n\nMore\nParked: until 2026-10-18 20:53", Form(kind="date", at=datetime(2026, 10, 18, 20, 53))),
    ("Parked: 24 h after core a1b2c3d reaches owner/repo",
     Form(kind="core", hours=24.0, sha="a1b2c3d", owner_repo="owner/repo")),
    ("Parked: 1.5 h after core 0123456789abcdef reaches owner/repo.",
     Form(kind="core", hours=1.5, sha="0123456789abcdef", owner_repo="owner/repo")),
    ("Parked: until #63 closes", Form(kind="issue", number=63)),
    ("Parked: until owner/repo#63 closes", Form(kind="issue", owner_repo="owner/repo", number=63)),
    ("Parked: waiting for Dana", None),
    ("Parked: until Friday", None),
    ("Parked: until 2026-13-01 10:00", None),
    ("Parked: until 2026-02-30", None),
    ("Some text\nmore text\nthird line\nParked: until 2026-10-18 20:53", None),
    ("", None),
])
def test_parse_accepts_the_three_machine_forms_and_nothing_else(body, expected):
    assert parse_parked_line(body) == expected


# -- the event, per form: before nothing, after one flip, a second pass nothing ----------------------------------

def test_date_form_flips_once_the_date_has_come():
    w = World("Parked: until 2026-10-18 20:53\n\nthe wait")
    flips = _lifecycle(w, lambda w: _move(w, datetime(2026, 10, 18, 20, 53)))
    assert flips[0].event.endswith("2026-10-18 20:53 has passed.")
    assert w.gh.comments[0][1].splitlines()[1] == flips[0].event


def test_core_form_flips_once_the_hours_after_the_reach_have_passed():
    reached = datetime(2026, 10, 16, 12, 0)
    w = World(f"Parked: 24 h after core {SHA} reaches owner/repo", now=reached + timedelta(hours=23),
              cores={"owner/repo": (MARKER, reached)}, pairs={(SHA, MARKER)})
    flips = _lifecycle(w, lambda w: _move(w, reached + timedelta(hours=25)))
    assert flips[0].event.endswith(f"core {SHA} reached owner/repo 24 h ago.")


def test_issue_form_own_repo_flips_once_the_issue_is_closed():
    w = World("Parked: until #63 closes", states={(HOME, 63): "OPEN"})
    flips = _lifecycle(w, lambda w: _close(w, HOME, 63))
    assert flips[0].event.endswith("#63 is closed.")


def test_issue_form_other_repo_flips_once_the_issue_is_closed():
    w = World("Parked: until other/repo#63 closes", states={("other/repo", 63): "OPEN"})
    flips = _lifecycle(w, lambda w: _close(w, "other/repo", 63))
    assert flips[0].event.endswith("other/repo#63 is closed.")


# -- core form: ancestry and the hours ----------------------------------------------------------------------------

def test_core_marker_that_is_an_ancestor_of_the_sha_is_not_reached_even_after_the_hours():
    reached = datetime(2026, 10, 16, 12, 0)
    w = World(f"Parked: 24 h after core {SHA} reaches owner/repo", now=reached + timedelta(hours=25),
              cores={"owner/repo": (MARKER, reached)}, pairs={(MARKER, SHA)})
    assert w.runner().run_once() == []
    assert w.gh.ops == [] and w.gh.comments == []


def test_core_marker_descendant_is_not_reached_at_23_h_and_is_at_25_h():
    reached = datetime(2026, 10, 16, 12, 0)
    w = World(f"Parked: 24 h after core {SHA} reaches owner/repo", now=reached + timedelta(hours=23),
              cores={"owner/repo": (MARKER, reached)}, pairs={(SHA, MARKER)})
    pr = w.runner()
    assert pr.run_once() == []
    assert w.gh.ops == []
    _move(w, reached + timedelta(hours=25))
    assert [f.number for f in pr.run_once()] == [7]


def test_core_form_with_unknown_target_core_is_not_reached():
    w = World(f"Parked: 1 h after core {SHA} reaches owner/repo", now=NOW + timedelta(days=30))
    assert w.runner().run_once() == []
    assert w.gh.ops == []


def test_unknown_core_is_logged_once_per_issue_per_instance():
    w = World(f"Parked: 24 h after core {SHA} reaches other/repo")
    pr = w.runner()
    assert pr.run_once() == []
    assert pr.run_once() == []
    waits = [line for line in w.lines if "waits:" in line]
    assert waits == ["parked return: #7 waits: core of other/repo unknown"]
    assert w.runner().run_once() == []
    assert len([line for line in w.lines if "waits:" in line]) == 2


def test_core_event_text_writes_hours_and_minutes_not_decimal_hours():
    reached = datetime(2026, 10, 16, 12, 0)
    w = World(f"Parked: 2.25 h after core {SHA} reaches owner/repo", now=reached + timedelta(hours=3),
              cores={"owner/repo": (MARKER, reached)}, pairs={(SHA, MARKER)})
    flips = w.runner().run_once()
    assert len(flips) == 1
    assert flips[0].event.endswith(f"core {SHA} reached owner/repo 2 h 15 min ago.")


@pytest.mark.parametrize("hours,text", [(0.5, "30 min"), (0.75, "45 min"), (1.5, "1 h 30 min"),
                                        (2.25, "2 h 15 min"), (24, "24 h")])
def test_hours_text_is_written_for_a_human(hours, text):
    assert _hours_text(hours) == text


# -- what is never flipped ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["Parked: waiting for Dana", "Parked: until Friday"])
def test_free_text_parked_line_is_never_flipped(body):
    w = World(body, now=datetime(2030, 1, 1))
    assert w.runner().run_once() == []
    assert w.gh.ops == [] and w.gh.comments == []
    assert w.labels() == ["parked"]


@pytest.mark.parametrize("human", ["needs-info", "ready-for-human", "wontfix"])
def test_human_wait_label_blocks_the_flip_even_when_the_date_has_come(human):
    w = World("Parked: until 2026-10-18 20:53", labels=("parked", human), now=datetime(2026, 10, 19))
    assert w.runner().run_once() == []
    assert w.gh.ops == [] and w.gh.comments == []
    assert w.labels() == ["parked", human]


def test_add_label_failure_writes_nothing_more_and_logs():
    w = World("Parked: until 2026-10-18 20:53", now=datetime(2026, 10, 19), fail_add=True)
    assert w.runner().run_once() == []
    assert w.gh.ops == [("add", 7, READY)]
    assert w.gh.comments == []
    assert w.labels() == ["parked"]
    assert any("#7" in line for line in w.lines)


def test_a_raising_gh_call_is_caught_per_issue_and_logged():
    class Broken(FakeGh):
        def issue_state(self, owner_repo, n):
            raise RuntimeError("boom")

    w = World("Parked: until #63 closes")
    w.gh = Broken([{"number": 7, "body": "Parked: until #63 closes", "labels": ["parked"]}])
    assert w.runner().run_once() == []
    assert any("#7" in line for line in w.lines)


# -- one list call per pass ----------------------------------------------------------------------------------------

def test_one_list_call_per_pass_with_five_parked_issues():
    issues = [{"number": n, "body": body, "labels": ["parked"]} for n, body in [
        (1, "Parked: until 2026-10-18 20:53"),
        (2, "Parked: until 2026-10-19 09:00"),
        (3, "Parked: until #63 closes"),
        (4, "Parked: waiting for Dana"),
        (5, "Parked: until 2026-10-16 08:00"),
    ]]
    gh = FakeGh(issues)
    pr = ParkedReturn(HOME, gh, now=Clock(NOW), log=lambda s: None)
    assert [f.number for f in pr.run_once()] == [5]
    assert gh.list_calls == 1
    pr.run_once()
    assert gh.list_calls == 2


# -- GhRunner: the argv it builds --------------------------------------------------------------------------------

class RecordingRun:
    def __init__(self, rc=0, stdout="", exc=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.rc = rc
        self.stdout = stdout
        self.exc = exc

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.rc, stdout=self.stdout, stderr="")


def test_gh_runner_list_parked_is_one_call_with_the_exact_argv():
    run = RecordingRun(stdout=json.dumps([{"number": 7, "body": "Parked: until 2026-10-18",
                                           "labels": [{"name": "parked"}, {"name": "bug"}]}]))
    out = GhRunner(HOME, run).list_parked()
    assert run.calls == [(["issue", "list", "-R", HOME, "--state", "open", "--label", "parked",
                           "--limit", "200", "--json", "number,body,labels"], None)]
    assert out == [{"number": 7, "body": "Parked: until 2026-10-18", "labels": ["parked", "bug"]}]


def test_gh_runner_list_parked_failure_is_an_empty_list_not_a_raise():
    assert GhRunner(HOME, RecordingRun(rc=1, stdout="")).list_parked() == []
    assert GhRunner(HOME, RecordingRun(exc=subprocess.TimeoutExpired("gh", 1))).list_parked() == []


def test_gh_runner_issue_state_argv_and_failure():
    run = RecordingRun(stdout="CLOSED\n")
    assert GhRunner(HOME, run).issue_state("other/repo", 63) == "CLOSED"
    assert run.calls == [(["issue", "view", "63", "-R", "other/repo", "--json", "state", "-q", ".state"], None)]
    assert GhRunner(HOME, RecordingRun(rc=1)).issue_state("other/repo", 63) is None


def test_gh_runner_label_edits_and_comment_argv():
    run = RecordingRun()
    gh = GhRunner(HOME, run)
    assert gh.add_label(7, READY) is True
    assert gh.remove_label(7, "parked") is True
    assert gh.comment(7, "text\nmore") is True
    assert run.calls == [
        (["issue", "edit", "7", "-R", HOME, "--add-label", READY], None),
        (["issue", "edit", "7", "-R", HOME, "--remove-label", "parked"], None),
        (["issue", "comment", "7", "-R", HOME, "--body-file", "-"], "text\nmore"),
    ]
    failing = GhRunner(HOME, RecordingRun(rc=1))
    assert failing.add_label(7, READY) is False
    assert failing.remove_label(7, "parked") is False
    assert failing.comment(7, "x") is False


# -- the two git defaults, in one small tmp repo ------------------------------------------------------------------

def _git(cwd: Path, *args: str, committer_date: str | None = None) -> str:
    env = dict(os.environ, GIT_COMMITTER_DATE=committer_date) if committer_date else None
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True,
                          env=env, timeout=30).stdout.strip()


def test_git_is_ancestor_on_a_tmp_repo(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    first = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "two")
    second = _git(tmp_path, "rev-parse", "HEAD")
    assert git_is_ancestor(first, second, cwd=tmp_path) is True
    assert git_is_ancestor(second, first, cwd=tmp_path) is False
    assert git_is_ancestor("0000000", second, cwd=tmp_path) is False


def test_default_target_core_reads_marker_and_commit_time_of_a_tmp_target(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    (target / "config").mkdir()
    (target / "config" / "orchestration.json").write_text(json.dumps({"core_version": MARKER}), encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "core")

    settings = {"repo": HOME, "targets": [{"repo": "other/repo", "folder": "target"}]}
    got = default_target_core(settings, tmp_path)("other/repo")
    assert got is not None
    marker, reached_at = got
    assert marker == MARKER
    assert abs(reached_at - datetime.now()) < timedelta(minutes=5)

    assert default_target_core(settings, tmp_path)("unknown/repo") is None


HOME_COMMIT_DATE = "2026-10-10T08:00:00+00:00"


def test_default_target_core_home_is_its_own_head_reached_at_its_last_commit(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "home", committer_date=HOME_COMMIT_DATE)
    head = _git(tmp_path, "rev-parse", "HEAD")
    marker, reached_at = default_target_core({"repo": HOME, "targets": []}, tmp_path)(HOME)
    assert marker == head
    assert reached_at == datetime.fromisoformat(HOME_COMMIT_DATE).astimezone().replace(tzinfo=None)


def test_home_core_form_counts_its_hours_from_the_shas_own_commit_not_from_the_moving_head():
    """The home's HEAD moves on, so its hours run from the sha's commit (`commit_time`), never from `target_core`."""
    sha_at = datetime(2026, 10, 16, 12, 0)
    w = World(f"Parked: 24 h after core {SHA} reaches {HOME}", now=sha_at + timedelta(hours=23),
              cores={HOME: (MARKER, sha_at + timedelta(hours=23))}, pairs={(SHA, MARKER)})
    run = ParkedReturn(HOME, w.gh, now=w.clock, target_core=lambda r: w.cores.get(r), log=w.lines.append,
                       is_ancestor=lambda older, newer: (older, newer) in w.pairs,
                       commit_time=lambda sha: sha_at if sha == SHA else None)
    assert run.run_once() == []
    _move(w, sha_at + timedelta(hours=25))
    flips = run.run_once()
    assert [f.number for f in flips] == [7]
    assert flips[0].event.endswith(f"core {SHA} reached {HOME} 24 h ago.")


def test_home_core_form_with_an_unknown_sha_commit_is_not_reached():
    w = World(f"Parked: 0 h after core {SHA} reaches {HOME}", cores={HOME: (MARKER, NOW)}, pairs={(SHA, MARKER)})
    run = ParkedReturn(HOME, w.gh, now=w.clock, target_core=lambda r: w.cores.get(r), log=w.lines.append,
                       is_ancestor=lambda older, newer: (older, newer) in w.pairs, commit_time=lambda sha: None)
    assert run.run_once() == []
