"""#232 unit 4b (D119): the free-minutes counter of the private repo on its own GitHub runners (acceptance row 4).

Seams: `record_run`, `minutes_used`, `median_merge_minutes`, `over_budget` in `tools/agentops/github_minutes.py`.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops import core_settings
from tools.agentops import github_minutes as gm


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def row(repo, month, minutes, **extra):
    return {
        "time": f"{month}-15T12:00:00+00:00",
        "repo": repo,
        "slug": f"owner/{repo}",
        "month": month,
        "minutes": minutes,
        "machines": 1,
        "outcome": "passed",
        "run_id": 1,
        "own_runner": True,
        **extra,
    }


def write_log(path, lines):
    """`lines` is a list of dicts (written as JSON) or raw strings (written as they are)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)
    path.write_text(text, encoding="utf-8")


def u_log_1990_used_median_15(path):
    """1,990 used in October for U: one 1,840-minute run, then ten 15-minute runs (the last ten lines, so the
    median of the repo's last 10 is 15)."""
    write_log(path, [row("U", "2026-10", 1840)] + [row("U", "2026-10", 15) for _ in range(10)])
    return path


def test_row4_1990_used_and_median_15_is_over_budget_until_the_first_of_next_month(tmp_path):
    log = u_log_1990_used_median_15(tmp_path / "runs.jsonl")
    assert gm.minutes_used("U", "2026-10", path=log) == 1990
    assert gm.median_merge_minutes("U", path=log) == 15
    assert gm.over_budget("U", now=utc(2026, 10, 19, 12, 0, 0), limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2026-11-01",
    )


def test_row4_the_same_log_on_the_first_of_next_month_is_under_budget(tmp_path):
    log = u_log_1990_used_median_15(tmp_path / "runs.jsonl")
    assert gm.over_budget("U", now=utc(2026, 11, 1, 0, 0, 0), limit=2000, path=log) == (False, "")


def test_limit_default_is_the_free_minutes_setting(tmp_path):
    assert core_settings.load()["github_free_minutes"] == 2000
    log = u_log_1990_used_median_15(tmp_path / "runs.jsonl")
    assert gm.over_budget("U", now=utc(2026, 10, 19), path=log)[0] is True


def test_repo_with_no_lines_is_not_over_budget(tmp_path):
    missing = tmp_path / "no_such_runs.jsonl"
    assert gm.minutes_used("U", "2026-10", path=missing) == 0
    assert gm.median_merge_minutes("U", path=missing) == 0
    assert gm.over_budget("U", now=utc(2026, 10, 19), limit=2000, path=missing) == (False, "")


def test_another_repos_lines_never_count(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("W", "2026-10", 1990)] + [row("W", "2026-10", 15) for _ in range(10)])
    assert gm.minutes_used("U", "2026-10", path=log) == 0
    assert gm.median_merge_minutes("U", path=log) == 0
    assert gm.over_budget("U", now=utc(2026, 10, 19), limit=2000, path=log) == (False, "")


def test_month_boundary_at_23_59_59_utc_then_midnight(tmp_path):
    log = u_log_1990_used_median_15(tmp_path / "runs.jsonl")
    assert gm.over_budget("U", now=utc(2026, 10, 31, 23, 59, 59), limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2026-11-01",
    )
    assert gm.over_budget("U", now=utc(2026, 11, 1, 0, 0, 0), limit=2000, path=log) == (False, "")


def test_december_rolls_over_to_january_of_the_next_year(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("U", "2026-12", 1840)] + [row("U", "2026-12", 15) for _ in range(10)])
    assert gm.over_budget("U", now=utc(2026, 12, 31, 23, 59, 59), limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2027-01-01",
    )
    assert gm.over_budget("U", now=utc(2027, 1, 1, 0, 0, 0), limit=2000, path=log) == (False, "")


def test_bad_lines_are_skipped(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(
        log,
        [
            row("U", "2026-10", 100),
            "not json at all",
            "",
            "[1, 2, 3]",
            {"repo": "U", "month": "2026-10", "minutes": "abc"},
            {"repo": "U", "month": "2026-10", "minutes": True},
            {"repo": "U", "month": "2026-10"},
            row("U", "2026-10", 20),
        ],
    )
    assert gm.minutes_used("U", "2026-10", path=log) == 120
    assert gm.median_merge_minutes("U", path=log) == 60  # median of the two valid lines: (100 + 20) / 2


def test_median_reads_only_the_last_n_lines(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("U", "2026-10", 1000), row("U", "2026-10", 1000)] + [row("U", "2026-10", 15) for _ in range(10)])
    assert gm.median_merge_minutes("U", path=log) == 15
    assert gm.median_merge_minutes("U", last=2, path=log) == 15


def test_median_of_an_even_count_rounds_up_to_a_whole_minute(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("U", "2026-10", 10), row("U", "2026-10", 15)])
    assert gm.median_merge_minutes("U", path=log) == 13  # 12.5 rounded up


def test_record_run_then_minutes_used_round_trip(mdir):
    """Written through `record_run` into the machine dir and read back: the two own-runner lines of U count (42,
    median 21); the free-copy line of U and W's line are written but never counted for U."""
    gm.record_run(row("U", "2026-10", 40, pool="Zorblax"))
    gm.record_run(row("U", "2026-10", 2))
    gm.record_run(row("U", "2026-10", 700, own_runner=False))
    gm.record_run(row("W", "2026-10", 500))
    assert (mdir / "github_runs.jsonl").is_file()
    assert gm.minutes_used("U", "2026-10") == 42
    assert gm.median_merge_minutes("U") == 21
    first = json.loads((mdir / "github_runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["pool"] == "Zorblax"
    assert first["month"] == "2026-10" and first["minutes"] == 40


def test_zero_minute_lines_are_ignored_by_the_median_and_the_guard_still_fires(tmp_path):
    """A refused or failed send costs 0 minutes: the median reads only the lines with minutes > 0."""
    log = tmp_path / "runs.jsonl"
    write_log(
        log,
        [row("U", "2026-10", 1930)]
        + [row("U", "2026-10", 15) for _ in range(4)]
        + [row("U", "2026-10", 0) for _ in range(6)],
    )
    assert gm.median_merge_minutes("U", path=log) == 15
    assert gm.minutes_used("U", "2026-10", path=log) == 1990
    assert gm.over_budget("U", now=utc(2026, 10, 19), limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2026-11-01",
    )


def test_free_copy_lines_never_count_and_own_runner_lines_do(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(
        log,
        [row("U", "2026-10", 1500, own_runner=False)]
        + [row("U", "2026-10", 15, own_runner=False) for _ in range(10)]
        + [row("U", "2026-10", 100)],
    )
    assert gm.minutes_used("U", "2026-10", path=log) == 100
    assert gm.median_merge_minutes("U", path=log) == 100


def test_a_line_without_own_runner_counts_as_a_free_copy_run(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [{"repo": "U", "month": "2026-10", "minutes": 700}, row("U", "2026-10", 50)])
    assert gm.minutes_used("U", "2026-10", path=log) == 50
    assert gm.median_merge_minutes("U", path=log) == 50


def test_naive_now_is_taken_as_utc_and_aware_now_is_converted(tmp_path):
    log = u_log_1990_used_median_15(tmp_path / "runs.jsonl")
    naive_before = datetime(2026, 10, 31, 23, 59, 59)
    naive_after = datetime(2026, 11, 1, 0, 0, 0)
    assert gm.over_budget("U", now=naive_before, limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2026-11-01",
    )
    assert gm.over_budget("U", now=naive_after, limit=2000, path=log) == (False, "")
    plus3 = timezone(timedelta(hours=3))
    assert gm.over_budget("U", now=datetime(2026, 11, 1, 2, 59, 59, tzinfo=plus3), limit=2000, path=log)[0] is True
    assert gm.over_budget("U", now=datetime(2026, 11, 1, 3, 0, 0, tzinfo=plus3), limit=2000, path=log) == (False, "")


def test_two_threads_appending_50_lines_each_leave_100_intact_lines(mdir):
    def writer(tag):
        for seq in range(50):
            gm.record_run(row("U", "2026-10", 1, tag=tag, seq=seq, pad="x" * 4000))

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = (mdir / "github_runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    parsed = [json.loads(line) for line in lines]  # an interleaved line would not parse
    assert sorted((p["tag"], p["seq"]) for p in parsed) == sorted((t, s) for t in ("a", "b") for s in range(50))
