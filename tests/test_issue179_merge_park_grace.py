"""#179: a ticket parked on a merge result file whose merge never started goes back to `ready-for-agent`.

Rows 1-5 of the issue. The dispatcher is driven through `tick()` with the fake claude and a fake gh that
lists one open ticket without `ready-for-agent`; the wall clock and the trace probe are injected, so no real
merge, no real clock and no real lock folder is read. The default probe (`merge_trace_of`) is tested apart on
a temporary lock folder and suite folder. Row 6 is a live reading by the brain.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import (
    Dispatcher, merge_branch_of_result, merge_trace_of,
)

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tests.test_agentops_dispatch_finishing import FakeTimestamp
from tests._area_rule import _no_area_rule  # noqa: F401

PARK_AT = datetime(2026, 10, 18, 10, 18, 23, tzinfo=timezone.utc)
RESULT = "var/agentops/results/20261018T121809-merge-b.json"
PARK_BODY = (f"Checkpoint 2 (ORCH 2008a72d, #7). Merge of `b` queued.\n"
             f"<!-- parked-on-run: parked on merge {RESULT} -->")


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class ParkedGh(FakeGh):
    """One open ticket without `ready-for-agent`; like the real tracker, a comment lands in its list and a
    label edit changes its labels."""

    def __init__(self, comments: list[dict], labels: list[str]) -> None:
        super().__init__()
        self.ticket = {"number": 7, "labels": [{"name": n} for n in labels], "comments": list(comments)}
        self.stamp: FakeTimestamp | None = None

    def unlabelled_open_issues(self) -> list[dict]:
        if any(l["name"] == "ready-for-agent" for l in self.ticket["labels"]):
            return []
        return [json.loads(json.dumps(self.ticket))]

    def add_label(self, number: int, label: str) -> bool:
        super().add_label(number, label)
        self.ticket["labels"].append({"name": label})
        return True

    def remove_label(self, number: int, label: str) -> bool:
        super().remove_label(number, label)
        self.ticket["labels"] = [l for l in self.ticket["labels"] if l["name"] != label]
        return True

    def comment(self, number: int, body: str) -> bool:
        super().comment(number, body)
        self.ticket["comments"].append({"createdAt": _iso(self.stamp.value), "body": body})
        return True


class World:
    """A dispatcher with a ticket parked at PARK_AT, a fake clock and a probe that answers `trace`."""

    def __init__(self, tmp_path: Path, *, labels=("parked",), comments=None, trace=None) -> None:
        self.stamp = FakeTimestamp(PARK_AT)
        self.clock = FakeClock()
        self.gh = ParkedGh(comments if comments is not None else [{"createdAt": _iso(PARK_AT), "body": PARK_BODY}],
                           list(labels))
        self.gh.stamp = self.stamp
        self.trace = trace
        self.probes: list[tuple] = []
        self.log: list[str] = []
        self.d = self.new_dispatcher(tmp_path)

    def new_dispatcher(self, tmp_path: Path) -> Dispatcher:
        d = Dispatcher(claude=FakeClaude(), gh=self.gh, events_file=tmp_path / "events.jsonl",
                       state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                       clock=self.clock, timestamp=self.stamp, orch_offer_max=99, merge_trace=self._probe)
        d.log = self.log.append
        return d

    def _probe(self, branch: str, result_file: str, since: float):
        self.probes.append((branch, result_file, since))
        return self.trace

    def at(self, minutes: float, d: Dispatcher | None = None) -> None:
        """The wall clock stands `minutes` after the park; one tick (the relabel pass runs every 60 s)."""
        self.stamp.value = PARK_AT + timedelta(minutes=minutes)
        self.clock.advance(61)
        (d or self.d).tick()

    @property
    def grace_comments(self) -> list[str]:
        return [b for _, b in self.gh.comments if "never started" in b]

    @property
    def grace_lines(self) -> list[str]:
        return [m for m in self.log if "never started" in m]


def test_row1_no_trace_16_min_after_the_park_goes_back_to_ready(tmp_path):
    w = World(tmp_path)
    w.at(16)
    assert (7, "ready-for-agent") in w.gh.added_labels
    assert (7, "parked") in w.gh.removed_labels
    assert len(w.grace_comments) == 1
    body = w.grace_comments[0]
    assert (f"the merge this ticket is parked on ({RESULT}) never started: no result, no suite output, "
            f"no lock or queue entry for b after 15 min; back to ready") in body
    assert len(w.grace_lines) == 1 and "#7" in w.grace_lines[0]
    assert w.probes[0][0] == "b"  # the branch comes from the result file's name `<stamp>-merge-<branch>.json`


def test_row1_a_ticket_with_no_parked_label_is_readied_and_nothing_is_removed(tmp_path):
    w = World(tmp_path, labels=())
    w.at(16)
    assert (7, "ready-for-agent") in w.gh.added_labels
    assert w.gh.removed_labels == []
    assert len(w.grace_comments) == 1


def test_row2_14_min_after_the_park_nothing_happens(tmp_path):
    w = World(tmp_path)
    w.at(14)
    assert w.gh.added_labels == [] and w.gh.comments == [] and w.grace_lines == []


def test_row3_a_trace_keeps_the_ticket_parked_however_long(tmp_path):
    w = World(tmp_path, trace="queue entry of pid 31048")
    w.at(60)
    w.at(600)
    assert w.gh.added_labels == [] and w.gh.comments == [] and w.grace_lines == []
    assert w.probes and w.probes[0][2] == PARK_AT.timestamp()  # a suite output counts when started after the park


def test_row3_the_default_probe_sees_a_suite_output_a_lock_a_holding_sidecar_and_a_queue_entry(tmp_path):
    suite, locks = tmp_path / "merge_suite", tmp_path / "locks"
    lock = locks / "merge.lock"
    queue = locks / "merge.lock.queue"
    suite.mkdir()
    queue.mkdir(parents=True)
    since = PARK_AT.timestamp()

    def probe():
        return merge_trace_of("b", since=since, lock_path=lock, suite_dir=suite)

    assert probe() is None

    out = suite / "20261018T122000-000000-b.out"  # started after the park
    out.write_text("x", encoding="utf-8")
    os.utime(out, (since + 600, since + 600))
    assert probe() is not None
    out.unlink()

    old = suite / "20261018T090000-000000-b.out"  # a finished suite of an earlier attempt
    old.write_text("x", encoding="utf-8")
    os.utime(old, (since - 3600, since - 3600))
    other = suite / "20261018T122000-000000-bb.out"  # another branch whose name ends alike
    other.write_text("x", encoding="utf-8")
    os.utime(other, (since + 600, since + 600))
    assert probe() is None

    ticket = queue / f"01792318693053581600-{os.getpid()}-0"
    ticket.write_text(json.dumps({"kind": "merge", "branch": "b"}), encoding="utf-8")
    assert "queue" in probe()
    ticket.write_text(json.dumps({"kind": "merge", "branch": "other"}), encoding="utf-8")
    assert probe() is None
    ticket.write_text(json.dumps({"kind": "merge", "branch": "b"}), encoding="utf-8")
    dead = queue / "01792318693053581601-4000000000-0"  # a pid that is gone does not hold a place
    dead.write_text(json.dumps({"kind": "merge", "branch": "c"}), encoding="utf-8")
    ticket.unlink()
    dead.write_text(json.dumps({"kind": "merge", "branch": "b"}), encoding="utf-8")
    assert probe() is None

    holding = locks / "merge.lock.holding"
    holding.write_text(json.dumps({"pid": os.getpid(), "ns": 1, "info": {"kind": "merge", "branch": "b"}}),
                       encoding="utf-8")
    assert "holding" in probe()
    holding.unlink()

    lock.write_text(json.dumps({"pid": os.getpid(), "kind": "merge", "branch": "b"}), encoding="utf-8")
    assert "lock" in probe()


def test_row4_a_result_file_that_appeared_is_todays_path_no_comment(tmp_path):
    w = World(tmp_path)
    result = tmp_path / "20261018T121809-merge-b.json"
    result.write_text(json.dumps({"rc": 0}), encoding="utf-8")
    w.gh.ticket["comments"][0]["body"] = PARK_BODY.replace(RESULT, str(result).replace("\\", "/"))
    w.at(30)
    assert w.gh.comments == [] and w.grace_lines == []
    assert w.gh.added_labels == []
    assert w.probes == []


def test_row4_a_merge_result_comment_is_todays_path_no_comment(tmp_path):
    result_comment = {"createdAt": _iso(PARK_AT + timedelta(minutes=5)),
                      "body": "<!-- merge result -->\nMerge of b landed on main 0123456789abcdef"}
    w = World(tmp_path, comments=[{"createdAt": _iso(PARK_AT), "body": PARK_BODY}, result_comment])
    w.at(30)
    assert w.gh.comments == [] and w.grace_lines == []


def test_row5_two_reads_still_one_comment(tmp_path):
    w = World(tmp_path)
    w.at(16)
    w.at(17)
    w.at(40)
    assert len(w.grace_comments) == 1 and len(w.grace_lines) == 1


def test_row5_a_dispatcher_restart_does_not_post_again(tmp_path):
    w = World(tmp_path)
    w.at(16)
    w.gh.ticket["labels"] = [{"name": "parked"}]  # the ticket stands unlabelled again (a hand edit)
    (tmp_path / "restart").mkdir()
    fresh = w.new_dispatcher(tmp_path / "restart")
    w.at(30, fresh)
    assert len(w.grace_comments) == 1


def test_a_later_park_starts_a_new_count(tmp_path):
    w = World(tmp_path)
    w.at(16)
    again = PARK_AT + timedelta(minutes=40)
    w.gh.ticket["comments"].append({"createdAt": _iso(again), "body": PARK_BODY})
    w.gh.ticket["labels"] = [{"name": "parked"}]
    w.at(40 + 14)
    assert len(w.grace_comments) == 1
    w.at(40 + 16)
    assert len(w.grace_comments) == 2


def test_a_result_file_name_that_is_not_a_merge_names_no_branch(tmp_path):
    assert merge_branch_of_result("var/agentops/results/20261018T121809-merge-u176b.json") == "u176b"
    assert merge_branch_of_result("C:\\x\\results\\20261018T121809-merge-i178.json") == "i178"
    assert merge_branch_of_result("var/agentops/results/r1.json") is None
    w = World(tmp_path, comments=[{"createdAt": _iso(PARK_AT), "body": PARK_BODY.replace(RESULT, "var/r1.json")}])
    w.at(60)
    assert w.gh.comments == [] and w.probes == []


def test_a_ticket_someone_labelled_on_purpose_is_left_alone(tmp_path):
    w = World(tmp_path, labels=("needs-info",))
    w.at(60)
    assert w.gh.comments == [] and w.gh.added_labels == []


# -- a branch with a `/`: the result file's name and the suite output spell it differently from the raw tickets --

SLASH_RESULT = "var/agentops/results/20261018T121809-merge-claude_x.json"  # `detach.result_path_for` for `claude/x`


def test_a_branch_with_a_slash_is_read_from_the_result_name_as_the_tickets_spell_it(tmp_path):
    suite, locks = tmp_path / "merge_suite", tmp_path / "locks"
    lock, queue = locks / "merge.lock", locks / "merge.lock.queue"
    suite.mkdir()
    queue.mkdir(parents=True)
    since = PARK_AT.timestamp()
    label = merge_branch_of_result(SLASH_RESULT)
    assert label == "claude_x"

    def probe():
        return merge_trace_of(label, since=since, lock_path=lock, suite_dir=suite)

    assert probe() is None
    ticket = queue / f"01792318693053581600-{os.getpid()}-0"
    ticket.write_text(json.dumps({"kind": "merge", "branch": "claude/x"}), encoding="utf-8")
    assert "queue" in probe()
    ticket.unlink()

    holding = locks / "merge.lock.holding"
    holding.write_text(json.dumps({"pid": os.getpid(), "ns": 1, "info": {"kind": "merge", "branch": "claude/x"}}),
                       encoding="utf-8")
    assert "holding" in probe()
    holding.unlink()

    lock.write_text(json.dumps({"pid": os.getpid(), "kind": "merge", "branch": "claude/x"}), encoding="utf-8")
    assert "lock" in probe()
    lock.unlink()

    from tools.agentops.merge import _new_suite_output_path  # the name the suite really writes
    out = _new_suite_output_path("claude/x", suite)
    out.write_text("x", encoding="utf-8")
    os.utime(out, (since + 600, since + 600))
    assert "suite output" in probe()


def test_a_slash_branch_with_a_queue_ticket_is_left_parked_at_60_min(tmp_path):
    queue = tmp_path / "locks" / "merge.lock.queue"
    queue.mkdir(parents=True)
    (queue / f"01792318693053581600-{os.getpid()}-0").write_text(
        json.dumps({"kind": "merge", "branch": "claude/x"}), encoding="utf-8")
    w = World(tmp_path, comments=[{"createdAt": _iso(PARK_AT), "body": PARK_BODY.replace(RESULT, SLASH_RESULT)}])
    w.d.merge_trace = lambda branch, result_file, since: merge_trace_of(
        branch, since=since, lock_path=tmp_path / "locks" / "merge.lock", suite_dir=tmp_path / "none")
    w.at(60)
    assert w.gh.comments == [] and w.gh.added_labels == []
