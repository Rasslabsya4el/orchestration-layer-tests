"""#116 unit 2 (B1): the collector gives a did-not-land merge its `ended` (the card's HH:MM); `reason` and `failed` come
with the merge-status row (its `failed` is the attempt's own red-suite count, merge_status.py `_landed`), and a landed
row gets neither `ended` nor `failed`. Fixtures are shaped like the 17.10 status row; 0 PoB, no subprocess."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"


def _collect():
    spec = importlib.util.spec_from_file_location("collect_116", COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _status(branch: str, rc: int, reason: str | None, landed: str, failed: int | None = None) -> dict:
    # one row of `merge-status --json`'s `landed` (merge_status.py `_landed`)
    row = {"branch": branch, "issues": [109], "queued": "2026-10-17T15:52:59", "landed": landed,
           "queued_to_land_s": 2660, "rc": rc, "reason": reason, "attempts": 1, "failed_before": 0}
    if failed is not None:
        row["failed"] = failed
    return row


def test_did_not_land_row_gets_ended_and_keeps_reason_and_failed() -> None:
    data = {"at": "2026-10-17T17:00:00+02:00",
            "landed": [_status("i109", 1, "red-suite", "2026-10-17T16:37:19", failed=2)]}
    row = _collect().merge_shape(data)["landed"][0]
    assert row["ended"] == "2026-10-17T16:37:19"
    assert row["reason"] == "red-suite"
    assert row["failed"] == 2
    assert row["took_h"], "the duration column is filled for a did-not-land row too"


def test_did_not_land_conflict_has_its_end_and_no_failed_count() -> None:
    data = {"at": "2026-10-17T17:00:00+02:00",
            "landed": [_status("i109", 1, "conflict", "2026-10-17T16:37:19")]}
    row = _collect().merge_shape(data)["landed"][0]
    assert row["reason"] == "conflict"
    assert row["ended"] == "2026-10-17T16:37:19"
    assert row.get("failed") is None


def test_landed_row_keeps_its_fields_and_gets_no_ended_or_failed() -> None:
    data = {"at": "2026-10-17T17:00:00+02:00",
            "landed": [_status("i109", 0, None, "2026-10-17T16:37:19")]}
    row = _collect().merge_shape(data)["landed"][0]
    assert row["rc"] == 0
    assert "ended" not in row and "failed" not in row
