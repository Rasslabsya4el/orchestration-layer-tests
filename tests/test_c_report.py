"""Issue #376 unit 6: the closing report of the S-versus-C replay (tools/replays/c_report.py).

factor and s_full give S's full cost from its attributed cost; spread_pct is the spread in %;
type_verdict applies #375's rule (per-ticket quality, ORCH ruling of send-back #376) to one type's
tickets; ticket_table draws one ticket; run_spend writes the spend line; main is the only I/O.
Expected values are worked by hand, never recomputed with the module's own formula.
"""

import json

import pytest

from tools.replays.c_report import factor, main, run_spend, s_full, spread_pct, ticket_table, type_verdict

MODEL = "claude-opus-5-5"
USD_PER_PCT = 16.5


def _ticket(
    *,
    number=1,
    kind="core",
    s_cost=10.0,
    s_hours=10.0,
    c_usd=10.0,
    c_hours=10.0,
    blind_better="tie",
    s_breaks=0,
    c_breaks=0,
    s_defects=0,
    c_defects=0,
    s_defect_list=None,
    c_defect_list=None,
    s_passed=5,
    s_total=5,
    c_passed=5,
    c_total=5,
    c_na=0,
    s_merge_attempts=1,
    c_stopped=False,
):
    """One ticket record in the shape the module reads; defect lists default to one entry per count."""
    if s_defect_list is None:
        s_defect_list = [f"s{number}.py:{i + 1} defect" for i in range(s_defects)]
    if c_defect_list is None:
        c_defect_list = [f"c{number}.py:{i + 1} defect" for i in range(c_defects)]
    s = {
        "attributed_usd": s_cost,
        "full_usd": s_cost,
        "per_model": {MODEL: s_cost},
        "hours": s_hours,
        "merge_attempts": s_merge_attempts,
        "breaks": s_breaks,
        "send_backs": 0,
        "owner_msgs": 0,
        "acceptance_passed": s_passed,
        "acceptance_total": s_total,
        "suite_ok": True,
        "blind_score": 4.0,
        "defects": s_defects,
        "defect_list": s_defect_list,
    }
    c = {
        "per_model": {MODEL: c_usd},
        "usd": c_usd,
        "hours": c_hours,
        "breaks": c_breaks,
        "send_backs": 0,
        "owner_msgs": 0,
        "acceptance_passed": c_passed,
        "acceptance_total": c_total,
        "suite_ok": True,
        "blind_score": 3.0,
        "defects": c_defects,
        "defect_list": c_defect_list,
        "stopped": c_stopped,
        "n_a_rows": c_na,
    }
    return {"number": number, "type": kind, "s": s, "c": c, "blind_better": blind_better}


# --- factor and full cost -------------------------------------------------------------------

def test_factor_and_full_worked_example():
    # week spend 800, brain bucket 80, week attributed 400 -> factor (800 - 80) / 400 = 1.8
    assert factor(800.0, 80.0, 400.0) == pytest.approx(1.8)
    # attributed 10.0 x 1.8 = 18.0
    assert s_full(10.0, 800.0, 80.0, 400.0) == pytest.approx(18.0)


# --- spread ---------------------------------------------------------------------------------

def test_spread_is_positive_when_c_costs_more():
    assert spread_pct(10.0, 12.0) == pytest.approx(20.0)


def test_spread_is_negative_when_c_costs_less():
    assert spread_pct(10.0, 8.0) == pytest.approx(-20.0)


def test_spread_is_none_when_s_is_zero():
    assert spread_pct(0.0, 5.0) is None
    assert spread_pct(0.0, 0.0) is None


# --- type verdict: S holds -------------------------------------------------------------------

def test_s_holds_at_exactly_1_2_times_c_cost_and_hours():
    # 6.0 <= 1.2 x 5.0 on cost and on hours: exactly 1.2 x is still S holds
    records = [_ticket(s_cost=6.0, s_hours=6.0, c_usd=5.0, c_hours=5.0)]
    result = type_verdict(records)
    assert result["verdict"] == "S holds"
    assert result["reasons"]
    assert any("cost" in reason for reason in result["reasons"])
    assert any("1.2x" in reason for reason in result["reasons"])


def test_s_does_not_hold_just_above_1_2_times_cost():
    # 6.01 > 1.2 x 5.0; C is not 20 % cheaper either (5.0 is not < 0.8 x 6.01), so mixed
    records = [_ticket(s_cost=6.01, s_hours=6.0, c_usd=5.0, c_hours=5.0)]
    assert type_verdict(records)["verdict"] == "mixed"


def test_s_holds_with_zero_hours_on_both_sides():
    records = [_ticket(s_cost=5.0, s_hours=0.0, c_usd=5.0, c_hours=0.0)]
    assert type_verdict(records)["verdict"] == "S holds"


def test_s_holds_sums_cost_over_the_type():
    # one ticket over, one under: the cost sums are 12.0 vs 10.0 (= 1.2 x) and the hours 10.0 vs 10.0
    records = [
        _ticket(number=1, s_cost=7.0, s_hours=5.0, c_usd=5.0, c_hours=5.0),
        _ticket(number=2, s_cost=5.0, s_hours=5.0, c_usd=5.0, c_hours=5.0),
    ]
    assert type_verdict(records)["verdict"] == "S holds"


def test_s_loses_on_acceptance_so_it_does_not_hold():
    # S passes 3 of 5, C passes 5 of 5: S loses acceptance; costs are equal so C does not win either
    records = [_ticket(s_passed=3, s_total=5, c_passed=5, c_total=5)]
    assert type_verdict(records)["verdict"] == "mixed"


def test_c_na_rows_are_not_counted_as_failed():
    # C: 4 of 5 passed, one row n/a for C (denominator 4, share 1.0); S: 4 of 4 (share 1.0): no loss
    records = [_ticket(s_passed=4, s_total=4, c_passed=4, c_total=5, c_na=1)]
    assert type_verdict(records)["verdict"] == "S holds"


def test_s_loses_on_quality_when_c_is_better_on_a_ticket():
    records = [_ticket(blind_better="C")]
    assert type_verdict(records)["verdict"] == "mixed"


def test_s_loses_on_defects_when_it_has_more():
    records = [_ticket(s_defects=1, c_defects=0)]
    assert type_verdict(records)["verdict"] == "mixed"


def test_s_loses_on_breaks_when_it_has_more():
    records = [_ticket(s_breaks=1, c_breaks=0)]
    assert type_verdict(records)["verdict"] == "mixed"


@pytest.mark.parametrize(
    "dim, first, second",
    [
        ("acceptance", {"s_passed": 5, "c_passed": 3}, {"s_passed": 3, "c_passed": 5}),
        ("defects", {"s_defects": 0, "c_defects": 1}, {"s_defects": 1, "c_defects": 0}),
        ("breaks", {"s_breaks": 0, "c_breaks": 1}, {"s_breaks": 1, "c_breaks": 0}),
        ("blind", {"blind_better": "S"}, {"blind_better": "C"}),
    ],
)
def test_s_loss_on_one_ticket_is_not_hidden_by_a_sum(dim, first, second):
    # the sums are equal (acceptance 8/10 vs 8/10, defects 1 vs 1, one blind win each) and the costs
    # are equal, so only the per-ticket rule sees S worse on ticket #2
    records = [_ticket(number=1, **first), _ticket(number=2, **second)]
    result = type_verdict(records)
    assert result["verdict"] == "mixed"
    assert f"#2 {dim}" in " ".join(result["reasons"])


# --- type verdict: C wins --------------------------------------------------------------------

def test_c_wins_when_it_beats_s_by_more_than_20_percent_on_cost_and_hours():
    # 10.0 > 1.2 x 7.0, so S does not hold; 7.0 < 0.8 x 10.0 and 7.0 < 0.8 x 10.0, quality tie: C wins
    records = [_ticket(s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0)]
    assert type_verdict(records)["verdict"] == "C wins"


def test_exactly_20_percent_cheaper_is_not_c_wins():
    # C 8.0 is not < 0.8 x 10.0 = 8.0; S does not hold either (10.0 > 1.2 x 8.0 = 9.6): mixed
    records = [_ticket(s_cost=10.0, s_hours=10.0, c_usd=8.0, c_hours=7.0)]
    assert type_verdict(records)["verdict"] == "mixed"


def test_c_wins_blocked_when_s_is_better_on_quality():
    records = [_ticket(s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0, blind_better="S")]
    assert type_verdict(records)["verdict"] == "mixed"


def test_c_wins_with_zero_s_hours_does_not_divide_by_zero():
    # 0 < 0.8 x 0 is false, so the hours test fails cleanly: mixed, not an error
    records = [_ticket(s_cost=10.0, s_hours=0.0, c_usd=7.0, c_hours=0.0)]
    assert type_verdict(records)["verdict"] == "mixed"


@pytest.mark.parametrize(
    "dim, first, second",
    [
        ("defects", {"s_defects": 1, "c_defects": 0}, {"s_defects": 0, "c_defects": 1}),
        ("acceptance", {"s_passed": 3, "c_passed": 5}, {"s_passed": 5, "c_passed": 3}),
    ],
)
def test_c_loss_on_one_ticket_blocks_c_wins_even_when_sums_favour_c(dim, first, second):
    # the sums favour C (cost 20 vs 14, hours 20 vs 14) but C is worse on ticket #2: not C wins
    records = [
        _ticket(number=1, s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0, **first),
        _ticket(number=2, s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0, **second),
    ]
    result = type_verdict(records)
    assert result["verdict"] == "mixed"
    assert f"#2 {dim}" in " ".join(result["reasons"])


def test_c_wins_when_no_ticket_has_a_c_loss():
    # the same cost and hours as the blocked case, with equal quality on both tickets: C wins
    records = [
        _ticket(number=1, s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0),
        _ticket(number=2, s_cost=10.0, s_hours=10.0, c_usd=7.0, c_hours=7.0),
    ]
    assert type_verdict(records)["verdict"] == "C wins"


def test_type_verdict_on_no_tickets_raises():
    with pytest.raises(ValueError):
        type_verdict([])


# --- ticket table ----------------------------------------------------------------------------

def test_ticket_table_has_the_spread_column_and_rows():
    record = _ticket(number=349, kind="fan-out", s_cost=10.0, c_usd=12.0, s_hours=10.0, c_hours=8.0)
    text = ticket_table(record, USD_PER_PCT)
    assert "349" in text
    assert "| S | C | spread % |" in text
    cost_line = next(line for line in text.splitlines() if "S full" in line)
    assert "10.00" in cost_line and "12.00" in cost_line and "+20.0 %" in cost_line
    hours_line = next(line for line in text.splitlines() if line.startswith("| hours"))
    assert "-20.0 %" in hours_line
    assert MODEL in text


def test_ticket_table_shows_na_spread_when_s_is_zero():
    record = _ticket(s_hours=0.0, c_hours=3.0)
    hours_line = next(line for line in ticket_table(record, USD_PER_PCT).splitlines() if line.startswith("| hours"))
    assert "n/a" in hours_line


def test_ticket_table_shows_c_cost_as_percent_of_the_week():
    # C 33.0 USD / 16.5 USD per % = 2.0 %
    record = _ticket(c_usd=33.0)
    line = next(line for line in ticket_table(record, USD_PER_PCT).splitlines() if line.startswith("| cost, C %"))
    assert line == "| cost, C % of the week | — | 2.0 % | — |"


def test_ticket_table_names_the_defects_and_escapes_pipes():
    record = _ticket(
        s_defects=2,
        s_defect_list=["src/a.py:12 bad name", "src/b.py:3 x | y"],
        c_defects=1,
        c_defect_list=["tests/t.py:5 flaky"],
    )
    text = ticket_table(record, USD_PER_PCT)
    line = next(line for line in text.splitlines() if line.startswith("| named defects"))
    assert "src/a.py:12 bad name" in line
    assert "src/b.py:3 x \\| y" in line
    assert "tests/t.py:5 flaky" in line


def test_ticket_table_says_none_when_no_defect_is_named():
    line = next(line for line in ticket_table(_ticket(), USD_PER_PCT).splitlines() if line.startswith("| named defects"))
    assert line == "| named defects | none | none | — |"


@pytest.mark.parametrize("stopped, answer", [(True, "yes"), (False, "no")])
def test_ticket_table_shows_c_stopped_at_cap(stopped, answer):
    line = next(
        line
        for line in ticket_table(_ticket(c_stopped=stopped), USD_PER_PCT).splitlines()
        if line.startswith("| C stopped at cap")
    )
    assert line == f"| C stopped at cap | — | {answer} | — |"


def test_ticket_table_shows_s_merge_attempts():
    line = next(
        line
        for line in ticket_table(_ticket(s_merge_attempts=3), USD_PER_PCT).splitlines()
        if line.startswith("| merge attempts (S)")
    )
    assert line == "| merge attempts (S) | 3 | — | — |"


# --- spend line ------------------------------------------------------------------------------

def test_spend_line_is_not_flagged_at_exactly_100_usd_and_card_below_34():
    line = run_spend(60.0, 30.0, 10.0, 16.5, 26.0, 33.9)
    assert "OVER BUDGET" not in line
    assert "100.00" in line


def test_spend_line_flags_over_budget_above_100_usd():
    line = run_spend(60.0, 30.0, 10.01, 16.5, 26.0, 28.0)
    assert "OVER BUDGET" in line


def test_spend_line_flags_over_budget_when_card_reaches_34():
    line = run_spend(10.0, 1.0, 1.0, 16.5, 26.0, 34.0)
    assert "OVER BUDGET" in line


def test_spend_line_gives_percent_of_week_and_card():
    # 33.0 USD / 16.5 USD per % = 2.0 %
    line = run_spend(20.0, 8.0, 5.0, 16.5, 26.0, 28.0)
    assert "2.0 %" in line
    assert "26 %" in line and "28 %" in line


# --- main ------------------------------------------------------------------------------------

def test_main_prints_one_table_per_ticket_then_verdicts_then_spend(tmp_path, capsys):
    records = [
        _ticket(number=349, kind="fan-out"),
        _ticket(number=219, kind="fan-out"),
        _ticket(number=135, kind="core"),
        _ticket(number=218, kind="doc"),
        _ticket(number=127, kind="small"),
        _ticket(number=238, kind="small"),
    ]
    path = tmp_path / "records.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    code = main([
        str(path),
        "--c-usd", "60.0", "--judge-usd", "30.0", "--orch-usd", "5.0",
        "--usd-per-pct", "16.5", "--card-before", "26", "--card-after", "28",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("| hours") == 6
    for kind in ("fan-out", "core", "doc", "small"):
        assert any(line.startswith(f"{kind}: ") for line in out.splitlines())
    assert any(line.startswith("spend ") for line in out.splitlines())
