"""#151 (units-core, unit 2): pure helpers beside `unit_progress`.

    units_by_issue(outcomes) -> {issue: {done, total}} for every int issue with a k/n row
    cut_table_total(comment_bodies) -> n of the newest ORCH cut-table comment, or None

    python -m pytest -q tests/test_issue151_units_core.py
"""
from tools.agentops import monitor_agents as ma


def _row(ts: int, issue, unit: str | None, cls: str) -> dict:
    row = {"ts": ts, "issue": issue, "route": "worker", "class": cls, "why": "", "session": "s"}
    if unit is not None:
        row["unit"] = unit
    return row


# #136: three units, all accepted (the cut of issue 136)
ROWS_136 = [
    _row(1, 136, "1/3", "accepted"),
    _row(2, 136, "2/3", "accepted"),
    _row(3, 136, "3/3", "accepted"),
]

# #148: two units of a 4-cut, both redone
ROWS_148 = [
    _row(1, 148, "1/4", "redone"),
    _row(2, 148, "2/4", "redone"),
]


def test_units_by_issue_136_all_accepted():
    assert ma.units_by_issue(ROWS_136) == {136: {"done": 3, "total": 3}}


def test_units_by_issue_148_both_redone_gives_two_of_four():
    assert ma.units_by_issue(ROWS_148) == {148: {"done": 2, "total": 4}}  # #160: redone counts as done


def test_units_by_issue_collects_every_issue_with_a_kn_row():
    rows = ROWS_136 + ROWS_148
    assert ma.units_by_issue(rows) == {
        136: {"done": 3, "total": 3},
        148: {"done": 2, "total": 4},  # #160
    }


def test_issue_with_one_unit_is_absent():
    # a cut of one unit has total 1, which unit_progress answers None for
    rows = [_row(1, 149, "1/1", "accepted")]
    assert ma.units_by_issue(rows) == {}


def test_issue_without_kn_rows_is_absent():
    rows = [_row(1, 150, None, "accepted"), _row(2, 151, "1/2", "accepted")]
    assert ma.units_by_issue(rows) == {151: {"done": 1, "total": 2}}


def test_non_int_issue_is_skipped_without_error():
    # the string "136" and the None issue add nothing; only the int 136 row counts
    rows = [
        _row(1, "136", "1/2", "accepted"),
        _row(2, None, "1/2", "accepted"),
        _row(3, 136, "1/3", "accepted"),
    ]
    assert ma.units_by_issue(rows) == {136: {"done": 1, "total": 3}}


def test_only_non_int_issues_gives_empty():
    rows = [_row(1, "136", "1/2", "accepted"), _row(2, None, "1/2", "accepted")]
    assert ma.units_by_issue(rows) == {}


def test_non_int_issue_does_not_hide_int_issue():
    rows = [_row(1, "136", "1/2", "accepted"), _row(2, 148, "1/2", "accepted"), _row(3, 148, "2/2", "redone")]
    assert ma.units_by_issue(rows) == {148: {"done": 2, "total": 2}}  # #160


def test_units_by_issue_empty_log():
    assert ma.units_by_issue([]) == {}


CUT_TABLE_4 = """Checkpoint 1 of 4: the cut of #148, agreed with the brain.

| unit | outcome | files it edits | its test | needs |
|---|---|---|---|---|
| 1 of 4 (start-time) | accepted | tools/agentops/monitor_agents.py | tests/test_issue148.py | - |
| 2 of 4 (end-time) | pending | tools/agentops/monitor_agents.py | tests/test_issue148.py | 1 |
| 3 of 4 (totals) | pending | tools/agentops/monitor_agents.py | tests/test_issue148.py | 2 |
| 4 of 4 (panel) | pending | tools/agentops/monitor_agents.py | tests/test_issue148.py | 3 |
"""

CUT_TABLE_3 = """Checkpoint 1 of 3: first cut.

| unit | outcome | files it edits | its test | needs |
|---|---|---|---|---|
| 1 of 3 (a) | accepted | tools/x.py | tests/t.py | - |
| 2 of 3 (b) | pending | tools/x.py | tests/t.py | 1 |
| 3 of 3 (c) | pending | tools/x.py | tests/t.py | 2 |
"""


def test_cut_table_total_realistic_comment():
    assert ma.cut_table_total([CUT_TABLE_4]) == 4


def test_cut_table_total_header_is_case_insensitive():
    body = CUT_TABLE_4.replace("| unit | outcome |", "| Unit | Outcome |")
    assert ma.cut_table_total([body]) == 4


def test_cut_table_total_newest_comment_wins():
    assert ma.cut_table_total([CUT_TABLE_3, CUT_TABLE_4]) == 4


def test_cut_table_total_newest_comment_with_table_wins_over_later_plain_comment():
    plain = "Checkpoint 2: unit 2 of 4 is done, no table this time."
    assert ma.cut_table_total([CUT_TABLE_4, plain]) == 4


def test_cut_table_total_older_table_used_when_newer_has_no_table():
    assert ma.cut_table_total([CUT_TABLE_3, "Checkpoint 2: merged, numbers below."]) == 3


def test_cut_table_total_no_table_is_none():
    assert ma.cut_table_total(["Checkpoint 1: done, next, numbers.", "Checkpoint 2: merged."]) is None


def test_cut_table_total_empty_list_is_none():
    assert ma.cut_table_total([]) is None


def test_cut_table_total_single_unit_table_is_none():
    body = "| unit | outcome |\n|---|---|\n| 1 of 1 (only) | accepted |\n"
    assert ma.cut_table_total([body]) is None


def test_cut_table_total_table_without_kn_cells_is_not_a_cut_table():
    body = "| unit | outcome |\n|---|---|\n| start | accepted |\n"
    assert ma.cut_table_total([body]) is None


def test_cut_table_total_reads_cells_without_a_parenthesised_name():
    rows = ["| unit | outcome |", "|---|---|", "| 1 of 3 | a |", "| 2 of 3 start-time | b |", "| 3 of 3 - plugin | c |"]
    assert ma.cut_table_total([chr(10).join(rows)]) == 3
