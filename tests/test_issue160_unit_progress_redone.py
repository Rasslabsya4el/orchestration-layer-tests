"""#160: a unit is done when its latest row is accepted or redone; an older cut's rows count for their k.

    python -m pytest -q tests/test_issue160_unit_progress_redone.py
"""
from tools.agentops import monitor_agents as ma


def _rows(issue, *spec):
    return [{"ts": ts, "issue": issue, "route": "worker", "class": cls, "why": "", "session": "s", "unit": unit}
            for ts, (unit, cls) in enumerate(spec, start=1)]


def test_row1_issue_154_accepted_then_redone_is_3_of_3():
    rows = _rows(154, ("1/3", "accepted"), ("2/3", "redone"), ("3/3", "redone"))
    assert ma.unit_progress(rows, 154) == {"done": 3, "total": 3}


def test_row2_issue_149_takeover_numbering_is_6_of_6():
    rows = _rows(149, ("1/4", "redone"), ("2/4", "accepted"), ("3/4", "accepted"), ("4/4", "accepted"),
                 ("5/6", "accepted"), ("6/6", "accepted"))
    assert ma.unit_progress(rows, 149) == {"done": 6, "total": 6}


def test_row3_wrong_fact_and_abandoned_are_not_done():
    rows = _rows(7, ("1/3", "accepted"), ("2/3", "wrong-fact"), ("3/3", "abandoned"))
    assert ma.unit_progress(rows, 7) == {"done": 1, "total": 3}


def test_row4_recut_from_4_to_2_ignores_k_above_total():
    rows = _rows(8, ("1/4", "accepted"), ("2/4", "accepted"), ("3/4", "accepted"), ("4/4", "accepted"),
                 ("1/2", "redone"))
    assert ma.unit_progress(rows, 8) == {"done": 2, "total": 2}


def test_newer_row_for_same_k_wins_over_older_cut():
    rows = _rows(9, ("1/4", "accepted"), ("2/4", "accepted"), ("1/2", "wrong-fact"))
    assert ma.unit_progress(rows, 9) == {"done": 1, "total": 2}
