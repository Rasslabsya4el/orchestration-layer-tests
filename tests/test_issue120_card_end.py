"""#120 unit 2 (card-end): the Merge card's header queue end is the command's `queue_end_s` (merge-status --json,
a top-level int: seconds from now to the end of the queue), not a formula over the waiters. The card end equals the
command's queue end whatever the waiter starts are. Fixtures are plain dicts shaped as the command prints them;
0 PoB, no subprocess."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tools.agentops import merge_status

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"
# the median suite the collector would take from merge.log; pinned away from 1320 s so that the old
# `waiting[-1] + median` formula gives a different end than the command's queue end (red before the change)
PINNED_MEDIAN_S = 600.0


@pytest.fixture(autouse=True)
def _pinned_median(monkeypatch):
    monkeypatch.setattr(merge_status, "_log_facts", lambda path: ([PINNED_MEDIAN_S], None))


def _collect():
    spec = importlib.util.spec_from_file_location("collect_120", COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _running(eta: int) -> dict:
    # the running batch's row as merge_status.collect writes it (only the keys the card reads here)
    return {"kind": "batch", "message": "", "branches": ["orch-96"], "issues": [96], "phase": "suite",
            "percent": 40, "tests_done": 600, "tests_total": 1500, "failed": 0, "eta_s": eta,
            "started": "2026-10-17T16:52:00+02:00", "workers": 6, "suite_out": None}


def _waiter(branch: str, eta_start: int) -> dict:
    return {"branch": branch, "issues": [], "queued": "2026-10-17T16:40:00+02:00", "waited_s": 720,
            "eta_start_s": eta_start}


def test_card_end_is_the_commands_queue_end_with_six_waiters_in_one_batch() -> None:
    # fixture 1: one batch running (eta 120 s), six compatible waiters all starting at 120 s, queue end 1440 s
    data = {"at": "2026-10-17T17:00:00+02:00", "running": _running(120),
            "waiting": [_waiter(f"orch-{k}", 120) for k in range(6)], "queue_end_s": 1440}
    out = _collect().merge_shape(data)
    assert out["queue_end_h"] == _collect().hd(1440)


def test_card_end_is_the_commands_queue_end_with_a_commit_between_the_waiters() -> None:
    # fixture 2: waiters 1-3 at 120 s, a commit (its own suite) at 1440 s, waiters 4-6 at 2760 s, queue end 4080 s
    starts = [120, 120, 120, 2760, 2760, 2760]
    data = {"at": "2026-10-17T17:00:00+02:00", "running": _running(120),
            "waiting": [_waiter(f"orch-{k}", s) for k, s in enumerate(starts)], "queue_end_s": 4080}
    out = _collect().merge_shape(data)
    assert out["queue_end_h"] == _collect().hd(4080)


def test_card_end_is_the_running_eta_when_the_command_has_no_queue_end() -> None:
    # an older command: no queue_end_s key -> the running batch's eta only, never a formula over the waiters
    data = {"at": "2026-10-17T17:00:00+02:00", "running": _running(120),
            "waiting": [_waiter(f"orch-{k}", 120) for k in range(6)]}
    out = _collect().merge_shape(data)
    assert out["queue_end_h"] == _collect().hd(120)


@pytest.mark.parametrize("entries, starts, end", [
    ([("s",)] * 6, [120] * 6, 1440),  # row 1: six compatible waiters, one batch
    ([("s",)] * 3 + [None] + [("s",)] * 3, [120, 120, 120, 1440, 2760, 2760, 2760], 4080),  # row 2: a suite-needing commit
    ([("s",)] * 4 + [("x",)] + [("s",)], [120] * 4 + [1440, 120], 2760),  # row 3: waiter 5 has another main
])
def test_card_end_equals_the_commands_queue_end_through_the_real_batch_numbers(entries, starts, end) -> None:
    # the command's own numbers (running eta 120 s, median 1320 s) go into the card unchanged
    got_starts, got_end = merge_status.batch_starts(entries, 120, 1320)
    assert [round(s) for s in got_starts] == starts and round(got_end) == end
    waiters = [_waiter(f"orch-{k}", round(s)) for k, (e, s) in enumerate(zip(entries, got_starts)) if e is not None]
    data = {"at": "2026-10-17T17:00:00+02:00", "running": _running(120), "waiting": waiters, "queue_end_s": round(got_end)}
    assert _collect().merge_shape(data)["queue_end_h"] == _collect().hd(end)
