"""#202: a merge that just landed is never taken for one that never started.

Rows 1 and 2 of the issue. The dispatcher is driven through `tick()` (fake claude, fake gh, fake clock) with the
real default probe (`merge_trace_of`) pointed at temporary lock, suite and merge.log folders, so no real gh, merge
or clock is read. Rows 3 and 4 are the #179 tests and `core check`; row 5 is a live reading.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tools.agentops.dispatch import merge_trace_of

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_issue179_merge_park_grace import PARK_AT, World

DEAD_PID = 4000000000  # a pid that is gone


class Folders:
    """The lock, queue, batch, suite and merge.log folders of one fake installation."""

    def __init__(self, tmp: Path) -> None:
        self.locks = tmp / "locks"
        self.lock = self.locks / "merge.lock"
        self.queue = self.locks / "merge.lock.queue"
        self.batch = self.locks / "merge.lock.batch"
        self.suite = tmp / "merge_suite"
        self.log = tmp / "merge.log"
        for d in (self.queue, self.batch, self.suite):
            d.mkdir(parents=True)
        self.since = PARK_AT.timestamp()

    def probe(self, branch: str = "b"):
        return merge_trace_of(branch, since=self.since, lock_path=self.lock, suite_dir=self.suite,
                              merge_log=self.log)

    def log_line(self, names: str, *, minutes_after_park: float, reason: str = "pushed") -> None:
        at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since + minutes_after_park * 60))
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(f"{at} | {names} | {reason} | suite 47 min | 0 failed | pushed d0b7d25c6afc | "
                     f"output {self.suite}\\x.out\n")


def _world_with(tmp_path: Path, folders: Folders) -> World:
    w = World(tmp_path)
    w.d.merge_trace = lambda branch, result_file, since: merge_trace_of(
        branch, since=since, lock_path=folders.lock, suite_dir=folders.suite, merge_log=folders.log)
    return w


def _nothing_happened(w: World) -> bool:
    return w.gh.added_labels == [] and w.gh.comments == [] and w.grace_lines == []


# -- row 1: pushed to main, result not yet written -----------------------------------------------------------

def test_row1_a_batch_line_that_pushed_the_branch_is_a_trace_16_min_after_the_park(tmp_path):
    f = Folders(tmp_path)
    f.log_line("batch of 2: a, b", minutes_after_park=15.9)
    w = _world_with(tmp_path, f)
    w.at(16)
    assert _nothing_happened(w)


def test_row1_a_single_pushed_line_is_a_trace_however_long_the_result_is_missing(tmp_path):
    f = Folders(tmp_path)
    f.log_line("b", minutes_after_park=3)
    w = _world_with(tmp_path, f)
    w.at(16)
    w.at(120)
    assert _nothing_happened(w)


def test_row1_the_probe_names_the_merge_log_line(tmp_path):
    f = Folders(tmp_path)
    f.log_line("batch of 2: a, b", minutes_after_park=5)
    assert "merge.log" in f.probe()


def test_a_line_of_another_branch_or_an_earlier_attempt_is_no_trace(tmp_path):
    f = Folders(tmp_path)
    f.log_line("bb", minutes_after_park=5)  # another branch whose name ends alike
    f.log_line("batch of 2: ab, c", minutes_after_park=5)
    f.log_line("b", minutes_after_park=-60)  # a landing of the same name before this park
    assert f.probe() is None
    w = _world_with(tmp_path, f)
    w.at(16)
    assert (7, "ready-for-agent") in w.gh.added_labels and len(w.grace_comments) == 1


def test_every_final_line_naming_the_branch_is_a_trace(tmp_path):
    for n, (names, reason) in enumerate((("batch of 2: a, b", "red-suite"), ("b", "ejected: red-suite tests/x.py::t"),
                          ("b", "ejected: target-check x"), ("b", "red-on-branch"), ("b", "dirty"),
                          ("b", "lock-timeout"), ("b", "nothing-to-merge"))):
        f = Folders(tmp_path / f"final{n}")
        f.log_line(names, minutes_after_park=5, reason=reason)
        assert f.probe() is not None, (names, reason)


def test_a_waiting_line_is_no_trace_and_ends_an_earlier_final_one(tmp_path):
    for reason in ("left in line: behind a Priority ticket", "re-queued after red batch",
                   "cut for a Priority ticket (#9): back in line at its place"):
        f = Folders(tmp_path / reason[:6].replace(" ", "_"))
        f.log_line("b", minutes_after_park=5, reason=reason)
        assert f.probe() is None, reason
        f2 = Folders(tmp_path / ("after_" + reason[:6].replace(" ", "_")))
        f2.log_line("batch of 2: a, b", minutes_after_park=4, reason="red-suite")
        f2.log_line("b", minutes_after_park=6, reason=reason)  # the red batch put it back in line
        assert f2.probe() is None, reason


def test_an_informational_line_neither_makes_nor_hides_a_trace(tmp_path):
    f = Folders(tmp_path)
    f.log_line("b", minutes_after_park=5, reason="priority not written (OSError: x)")
    f.log_line("b", minutes_after_park=5, reason="blockers not read: #9 (X)")
    assert f.probe() is None
    f.log_line("b", minutes_after_park=6, reason="pushed")
    f.log_line("b", minutes_after_park=7, reason="priority not written (OSError: x)")
    assert f.probe() is not None


def test_a_slash_branch_is_found_in_the_merge_log_by_its_result_label(tmp_path):
    f = Folders(tmp_path)
    f.log_line("batch of 2: a, claude/x", minutes_after_park=5)
    assert f.probe("claude_x") is not None


def test_a_missing_merge_log_is_no_trace_and_raises_nothing(tmp_path):
    f = Folders(tmp_path)
    assert f.probe() is None


# -- row 2: a batch member while the head's suite runs ---------------------------------------------------------

def _batch_in_flight(f: Folders, *, claim_pid: int, head_output: bool = True, lock_pid: int | None = None) -> None:
    """Head `h` holds the lock (a live pid) and its suite output exists; member `b` has no suite output of its own,
    its waiter is gone from the live pids, and a batch claim of the holder names its queue ticket."""
    lock_pid = os.getpid() if lock_pid is None else lock_pid
    f.lock.write_text(json.dumps({"pid": lock_pid, "kind": "merge", "branch": "h"}), encoding="utf-8")
    if head_output:
        out = f.suite / "20261018T122000-000000-h.out"
        out.write_text("x", encoding="utf-8")
        os.utime(out, (f.since + 60, f.since + 60))
    ticket = f"01792318693053581600-{DEAD_PID}-0"
    (f.queue / ticket).write_text(json.dumps({"kind": "merge", "branch": "b"}), encoding="utf-8")
    (f.batch / f"{ticket}.claim").write_text(
        json.dumps({"pid": claim_pid, "token": "t", "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8")


def test_row2_a_member_counts_as_having_suite_output_while_the_heads_exists_at_60_min(tmp_path):
    f = Folders(tmp_path)
    _batch_in_flight(f, claim_pid=os.getpid())
    assert not [p for p in f.suite.iterdir() if p.name.endswith("-b.out")]  # b has no output of its own
    w = _world_with(tmp_path, f)
    w.at(60)
    assert _nothing_happened(w)
    assert "suite output" in f.probe() and "-h.out" in f.probe()


def test_row2_without_the_heads_suite_output_a_member_is_no_trace(tmp_path):
    f = Folders(tmp_path)
    _batch_in_flight(f, claim_pid=os.getpid(), head_output=False)
    assert f.probe() is None


def test_row2_a_claim_of_another_holder_than_the_lock_names_no_batch(tmp_path):
    f = Folders(tmp_path)
    _batch_in_flight(f, claim_pid=DEAD_PID)  # the lock is held by this process, the claim names another
    assert f.probe() is None


def test_row2_a_lock_of_a_dead_head_names_no_batch(tmp_path):
    f = Folders(tmp_path)
    _batch_in_flight(f, claim_pid=DEAD_PID, lock_pid=DEAD_PID)
    assert f.probe() is None


def test_row2_the_head_itself_is_still_found_by_its_suite_output(tmp_path):
    f = Folders(tmp_path)
    _batch_in_flight(f, claim_pid=os.getpid())
    assert f.probe("h") is not None


# -- row 3 of #179 stays: no trace at all for 15 min goes back to ready -------------------------------------------

def test_no_trace_at_all_still_goes_back_to_ready_with_one_comment(tmp_path):
    f = Folders(tmp_path)
    w = _world_with(tmp_path, f)
    w.at(14)
    assert _nothing_happened(w)
    w.at(16)
    assert (7, "ready-for-agent") in w.gh.added_labels and len(w.grace_comments) == 1
    assert w.probes == []  # the probe above is the real one, not the World's recorder
