"""The closing report of the S-versus-C replay (issue #376 unit 6).

S is the layer's own record of a closed ticket; C is the same ticket re-run by one strong agent in a
sealed clone. One ticket record is a dict with ``number``, ``type`` ("fan-out" | "core" | "doc" |
"small"), ``blind_better`` ("S" | "C" | "tie"), ``s`` and ``c``. The keys read here:

- ``s``: ``attributed_usd``, ``full_usd``, ``per_model``, ``hours``, ``merge_attempts``, ``breaks``,
  ``send_backs``, ``owner_msgs``, ``acceptance_passed``, ``acceptance_total``, ``suite_ok``,
  ``blind_score``, ``defects``, ``defect_list``.
- ``c``: ``usd`` in place of ``full_usd``, ``per_model``, ``hours``, ``breaks``, ``send_backs``,
  ``owner_msgs``, ``acceptance_passed``, ``acceptance_total``, ``n_a_rows``, ``suite_ok``, ``blind_score``,
  ``defects``, ``defect_list``, ``stopped``.

``defect_list`` holds "file:line text" strings; ``defects`` is its count (the data keeps them equal).

Everything here is a pure function except ``main``, the only I/O (it reads the records file and prints).

ORCH ruling (send-back #376), the interpretation of ``type_verdict`` (the rule is #375's):

- Quality is compared per ticket, not summed. On each ticket there are four quality dimensions:
  acceptance share, blind result (``blind_better``), defects and breaks. S loses a dimension on a ticket
  when S is worse than C there; C loses it when C is worse than S there. S holds needs S to lose on no
  ticket of the type; C wins needs C to lose on no ticket of the type (the symmetric rule).
- Cost and hours are summed over the type's tickets, each side on its own, and compared as #375 states
  (S at most 1.2 x C; C below 0.8 x S).
- A stopped C enters at its stop cost: its ``usd`` and ``hours`` are what it spent up to the stop.
- Acceptance share on a ticket is passed / (total - n_a_rows), with the n/a rows out of the denominator
  on both sides. A ticket with no scored row has share 0.0 on that side.
- Whether ``defects`` equals the length of ``defect_list`` is a fact of the data, checked by a test
  of the data, not by this module.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TYPES = ("fan-out", "core", "doc", "small")
QUALITY_DIMENSIONS = ("acceptance", "blind", "defects", "breaks")
S_HOLDS = "S holds"
C_WINS = "C wins"
MIXED = "mixed"
S_MAX_RATIO = 1.2  # S holds: S cost and S hours <= 1.2 x C
C_MAX_RATIO = 0.8  # C wins: C cost and C hours < 0.8 x S
SPEND_OVER_USD = 100
SPEND_OVER_CARD = 34


def factor(week_total: float, week_brain_bucket: float, week_attributed: float) -> float:
    """S's weekly factor: the week's whole spend less the brain chat bucket, over the week's attributed spend."""
    return (week_total - week_brain_bucket) / week_attributed


def s_full(attributed_usd: float, week_total: float, week_brain_bucket: float, week_attributed: float) -> float:
    """S's full cost of one ticket: its attributed cost x the factor of its closing week."""
    return attributed_usd * factor(week_total, week_brain_bucket, week_attributed)


def spread_pct(s: float, c: float) -> float | None:
    """How much C costs more than S in %: positive when C is the larger; None when S is zero."""
    if s == 0:
        return None
    return (c - s) / s * 100


def _sum(records: list[dict], side: str, key: str) -> float:
    return sum(record[side][key] for record in records)


def _ticket_share(side: dict) -> float:
    scored = side["acceptance_total"] - side.get("n_a_rows", 0)
    return side["acceptance_passed"] / scored if scored else 0.0


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _gaps(record: dict) -> dict[str, int]:
    """Per quality dimension on one ticket: +1 where S is worse than C, -1 where C is worse, 0 level."""
    s, c = record["s"], record["c"]
    return {
        "acceptance": _sign(_ticket_share(c) - _ticket_share(s)),
        "blind": {"C": 1, "S": -1}.get(record["blind_better"], 0),
        "defects": _sign(s["defects"] - c["defects"]),
        "breaks": _sign(s["breaks"] - c["breaks"]),
    }


def _losses(gaps_by_ticket: list[tuple[int, dict[str, int]]], sign: int) -> list[str]:
    """The ``#number dimension`` pairs where the side with this sign is worse (1 for S, -1 for C)."""
    return [
        f"#{number} {dim}"
        for number, gaps in gaps_by_ticket
        for dim in QUALITY_DIMENSIONS
        if gaps[dim] == sign
    ]


def type_verdict(records: list[dict]) -> dict:
    """#375's rule on one type's tickets: ``{"verdict": "S holds" | "C wins" | "mixed", "reasons": [...]}``.

    S holds when S full cost (summed) <= 1.2 x C cost, S hours (summed) <= 1.2 x C hours, and on no ticket
    is S worse than C on acceptance, blind result, defects or breaks. C wins when C cost < 0.8 x S full
    cost, C hours < 0.8 x S hours, and on no ticket is C worse than S on those four. S holds is checked
    first. Raises ValueError on an empty list: a type with no tickets has no verdict.
    """
    if not records:
        raise ValueError("type_verdict needs at least one ticket of the type")
    s_cost = _sum(records, "s", "full_usd")
    c_cost = _sum(records, "c", "usd")
    s_hours = _sum(records, "s", "hours")
    c_hours = _sum(records, "c", "hours")
    gaps_by_ticket = [(record["number"], _gaps(record)) for record in records]
    s_losses = _losses(gaps_by_ticket, 1)
    c_losses = _losses(gaps_by_ticket, -1)

    s_holds = (
        s_cost <= S_MAX_RATIO * c_cost
        and s_hours <= S_MAX_RATIO * c_hours
        and not s_losses
    )
    c_wins = (
        c_cost < C_MAX_RATIO * s_cost
        and c_hours < C_MAX_RATIO * s_hours
        and not c_losses
    )
    if s_holds:
        verdict = S_HOLDS
    elif c_wins:
        verdict = C_WINS
    else:
        verdict = MIXED
    reasons = [
        f"cost: S full {s_cost:.2f} USD, C {c_cost:.2f} USD "
        f"(S at most {S_MAX_RATIO:.1f}x C; C wins below {C_MAX_RATIO * 100:.0f} % of S)",
        f"hours: S {s_hours:.2f}, C {c_hours:.2f} (same two limits)",
        f"S worse per ticket: {', '.join(s_losses) or 'none'}",
        f"C worse per ticket: {', '.join(c_losses) or 'none'}",
    ]
    return {"verdict": verdict, "reasons": reasons}


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _spread_cell(s: float | None, c: float | None) -> str:
    if s is None or c is None:
        return "—"
    spread = spread_pct(s, c)
    return "n/a" if spread is None else f"{spread:+.1f} %"


def _row(label: str, s_cell: str, c_cell: str, spread: str = "—") -> str:
    return f"| {label} | {s_cell} | {c_cell} | {spread} |"


def _blind_cell(better: str, side: str) -> str:
    if better == "tie":
        return "tie"
    return "won" if better == side else "lost"


def _named_cell(named: list[str]) -> str:
    if not named:
        return "none"
    return "; ".join(item.replace("|", "\\|") for item in named)


def ticket_table(record: dict, usd_per_pct: float) -> str:
    """One ticket as a markdown table; C's cost is also shown as % of the week (cost / ``usd_per_pct``)."""
    s, c = record["s"], record["c"]
    better = record["blind_better"]
    rows = [
        _row("cost, S attributed $", _fmt(s["attributed_usd"]), "—"),
        _row("cost, S full / C $", _fmt(s["full_usd"]), _fmt(c["usd"]), _spread_cell(s["full_usd"], c["usd"])),
        _row("cost, C % of the week", "—", f"{c['usd'] / usd_per_pct:.1f} %"),
    ]
    for model in sorted(set(s["per_model"]) | set(c["per_model"])):
        s_val = s["per_model"].get(model)
        c_val = c["per_model"].get(model)
        rows.append(_row(f"cost, {model} $", _fmt(s_val), _fmt(c_val), _spread_cell(s_val, c_val)))
    rows += [
        _row("hours", _fmt(s["hours"]), _fmt(c["hours"]), _spread_cell(s["hours"], c["hours"])),
        _row("merge attempts (S)", str(s["merge_attempts"]), "—"),
        _row(
            "acceptance passed (of total, n/a)",
            f"{s['acceptance_passed']} of {s['acceptance_total']}",
            f"{c['acceptance_passed']} of {c['acceptance_total']} (n/a {c['n_a_rows']})",
        ),
        _row("merge suite", "green" if s["suite_ok"] else "red", "green" if c["suite_ok"] else "red"),
        _row("blind verdict", _blind_cell(better, "S"), _blind_cell(better, "C")),
        _row("blind score (1-5)", f"{s['blind_score']:.1f}", f"{c['blind_score']:.1f}"),
        _row("defects", str(s["defects"]), str(c["defects"])),
        _row("named defects", _named_cell(s["defect_list"]), _named_cell(c["defect_list"])),
        _row("breaks", str(s["breaks"]), str(c["breaks"])),
        _row("C stopped at cap", "—", "yes" if c["stopped"] else "no"),
        _row("rework (send-backs)", str(s["send_backs"]), str(c["send_backs"])),
        _row("owner load (owner msgs)", str(s["owner_msgs"]), str(c["owner_msgs"])),
    ]
    header = ["| row | S | C | spread % |", "| --- | --- | --- | --- |"]
    return "\n".join([f"### #{record['number']} ({record['type']})", "", *header, *rows])


def run_spend(
    c_usd: float,
    judge_usd: float,
    orch_usd: float,
    usd_per_pct: float,
    card_before: float,
    card_after: float,
) -> str:
    """The run's spend as one line; OVER BUDGET when the total is above $100 or the card reads 34 % or more."""
    total = c_usd + judge_usd + orch_usd
    pct = total / usd_per_pct
    over = total > SPEND_OVER_USD or card_after >= SPEND_OVER_CARD
    line = (
        f"spend ${total:.2f} = {pct:.1f} % of the week (C {c_usd:.2f}, judge {judge_usd:.2f}, "
        f"ORCH {orch_usd:.2f} USD); card {card_before:g} % -> {card_after:g} %"
    )
    return line + ("  OVER BUDGET" if over else "")


def main(argv: list[str] | None = None) -> int:
    """Print every ticket's table, the verdict line of each type, and the spend line."""
    parser = argparse.ArgumentParser(prog="python -m tools.replays.c_report", description=main.__doc__)
    parser.add_argument("records", type=Path, help="JSON list of ticket records")
    for flag in ("--c-usd", "--judge-usd", "--orch-usd", "--usd-per-pct", "--card-before", "--card-after"):
        parser.add_argument(flag, type=float, required=True)
    args = parser.parse_args(argv)
    records = json.loads(args.records.read_text(encoding="utf-8"))
    for record in records:
        print(ticket_table(record, args.usd_per_pct))
        print()
    for kind in TYPES:
        group = [record for record in records if record["type"] == kind]
        if not group:
            print(f"{kind}: no tickets")
            continue
        result = type_verdict(group)
        print(f"{kind}: {result['verdict']} - " + "; ".join(result["reasons"]))
    print(
        run_spend(
            args.c_usd, args.judge_usd, args.orch_usd, args.usd_per_pct, args.card_before, args.card_after
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
