"""Cost buckets of the weekly loss table: what the closed tickets of a window cost at API price, and how
much of the spend sits on no closed ticket.

`cost_buckets` reads one projects folder of session transcripts through the token meter's own scan,
prices every turn of the window, and splits each orchestrator session's cost over the issues it held,
by the seconds it held each one (the event log's taking and ending pairs). It reads nothing else: the
caller passes the event list and the closed-ticket rows. Money is returned as plain numbers, never
formatted here.

Stdlib, plus the token meter in the home's scripts folder.
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS_DIR = _HERE.parents[2] / "scripts"
_PRICES_PATH = _HERE.parents[2] / "config" / "model_prices.json"


def _token_meter():
    """The token meter, imported on first use: the agents panel and the dispatcher import this module
    and must load without it (#1136)."""
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import token_meter

    return token_meter


from .losses_calc import held_spans, session_issue_weights  # noqa: E402


@dataclass(frozen=True)
class CostBuckets:
    """The cost buckets of one window. Money in USD at API price, unrounded.

    ``closed`` is the number of distinct closed tickets whose ``closedAt`` lies in the window.
    ``spent_usd`` is every priced turn of the window, attributed or not; ``attributed_usd`` is the part
    of it that sits on those closed tickets; ``unowned_usd`` is the rest. ``unowned_by_source`` splits
    ``unowned_usd`` into the keys of ``UNOWNED_SOURCES`` (they sum to ``unowned_usd``). ``mean_usd`` is
    ``spent_usd`` over ``closed``; the median, p90 and max are over the per-ticket attributed costs of all
    closed tickets (a ticket with no cost counts as 0).
    """

    closed: int
    spent_usd: float
    attributed_usd: float
    unowned_usd: float
    unowned_share: float
    mean_usd: float
    median_usd: float
    p90_usd: float
    max_usd: float
    unowned_by_source: dict[str, float]


#: the three sources of the unowned cost: a ticket not closed in the window, a brain chat turn, and a turn
#: that names no ticket at all
UNOWNED_SOURCES = ("ticket still open", "brain chat", "no ticket named")


def nearest_rank(sorted_values: list[float], percent: int) -> float:
    """The nearest-rank percentile of an ascending list: the value at rank ceil(percent / 100 * n),
    counted from 1. Integer arithmetic on the rank, so no float edge at a whole rank."""

    rank = (percent * len(sorted_values) + 99) // 100
    return sorted_values[rank - 1]


def _full_session_weights(
    weights: dict[str, dict[int, float]], session_ids: set[str]
) -> dict[str, dict[int, float]]:
    """Re-key each session weight map from the id the event log gives (the dispatch log writes the first
    8 characters of a session id) to the transcript's full session id. A key equal to a transcript's id
    is that id; otherwise the key must be a prefix of exactly one transcript's id. A key that names no
    transcript, or more than one, is dropped: it is never guessed."""

    out: dict[str, dict[int, float]] = {}
    for key, share in weights.items():
        if not key:
            continue
        matches = [key] if key in session_ids else [s for s in session_ids if s.startswith(key)]
        if len(matches) == 1:
            out[matches[0]] = share
    return out


def _closed_in_window(row: dict, since: datetime, until: datetime) -> bool:
    closed_at = _token_meter().parse_timestamp(row.get("closedAt"))
    return closed_at is not None and since <= closed_at <= until


def _with_session_weights(turn, session_issue_weights: dict[str, dict[int, float]] | None):
    """The turn as ``cost_per_closed_issue`` splits it: a split turn's shares are replaced by its session's
    time-held weights when the session has them, so the unowned sources add up to the same unowned cost."""

    if not turn.issue_shares or not session_issue_weights:
        return turn
    mapped = session_issue_weights.get(turn.session_id)
    if not mapped:
        return turn
    shares = _token_meter()._mapped_shares(turn.issue_shares, mapped)
    return turn if shares is None else replace(turn, issue_shares=shares)


def _unowned_by_source(
    turns,
    closed_numbers: set[int],
    session_issue_weights: dict[str, dict[int, float]] | None,
    prices: dict,
    since: datetime,
    until: datetime,
) -> dict[str, float]:
    """The priced cost of the window that no closed ticket of the window owns, by source. Each turn is
    grouped with the token meter's own group weights (``issue_group_weights``, after the session weights);
    a ``#N`` key of a closed ticket is owned, any other ``#N`` is "ticket still open", the brain key is
    "brain chat" and the unattributed key is "no ticket named"."""

    out = {source: 0.0 for source in UNOWNED_SOURCES}
    for turn in turns:
        if not _token_meter().in_window(turn, since, until):
            continue
        cost = _token_meter().turn_cost(turn, prices)
        if cost is None:
            continue
        groups = _token_meter().issue_group_weights(_with_session_weights(turn, session_issue_weights))
        for key, weight in groups.items():
            if key.startswith("#"):
                if int(key[1:]) in closed_numbers:
                    continue
                source = "ticket still open"
            elif key == "brain":
                source = "brain chat"
            else:
                source = "no ticket named"
            out[source] += cost * weight
    return out


def cost_buckets(
    projects_dir: Path,
    closed_issues: list[dict],
    events: list[dict],
    since: datetime,
    until: datetime,
    prices: dict | None = None,
) -> CostBuckets:
    """The cost buckets of the window ``since``..``until`` (both timezone-aware).

    ``closed_issues`` rows carry ``number`` and ``closedAt``; only those closed inside the window count as
    tickets. ``events`` is the outcome log's event list (``issue_taken`` / ``orch_end`` with ``ts``); an
    orchestrator session's cost splits over its held issues by the seconds held. ``prices`` is the price
    table (``token_meter.load_prices`` shape); ``None`` reads the home's ``config/model_prices.json``.
    """

    if prices is None:
        prices = _token_meter().load_prices(_PRICES_PATH)

    scan = _token_meter().scan_projects_dir(projects_dir, since=since, until=until)
    session_ids = {path.stem for path in scan.main_transcripts}
    weights = _full_session_weights(session_issue_weights(held_spans(events)), session_ids)

    tickets = [row for row in closed_issues if _closed_in_window(row, since, until)]
    numbers = sorted({row["number"] for row in tickets})
    costs = _token_meter().cost_per_closed_issue(
        scan.turns, tickets, prices, since=since, until=until, session_issue_weights=weights,
    )

    spent = 0.0
    for turn in scan.turns:
        if not _token_meter().in_window(turn, since, until):
            continue
        cost = _token_meter().turn_cost(turn, prices)
        if cost is not None:
            spent += cost
    attributed = sum(ic.cost_usd for ic in costs)
    unowned = spent - attributed
    by_source = _unowned_by_source(scan.turns, set(numbers), weights, prices, since, until)

    by_number = {ic.number: ic.cost_usd for ic in costs}
    per_ticket = sorted(by_number.get(n, 0.0) for n in numbers)
    closed = len(numbers)
    return CostBuckets(
        closed=closed,
        spent_usd=spent,
        attributed_usd=attributed,
        unowned_usd=unowned,
        unowned_share=unowned / spent if spent > 0 else 0.0,
        mean_usd=spent / closed if closed else 0.0,
        median_usd=statistics.median(per_ticket) if per_ticket else 0.0,
        p90_usd=nearest_rank(per_ticket, 90) if per_ticket else 0.0,
        max_usd=per_ticket[-1] if per_ticket else 0.0,
        unowned_by_source=by_source,
    )
