"""#67: `_unseen_merge_result` returns a merge result as unseen right before a "parked on merge"
checkpoint only when that checkpoint waits on the same merge (the same result file) or names no file.
A checkpoint naming a different result file means the result was already answered by a new merge: None.
Driven on the comment lists directly; no `gh`, no heavy tool."""

from __future__ import annotations

from tools.agentops import merge_handback
from tools.agentops.dispatch import _unseen_merge_result

FILE_A = "var/agentops/results/20261016T170123-merge-i67.json"
FILE_B = "var/agentops/results/20261016T172831-merge-i67.json"


def _result(result_file: str, at: str = "2026-10-16T17:00:00Z") -> dict:
    """A real `<!-- merge result -->` comment, built by the same code that posts it."""
    body = merge_handback._body({"rc": 1, "reason": "lock-timeout"}, result_file, "i67")
    return {"body": body, "createdAt": at}


def _park(text: str, at: str = "2026-10-16T17:05:00Z") -> dict:
    return {"body": f"ORCH deadbeef, #67 checkpoint: {text}", "createdAt": at}


def test_result_then_park_on_another_file_is_answered(tmp_path):
    """Row 1: the park waits on file B, the result named file A: the result was answered, so None."""
    result = _result(FILE_A)
    assert _unseen_merge_result([result, _park(f"parked on merge {FILE_B}; the merge is queued.")]) is None


def test_result_then_park_on_the_same_file_is_unseen(tmp_path):
    """Row 2: the park waits on the same result file A: the result is unseen, returned."""
    result = _result(FILE_A)
    assert _unseen_merge_result([result, _park(f"parked on merge {FILE_A}; the merge is queued.")]) is result


def test_result_then_park_naming_no_file_is_unseen(tmp_path):
    """Row 3: the park names no result file: the result is unseen (today's behaviour), returned."""
    result = _result(FILE_A)
    assert _unseen_merge_result([result, _park("parked on merge, waiting for the merge result.")]) is result


def test_newest_result_alone_is_unseen(tmp_path):
    """Row 4: the result is the newest comment: returned."""
    result = _result(FILE_A)
    assert _unseen_merge_result([result]) is result


def test_backslash_result_path_and_slash_park_path_match_by_basename(tmp_path):
    """Row 5: the result names its file with Windows separators, the park with forward slashes; the
    basenames are the same file, so the result is unseen, returned."""
    result = _result(FILE_A.replace("/", "\\"))
    assert _unseen_merge_result([result, _park(f"parked on merge {FILE_A}; the merge is queued.")]) is result


def test_park_with_result_file_wording_same_file_is_unseen_other_file_is_not():
    """Review fix: the park shape `merge of i372, result file <path>` (no 'parked on merge') is judged too."""
    from tools.agentops.dispatch import _unseen_merge_result
    result = {"body": "<!-- merge result -->\nMerge landed. Result C:/r/A.json; suite -."}
    same = {"body": "<!-- parked-on-run: merge of i372, result file var/agentops/results/a.json -->"}
    other = {"body": "<!-- parked-on-run: merge of i372, result file var/agentops/results/B.json -->"}
    assert _unseen_merge_result([result, same]) is result  # basename case-insensitive
    assert _unseen_merge_result([result, other]) is None


def test_result_without_a_file_name_stays_unseen():
    from tools.agentops.dispatch import _unseen_merge_result
    result = {"body": "<!-- merge result -->\nlanded."}
    park = {"body": "<!-- parked-on-run: parked on merge C:/r/B.json -->"}
    assert _unseen_merge_result([result, park]) is result
