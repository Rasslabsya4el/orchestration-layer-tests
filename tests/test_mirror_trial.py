"""#425 unit 5: the pure helpers of the live trial `tests/mirror_trial.py` (its table, its summary, its exit code, the
guard that keeps every write on the throwaway repo, the first-line match of a mirror). The live run itself is the
trial's own check and is not a pytest case: no test here reaches GitHub."""

from __future__ import annotations

from tests.mirror_trial import (
    REPO, ROWS, exit_code, is_mirror_of, row_line, summary_line, write_refusal,
)

URL = "https://github.com/owner/orchestration-layer-tests/issues/7"


def test_row_line_prints_the_four_fields_and_pass_or_fail():
    assert row_line(2, "three openers", "3 of 3", True) == "row 2 | three openers | 3 of 3 | pass"
    assert row_line(5, "close both ways", "2 of 4", False) == "row 5 | close both ways | 2 of 4 | FAIL"


def test_summary_counts_passes_out_of_six_rows():
    assert ROWS == 6
    assert summary_line([True, True, False, True, True, True]) == "mirror-trial: 5 of 6 rows pass"
    assert summary_line([True] * 6) == "mirror-trial: 6 of 6 rows pass"


def test_exit_code_is_one_on_any_fail_or_any_missing_row():
    assert exit_code([True] * 6) == 0
    assert exit_code([True] * 5 + [False]) == 1
    assert exit_code([True] * 5) == 1  # a row that never printed counts as a failure


def test_write_guard_refuses_a_write_to_another_repo_and_allows_the_throwaway():
    assert write_refusal(["issue", "close", "7", "--repo", REPO], REPO) is None
    assert write_refusal(["issue", "create", "-R", REPO, "--title", "t"], REPO) is None
    other = write_refusal(["issue", "close", "7", "--repo", "Rasslabsya4el/orch-it"], REPO)
    assert other is not None and "orch-it" in other
    assert write_refusal(["label", "create", "mirror", "--repo", "Rasslabsya4el/other"], REPO) is not None


def test_write_guard_refuses_a_write_with_no_repo_and_a_non_get_api_call():
    assert write_refusal(["issue", "comment", "7", "--body", "x"], REPO) is not None
    assert write_refusal(["api", "repos/x/y/issues/7", "-X", "PATCH"], REPO) is not None


def test_reads_of_any_repo_pass_the_guard():
    assert write_refusal(["issue", "list", "--repo", "Rasslabsya4el/this-repo-does-not-exist-425"], REPO) is None
    assert write_refusal(["api", "repos/owner/x/issues/7"], REPO) is None
    assert write_refusal(["issue", "view", "7", "--repo", "Rasslabsya4el/orch-it", "--json", "state"], REPO) is None


def test_mirror_match_reads_only_the_first_line_and_the_whole_url():
    body = f"Mirror of {URL}; the source holds its history and comments.\n\nthe source body"
    assert is_mirror_of(body, URL)
    # a URL that is only a prefix of another (issue 7 vs issue 70) is a different source
    assert not is_mirror_of(body, URL + "0")
    # the URL on a later line is not a mirror line
    assert not is_mirror_of("the body\n" + f"Mirror of {URL}; the source holds its history and comments.", URL)
    assert not is_mirror_of("", URL)
