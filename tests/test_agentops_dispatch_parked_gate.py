"""tools/agentops/dispatch.py (#470, unit 1): a parked ticket whose body's machine `Parked:` line names an event that has
not happened is held out of the ready queue, so neither the offer nor the wake of a live ORCH sees it, whatever
returned it there. Rows 1-3 of #470's "Result".

The dispatcher is driven through `tick()` with a real `GhCli` on a fake `gh` runner (the seam of
`tests/test_agentops_dispatch_parked_return.py`), a fake claude and a fake clock. The ready queue, the parked list, the
unlabelled list of the merge-result relabel, and the issue reads (`--json state`, the view for the rest) answer from one
in-memory issue table, so an edit of a label is what the next read sees."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops import parked_return as parked_return_mod
from tools.agentops.dispatch import GhCli, ParkMark

from tests.test_agentops_dispatch import FakeClaude, FakeClock, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

REPO = "o/r"
READY = "ready-for-agent"
T_PARK = "2020-01-01T10:00:00Z"
T_LATER = "2020-01-01T11:00:00Z"
FUTURE = "until 2099-01-01 00:00"
PAST = "until 2020-01-01 00:00"
PARK_MERGE_BODY = ("Checkpoint 2 (ORCH 2008a72d, #7). Merge of `b` queued.\n"
                   "<!-- parked-on-run: parked on merge var/agentops/results/20200101T100000-merge-b.json -->")
MERGE_LANDED_BODY = "<!-- merge result -->\nMerge of `b` landed on main."


def _ok(args, out: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, 0, out, "")


def _row(number: int, *, body: str = "", labels=(), comments=(), state: str = "OPEN", updated: str = T_PARK,
         created: str = "2026-09-01T00:00:00Z") -> dict:
    return {"number": number, "body": body, "labels": list(labels), "comments": [dict(c) for c in comments],
            "state": state, "updatedAt": updated, "createdAt": created}


def _row_ready(number: int, body: str = "", **fields) -> dict:
    """A `ready-for-agent` row created in the week the gate tests share (the defaults they repeat)."""
    return _row(number, body=body, labels=[READY], created="2026-09-07T00:00:00Z", **fields)


class GateGh:
    """A fake `gh` runner over an in-memory issue table (`rows`: number -> row)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = {r["number"]: r for r in rows}
        self.calls: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []
        self.raise_state_for: set[int] = set()  # `issue view N --json state` raises for these numbers
        self.fail_state_for: set[int] = set()  # `issue view N --json state` exits 1 for these numbers

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return _ok(args, json.dumps(self._list(args)))
        if args[:2] == ["issue", "view"]:
            return self._view(args)
        if args[:2] == ["issue", "edit"]:
            row = self.rows[int(args[2])]
            if "--add-label" in args:
                name = args[args.index("--add-label") + 1]
                if name not in row["labels"]:
                    row["labels"].append(name)
            if "--remove-label" in args:
                name = args[args.index("--remove-label") + 1]
                row["labels"] = [x for x in row["labels"] if x != name]
            return _ok(args, "")
        if args[:2] == ["issue", "comment"]:
            self.comments.append((int(args[2]), stdin or ""))
            return _ok(args, "")
        return _ok(args, "[]")

    def _list(self, args) -> list[dict]:
        open_rows = [r for r in self.rows.values() if r["state"] == "OPEN"]
        if "--label" in args:
            name = args[args.index("--label") + 1]
            if name == "parked":
                return [{"number": r["number"], "body": r["body"], "labels": [{"name": x} for x in r["labels"]]}
                        for r in open_rows if "parked" in r["labels"]]
            if name == READY and "--search" in args:  # the ready queue; `--assignee` lists are not in it
                return [{"number": r["number"], "createdAt": r["createdAt"], "updatedAt": r["updatedAt"],
                         "body": r["body"], "labels": [{"name": x} for x in r["labels"]],
                         "comments": r["comments"], "blockedBy": {"nodes": []}}
                        for r in open_rows if READY in r["labels"]]
            return []
        if "--search" in args and "-label:" in args[args.index("--search") + 1]:  # the merge-result relabel's pool
            return [{"number": r["number"], "labels": [{"name": x} for x in r["labels"]], "comments": r["comments"]}
                    for r in open_rows if READY not in r["labels"]]
        return []

    def _view(self, args):
        n = int(args[2])
        if args[args.index("--json") + 1] == "state":
            if n in self.fail_state_for:
                return subprocess.CompletedProcess(args, 1, "", "gh: issue view failed")
            if n in self.raise_state_for:
                raise RuntimeError(f"gh issue view {n}: state unreadable")
            return _ok(args, self.rows[n]["state"] + "\n")
        row = self.rows[n]
        return _ok(args, json.dumps({"state": row["state"], "labels": [{"name": x} for x in row["labels"]],
                                     "assignees": [], "comments": row["comments"], "updatedAt": row["updatedAt"],
                                     "body": row["body"], "title": ""}))


def _dispatch(tmp_path: Path, gh: GateGh):
    claude = FakeClaude()
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=GhCli(repo=REPO, runner=gh), clock=clock)
    d.log_lines = []
    d.log = d.log_lines.append
    d.merge_trace = lambda branch, result_file, since: None  # no live merge is ever seen
    d.test_clock = clock
    return d, claude


def _offered(claude: FakeClaude) -> list[int]:
    return [n for offered, _cmd in claude.started for n in offered]


def _woken_about(claude: FakeClaude, number: int) -> bool:
    return any(f"#{number}" in message for _target, message in claude.waked)


def _advance(d, seconds: float = 61) -> None:
    """One minute on: the merge-result relabel runs every 60 s, so the second tick reads it again."""
    d.test_clock.advance(seconds)


# The five return paths of #470 (`_drop_still_parked` x4, `_return_unstarted_merge_park` x1), each with the ticket
# as that path leaves it, and the park mark it had (where the path reads one). The ticket has no `parked` label
# except on the unstarted-merge path, whose return is the one that removes it.
def _path_rows(path: str, line: str) -> tuple[list[dict], dict[int, ParkMark]]:
    body = f"Parked: {line}\n\nThe ticket."
    checkpoint = [{"createdAt": T_PARK, "body": "Checkpoint: parked"}]
    if path == "newer_comment":  # a newer comment after the parking checkpoint
        row = _row_ready(7, body=body, comments=checkpoint + [
            {"createdAt": T_LATER, "body": "A newer comment."}], updated=T_LATER)
        return [row], {7: ParkMark(comment_at=T_PARK, updated_at=T_PARK)}
    if path == "label_event":  # no newer comment, a label event after the checkpoint
        row = _row_ready(7, body=body, comments=checkpoint, updated=T_LATER)
        return [row], {7: ParkMark(comment_at=T_PARK, updated_at=T_PARK)}
    if path == "no_job":  # no job of its own is pending (no reader, `has_pending_job` None)
        row = _row_ready(7, body=body, comments=checkpoint, updated=T_PARK)
        return [row], {7: ParkMark(comment_at=T_PARK, updated_at=T_PARK)}
    if path == "merge_landed":  # the merge it waits on landed
        row = _row_ready(7, body=body, updated=T_LATER, comments=[
            {"createdAt": T_PARK, "body": PARK_MERGE_BODY}, {"createdAt": T_LATER, "body": MERGE_LANDED_BODY}])
        return [row], {7: ParkMark(comment_at=T_PARK, updated_at=T_PARK, reason_at=T_PARK)}
    if path == "finishing_only":  # only the finishing note parked it
        row = _row_ready(7, body=body, comments=checkpoint, updated=T_PARK)
        return [row], {7: ParkMark(comment_at=T_PARK, updated_at=T_PARK, finishing_only=True)}
    if path == "unstarted_merge":  # the merge never started: no result, no trace; the ticket is `parked`
        row = _row(7, body=body, labels=["parked"], created="2026-09-07T00:00:00Z",
                   comments=[{"createdAt": "2020-01-01T10:00:00Z", "body": PARK_MERGE_BODY}], updated=T_PARK)
        return [row], {}
    raise AssertionError(path)


PATHS = ["newer_comment", "label_event", "no_job", "merge_landed", "finishing_only", "unstarted_merge"]


def _tick_with_path(tmp_path: Path, path: str, line: str):
    """Tick 1: ORCH starts on #1 alone (#7 not yet in the queue). Then #7 is returned by `path` with its line;
    tick 2 runs with ORCH live. Returns (dispatcher, claude) after tick 2."""
    gh = GateGh([_row(1, labels=[READY], created="2026-09-01T00:00:00Z")])
    d, claude = _dispatch(tmp_path, gh)
    d.tick()
    assert _offered(claude) == [1]
    rows, marks = _path_rows(path, line)
    for row in rows:
        gh.rows[row["number"]] = row
    d._park_marks.update(marks)
    _advance(d)
    d.tick()
    return d, claude


# -- row 1: each return path, the event not happened (held) and happened (offered) -------------------

@pytest.mark.parametrize("path", PATHS)
def test_row1_a_return_path_does_not_offer_or_wake_about_a_ticket_whose_event_has_not_happened(tmp_path, path):
    d, claude = _tick_with_path(tmp_path, path, FUTURE)

    assert _offered(claude) == [1]
    assert not _woken_about(claude, 7)
    assert len(claude.started) == 1


@pytest.mark.parametrize("path", PATHS)
def test_row1_the_same_ticket_with_its_event_past_is_offered_or_woken_about(tmp_path, path):
    d, claude = _tick_with_path(tmp_path, path, PAST)

    assert 7 in _offered(claude) or _woken_about(claude, 7)


# -- row 2: each of the three machine forms, reached and not reached -----------------------------------

@pytest.mark.parametrize("reached", [False, True])
def test_row2_date_form(tmp_path, reached):
    line = PAST if reached else FUTURE
    gh = GateGh([_row(1, labels=[READY]), _row_ready(7, body=f"Parked: {line}\n\nThe ticket.")])
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert (7 in _offered(claude)) is reached


@pytest.mark.parametrize("reached", [False, True])
def test_row2_issue_form_until_a_named_issue_closes(tmp_path, reached):
    gh = GateGh([_row(1, labels=[READY]), _row(5, state="CLOSED" if reached else "OPEN"),
                 _row_ready(7, body="Parked: until #5 closes\n\nThe ticket.")])
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert (7 in _offered(claude)) is reached


@pytest.mark.parametrize("reached", [False, True])
def test_row2_core_form_after_a_sha_reaches_a_repo(tmp_path, reached, monkeypatch):
    monkeypatch.setattr(dispatch_mod, "default_target_core",
                        lambda settings, home_root: (lambda owner_repo: ("abcdef1234567890", datetime(2020, 1, 1))))
    monkeypatch.setattr(parked_return_mod, "git_is_ancestor", lambda older, newer, cwd=None: reached)
    gh = GateGh([_row(1, labels=[READY]),
                 _row_ready(7, body="Parked: 0 h after core abcdef1 reaches o/t\n\nThe ticket.")])
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert (7 in _offered(claude)) is reached


# -- row 3: the event check raises: held, one log line, no second line on the next pass ----------------

def test_row3_unreadable_issue_state_holds_the_ticket_and_logs_one_line(tmp_path):
    gh = GateGh([_row(1, labels=[READY]), _row(5, state="OPEN"),
                 _row_ready(7, body="Parked: until #5 closes\n\nThe ticket.")])
    gh.raise_state_for = {5}
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert _offered(claude) == [1]
    held = [line for line in d.log_lines if line.startswith("#7:")]
    assert len(held) == 1 and "held" in held[0] and "could not be read" in held[0]

    d.tick()

    assert _offered(claude) == [1]
    assert [line for line in d.log_lines if line.startswith("#7:")] == held


def test_row3_issue_state_read_exiting_nonzero_holds_the_ticket_and_logs_one_line(tmp_path):
    """The real runner's path: `gh issue view` exits non-zero, so `issue_state` returns None and nothing raises."""
    gh = GateGh([_row(1, labels=[READY]), _row(5, state="OPEN"),
                 _row_ready(7, body="Parked: until #5 closes\n\nThe ticket.")])
    gh.fail_state_for = {5}
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert any(call[:3] == ["issue", "view", "5"] for call in gh.calls)
    assert _offered(claude) == [1]
    held = [line for line in d.log_lines if line.startswith("#7:")]
    assert len(held) == 1 and "held" in held[0]

    d.tick()

    assert _offered(claude) == [1]
    assert [line for line in d.log_lines if line.startswith("#7:")] == held


# -- the lines that are not machine forms are left to the other filters ---------------------------------

@pytest.mark.parametrize("body_head", ["Parked: waiting for Dana", None])
def test_free_text_or_no_parked_line_is_not_held_by_the_gate(tmp_path, body_head):
    body = f"{body_head}\n\nThe ticket." if body_head else "The ticket."
    gh = GateGh([_row(1, labels=[READY]), _row_ready(7, body=body)])
    d, claude = _dispatch(tmp_path, gh)

    d.tick()

    assert _offered(claude) == [1, 7]
