"""Tests for scripts/transfer_issues.py (#12): every gh call goes through a fake runner, every pause through a fake sleep."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from scripts import transfer_issues as ti

FROM = "Rasslabsya4el/Poe-Junkroan"
TO = "Rasslabsya4el/orchestration-layer"

RATE_LIMIT = (1, "", "GraphQL: You have exceeded a secondary rate limit. Please wait.")
HTTP_429 = (1, "", "HTTP 429: Too Many Requests")
NOT_FOUND = (1, "", "GraphQL: issue not found")
BOOM = (1, "", "network reset by peer")


class FakeGh:
    """Scripted gh: a call on issue N pops the next reply queued under str(N); with none queued it succeeds."""

    def __init__(self, replies: dict[str, list[tuple[int, str, str]]] | None = None, list_json: str = "[]"):
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.list_json = list_json
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        queue = self.replies.get(args[2] if len(args) > 2 else "")
        if queue:
            return queue.pop(0)
        if args[:2] == ["issue", "transfer"]:
            n, to = int(args[2]), args[3]
            return 0, f"https://github.com/{to}/issues/{5000 + n}\n", ""
        if args[:2] == ["issue", "list"]:
            return 0, self.list_json, ""
        return 0, "", ""

    def transferred(self) -> list[int]:
        return [int(a[2]) for a in self.calls if a[:2] == ["issue", "transfer"]]


def write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def transfer_argv(lst, log, *extra: str) -> list[str]:
    return ["transfer", "--from-repo", FROM, "--to-repo", TO, "--list", str(lst), "--log", str(log), *extra]


def read_rows(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_transfers_listed_numbers_in_order_and_logs_each(tmp_path):
    lst = write(tmp_path / "list.txt", "11, 12\n13")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh(), []

    rc = ti.main(transfer_argv(lst, log, "--gap", "10"), runner=gh, sleep=sleeps.append)

    assert rc == 0
    assert gh.transferred() == [11, 12, 13]
    assert gh.calls[0] == ["issue", "transfer", "11", TO, "-R", FROM]
    rows = read_rows(log)
    assert [(r["old"], r["new"]) for r in rows] == [(11, 5011), (12, 5012), (13, 5013)]
    assert all(r["from"] == FROM and r["to"] == TO for r in rows)
    for r in rows:
        assert "T" in r["at"]  # ISO local time
    assert sleeps == [10, 10, 10]


def test_second_run_with_same_log_transfers_nothing_already_logged(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    ti.main(transfer_argv(lst, log), runner=FakeGh(), sleep=lambda s: None)

    gh = FakeGh()
    assert ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None) == 0
    assert gh.calls == []

    write(lst, "11 12 13")
    gh = FakeGh()
    assert ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None) == 0
    assert gh.transferred() == [13]
    assert [r["old"] for r in read_rows(log)] == [11, 12, 13]


def test_last_moves_that_number_after_all_others(tmp_path):
    lst = write(tmp_path / "list.txt", "1076 11 12 13")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh()

    assert ti.main(transfer_argv(lst, log, "--last", "1076"), runner=gh, sleep=lambda s: None) == 0
    assert gh.transferred() == [11, 12, 13, 1076]


def test_rate_limit_backs_off_60_120_240_480_then_stops_nonzero(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh({"11": [RATE_LIMIT] * 5}), []

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=sleeps.append)

    assert rc != 0
    assert gh.transferred() == [11, 11, 11, 11, 11]  # the 5th failure stops the run
    assert sleeps == [60, 120, 240, 480]
    rows = read_rows(log)
    assert rows == [{"old": 11, "error": rows[0]["error"]}]
    assert "rate limit" in rows[0]["error"]


def test_rate_limit_then_success_continues_the_run(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh({"11": [RATE_LIMIT, HTTP_429]}), []

    rc = ti.main(transfer_argv(lst, log, "--gap", "10"), runner=gh, sleep=sleeps.append)

    assert rc == 0
    assert gh.transferred() == [11, 11, 11, 12]
    assert sleeps == [60, 120, 10, 10]  # two back-offs, then the gap after each success
    assert [(r["old"], r["new"]) for r in read_rows(log)] == [(11, 5011), (12, 5012)]


def test_not_found_is_skipped_and_logged_and_the_run_goes_on(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12 13")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"12": [NOT_FOUND]})

    assert ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None) == 0
    assert gh.transferred() == [11, 12, 13]
    rows = read_rows(log)
    assert [r["old"] for r in rows] == [11, 12, 13]
    assert "skipped" in rows[1] and "not found" in rows[1]["skipped"]
    assert "new" not in rows[1]

    gh = FakeGh()  # a skipped number is done: the next run does not retry it
    ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None)
    assert gh.calls == []


def test_three_other_errors_in_a_row_stop_the_run_nonzero(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12 13 14 15")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"11": [BOOM], "12": [BOOM], "13": [BOOM]})

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None)

    assert rc != 0
    assert gh.transferred() == [11, 12, 13]
    assert [r["old"] for r in read_rows(log)] == [11, 12, 13]
    assert all("error" in r for r in read_rows(log))


def test_relabel_edits_each_mapping_once_and_resumes(tmp_path):
    mapping = write(
        tmp_path / "map.txt",
        "1074=area:cli\n1075=area:core\n# comment line\n" + json.dumps({"n": 1076, "label": "area:docs"}) + "\n",
    )
    log = tmp_path / "relabel.jsonl"
    gh, sleeps = FakeGh(), []
    argv = ["relabel", "--repo", TO, "--map", str(mapping), "--remove", "area:orchestration", "--log", str(log)]

    assert ti.main(argv, runner=gh, sleep=sleeps.append) == 0
    assert gh.calls == [
        ["issue", "edit", "1074", "-R", TO, "--remove-label", "area:orchestration", "--add-label", "area:cli"],
        ["issue", "edit", "1075", "-R", TO, "--remove-label", "area:orchestration", "--add-label", "area:core"],
        ["issue", "edit", "1076", "-R", TO, "--remove-label", "area:orchestration", "--add-label", "area:docs"],
    ]
    assert sleeps == [3, 3, 3]  # --gap defaults to 3 for relabel
    assert [(r["n"], r["label"]) for r in read_rows(log)] == [
        (1074, "area:cli"),
        (1075, "area:core"),
        (1076, "area:docs"),
    ]

    gh = FakeGh()  # resume: everything logged is done
    assert ti.main(argv, runner=gh, sleep=lambda s: None) == 0
    assert gh.calls == []


def test_count_prints_the_length_of_the_list(tmp_path, capsys):
    gh = FakeGh(list_json=json.dumps([{"number": 1}, {"number": 2}, {"number": 9}]))

    rc = ti.main(["count", "--repo", TO, "--label", "area:orchestration"], runner=gh, sleep=lambda s: None)

    assert rc == 0
    assert capsys.readouterr().out.strip() == "3"
    assert gh.calls == [
        ["issue", "list", "-R", TO, "--label", "area:orchestration", "--state", "all", "--limit", "5000", "--json", "number"]
    ]


def test_real_runner_has_no_shell_and_no_console_window(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(ti.subprocess, "run", fake_run)

    assert ti.real_runner(["issue", "list"]) == (0, "ok", "")
    assert seen["argv"] == ["gh", "issue", "list"]
    assert not seen["kwargs"].get("shell", False)
    if os.name == "nt":
        assert seen["kwargs"]["creationflags"] == 0x08000000
    else:
        assert "creationflags" not in seen["kwargs"]


# --- review send-back (#12 unit 2): nine points -------------------------------------------------

TIMEOUT = (124, "", "timeout")


@pytest.mark.parametrize(
    "text",
    [
        "GraphQL: Could not resolve to a Repository with the name 'Rasslabsya4el/Poe-Junkroan'.",
        "HTTP 404: Not Found (https://api.github.com/repos/Rasslabsya4el/Poe-Junkroan)",
        "Resource not accessible by integration",
        "HTTP 401: Bad credentials",
    ],
)
def test_repo_or_auth_404_stops_the_run_and_is_never_a_skip(tmp_path, text):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"11": [(1, "", text)]})

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None)

    assert rc != 0
    assert gh.transferred() == [11]
    rows = read_rows(log)
    assert len(rows) == 1 and "error" in rows[0] and "skipped" not in rows[0]


def test_per_issue_not_found_is_a_skip_not_a_stop(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"11": [(1, "", "GraphQL: Could not resolve to an Issue with the number of 11.")]})

    assert ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None) == 0
    assert gh.transferred() == [11, 12]
    assert "skipped" in read_rows(log)[0]


def test_plain_403_stops_at_once_with_no_back_off(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh({"11": [(1, "", "HTTP 403: Must have admin rights to Repository.")]}), []

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=sleeps.append)

    assert rc != 0
    assert gh.transferred() == [11]
    assert sleeps == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GraphQL: You have exceeded a secondary rate limit", True),
        ("HTTP 429: Too Many Requests", True),
        ("HTTP 403: You have exceeded a secondary rate limit", True),
        ("HTTP 403: Forbidden; secondary rate", True),
        ("HTTP 403: Must have admin rights", False),
        ("GraphQL: issue 14290 not found", False),
    ],
)
def test_rate_limit_markers(text, expected):
    assert ti.is_rate_limit(text) is expected


def test_transfer_resume_key_is_the_repo_and_the_number(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    write(
        log,
        json.dumps({"old": 11, "new": 900, "from": "Rasslabsya4el/Other", "to": TO, "at": "x"})
        + "\n"
        + json.dumps({"old": 12, "new": 901})
        + "\n",
    )
    gh = FakeGh()

    assert ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None) == 0
    assert gh.transferred() == [11]  # 11 is logged for another repo; 12 has no repo and counts for FROM


def test_relabel_resume_key_is_the_repo_and_the_number(tmp_path):
    mapping = write(tmp_path / "map.txt", "1074=area:cli\n1075=area:core\n")
    log = tmp_path / "relabel.jsonl"
    write(log, json.dumps({"n": 1074, "label": "area:cli", "repo": "Rasslabsya4el/Other"}) + "\n")
    gh = FakeGh()

    rc = ti.main(
        ["relabel", "--repo", TO, "--map", str(mapping), "--remove", "area:orchestration", "--log", str(log)],
        runner=gh,
        sleep=lambda s: None,
    )

    assert rc == 0
    assert [c[2] for c in gh.calls] == ["1074", "1075"]


def test_append_log_writes_a_missing_newline_first(tmp_path):
    lst = write(tmp_path / "list.txt", "12")
    log = tmp_path / "moved.jsonl"
    write(log, '{"old": 11, "new": 5011, "from": "%s", "to": "%s", "at": "x"}' % (FROM, TO))  # no newline at end

    assert ti.main(transfer_argv(lst, log), runner=FakeGh(), sleep=lambda s: None) == 0
    assert [r["old"] for r in read_rows(log)] == [11, 12]


def test_listed_number_without_a_done_row_makes_the_exit_nonzero(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"11": [BOOM]})

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None)

    assert rc != 0
    assert gh.transferred() == [11, 12]  # one other error does not stop the run
    rows = read_rows(log)
    assert rows[0] == {"old": 11, "error": "network reset by peer"}
    assert rows[1]["old"] == 12 and rows[1]["new"] == 5012


def test_relabel_three_consecutive_errors_stop_the_run(tmp_path):
    mapping = write(tmp_path / "map.txt", "1074=a\n1075=b\n1076=c\n1077=d\n")
    log = tmp_path / "relabel.jsonl"
    gh = FakeGh({"1074": [BOOM], "1075": [BOOM], "1076": [BOOM]})

    rc = ti.main(
        ["relabel", "--repo", TO, "--map", str(mapping), "--remove", "area:orchestration", "--log", str(log)],
        runner=gh,
        sleep=lambda s: None,
    )

    assert rc != 0
    assert [c[2] for c in gh.calls] == ["1074", "1075", "1076"]


def test_relabel_mapping_without_a_done_row_makes_the_exit_nonzero(tmp_path):
    mapping = write(tmp_path / "map.txt", "1074=a\n1075=b\n")
    log = tmp_path / "relabel.jsonl"
    gh = FakeGh({"1074": [BOOM]})

    rc = ti.main(
        ["relabel", "--repo", TO, "--map", str(mapping), "--remove", "area:orchestration", "--log", str(log)],
        runner=gh,
        sleep=lambda s: None,
    )

    assert rc != 0
    assert [c[2] for c in gh.calls] == ["1074", "1075"]
    rows = read_rows(log)
    assert rows[0] == {"n": 1074, "error": "network reset by peer"}
    assert rows[1]["n"] == 1075 and rows[1]["label"] == "b"


def test_parse_map_accepts_both_forms_and_skips_hash_comments():
    text = '1074=area:cli\n#1075=area:core\n# a note about this block\n{"n": 1076, "label": "area:docs"}\n\n'
    assert ti.parse_map(text) == [(1074, "area:cli"), (1075, "area:core"), (1076, "area:docs")]


def test_parse_map_malformed_line_raises_a_clear_error_with_its_line_number():
    with pytest.raises(ValueError, match="line 2"):
        ti.parse_map("1074=area:cli\nnot a mapping\n")
    with pytest.raises(ValueError, match="line 1"):
        ti.parse_map('{"n": ')


def test_main_reports_a_malformed_map_without_a_traceback(tmp_path, capsys):
    mapping = write(tmp_path / "map.txt", "garbage line\n")
    gh = FakeGh()

    rc = ti.main(
        ["relabel", "--repo", TO, "--map", str(mapping), "--remove", "x", "--log", str(tmp_path / "l.jsonl")],
        runner=gh,
        sleep=lambda s: None,
    )

    err = capsys.readouterr().err
    assert rc != 0 and gh.calls == []
    assert "line 1" in err and "Traceback" not in err


def test_one_timeout_then_success_continues_after_one_back_off(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh({"11": [TIMEOUT]}), []

    rc = ti.main(transfer_argv(lst, log, "--gap", "10"), runner=gh, sleep=sleeps.append)

    assert rc == 0
    assert gh.transferred() == [11, 11, 12]
    assert sleeps == [60, 10, 10]


def test_two_timeouts_count_as_an_error_and_the_run_goes_on(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh, sleeps = FakeGh({"11": [TIMEOUT, TIMEOUT]}), []

    rc = ti.main(transfer_argv(lst, log, "--gap", "10"), runner=gh, sleep=sleeps.append)

    assert rc != 0
    assert gh.transferred() == [11, 11, 12]
    assert sleeps == [60, 10]
    rows = read_rows(log)
    assert rows[0] == {"old": 11, "error": "timeout"} and rows[1]["new"] == 5012


def test_real_runner_timeout_is_124_and_has_a_120_second_limit(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(ti.subprocess, "run", fake_run)

    assert ti.real_runner(["issue", "list"]) == (124, "", "timeout")
    assert seen["kwargs"]["timeout"] == 120


def test_transfer_without_a_parsed_url_is_logged_done_and_stops(tmp_path):
    lst = write(tmp_path / "list.txt", "11 12")
    log = tmp_path / "moved.jsonl"
    gh = FakeGh({"11": [(0, "moved, but no link printed\n", "")]})

    rc = ti.main(transfer_argv(lst, log), runner=gh, sleep=lambda s: None)

    assert rc != 0
    assert gh.transferred() == [11]  # the run stops right after this row
    rows = read_rows(log)
    assert len(rows) == 1 and rows[0]["old"] == 11 and rows[0]["new"] is None
    assert rows[0]["raw"] == "moved, but no link printed\n"

    gh2 = FakeGh()  # the transfer happened: a rerun does not move 11 again
    assert ti.main(transfer_argv(lst, log), runner=gh2, sleep=lambda s: None) == 0
    assert gh2.transferred() == [12]


def test_parse_numbers_splits_skips_comments_and_dedupes():
    text = "11, 12  13\n#14\n# a comment line\n// another comment\n15 16,11\n"
    assert ti.parse_numbers(text) == [11, 12, 13, 14, 15, 16]


@pytest.mark.parametrize("bad", ["12abc", "1.5", "issue-12"])
def test_parse_numbers_rejects_other_tokens(bad):
    with pytest.raises(ValueError):
        ti.parse_numbers(bad)


def test_count_fails_when_the_result_hits_the_limit(capsys):
    gh = FakeGh(list_json=json.dumps([{"number": i} for i in range(5000)]))

    rc = ti.main(["count", "--repo", TO, "--label", "x"], runner=gh, sleep=lambda s: None)

    assert rc == 1
    assert "count failed" in capsys.readouterr().err


def test_count_fails_on_bad_json(capsys):
    gh = FakeGh(list_json="not json")

    rc = ti.main(["count", "--repo", TO, "--label", "x"], runner=gh, sleep=lambda s: None)

    assert rc == 1
    assert "count failed" in capsys.readouterr().err
