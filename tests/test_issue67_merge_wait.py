"""#67 unit 2: `checkpoint_merge_wait` counts the result right before the park as posted only when the
park names no result file or the same one (rows 6-7)."""

from __future__ import annotations

from tools.agentops.dispatch import checkpoint_merge_wait

IDS = ("abc12345",)


def _c(at: str, body: str) -> dict:
    return {"createdAt": at, "body": body}


def _result(name: str) -> dict:
    return _c("2026-10-16T16:54:00Z", f"<!-- merge result -->\nMerge of i372 did not land. Result C:/r/results/{name}; suite -.")


def _park(name: str | None) -> dict:
    where = f" var/agentops/results/{name}" if name else ""
    return _c("2026-10-16T16:59:00Z", f"ORCH abc12345\n<!-- parked-on-run: parked on merge{where} -->")


def test_row6_park_names_another_file_than_the_result_before_it():
    wait = checkpoint_merge_wait([_result("A.json"), _park("B.json")], IDS)
    assert wait is not None and wait.result_file == "var/agentops/results/B.json" and wait.result_posted is False


def test_row7_same_file_in_both_keeps_the_874_case():
    assert checkpoint_merge_wait([_result("A.json"), _park("A.json")], IDS).result_posted is True


def test_a_park_naming_no_file_keeps_the_874_case():
    assert checkpoint_merge_wait([_result("A.json"), _park(None)], IDS).result_posted is True


def test_a_result_after_the_park_still_counts_whatever_it_names():
    comments = [_result("A.json"), _park("B.json"), _result("B.json")]
    assert checkpoint_merge_wait(comments, IDS).result_posted is True
