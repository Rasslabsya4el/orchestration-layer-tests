"""Unit 4a of issue 1121: `cost_buckets` in tools/agentops/losses_cost.py.

Seam: the public `cost_buckets` function, called with a synthetic projects dir (tmp_path), a literal
event list and a literal closed-ticket list. Every expected value is written out by hand from the
fixture below (never recomputed with the code's own formula).

Fixture, window 2026-10-09..2026-10-16 (UTC), prices: one million input tokens = $2.00, one million
output tokens = $10.00:
  S1  aaaa1111-...  title "orch #9101 #9102" (a two-issue ORCH session), two turns, $2.00 + $5.00 = $7.00
  S2  bbbb2222-...  title "orch #9103" (a single-issue session), one turn, $1.00
  U   cccc3333-...  no title, no issue named: one turn, $1.00, belongs to no ticket
Events: S1 held #9101 for 1 h and #9102 for 3 h; S2 held #9103 for 30 min. The events name the
sessions by their 8-character prefix, as the real dispatch log does.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import token_meter  # noqa: E402
from tools.agentops.losses_cost import CostBuckets, cost_buckets  # noqa: E402

MODULE = ROOT / "tools" / "agentops" / "losses_cost.py"

SINCE = datetime(2026, 10, 9, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 16, tzinfo=timezone.utc)

S1 = "aaaa1111-0000-4000-8000-000000000001"
S2 = "bbbb2222-0000-4000-8000-000000000002"
U = "cccc3333-0000-4000-8000-000000000003"

A, B, C = 9101, 9102, 9103

PRICES = {
    "claude-sonnet-5": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.2, "output": 10.0},
}

ZERO = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}

# (session, title or None, [(message id, timestamp, usage)])
TRANSCRIPTS = [
    (S1, "orch #9101 #9102", [
        ("m1a", "2026-10-10T10:00:00Z", {**ZERO, "input_tokens": 1_000_000}),   # $2.00
        ("m1b", "2026-10-10T10:30:00Z", {**ZERO, "output_tokens": 500_000}),    # $5.00
    ]),
    (S2, "orch #9103", [
        ("m2a", "2026-10-11T09:10:00Z", {**ZERO, "input_tokens": 500_000}),     # $1.00
    ]),
    (U, None, [
        ("m3a", "2026-10-12T08:00:00Z", {**ZERO, "output_tokens": 100_000}),    # $1.00
    ]),
]

# a single-issue session names its ticket in its own first message (a title names one issue only as a
# hint, so the message is what attributes the turns); the other sessions open with a plain line
FIRST_MESSAGE = {S2: "take #9103 and finish it"}

# the dispatch log names the sessions by their first 8 characters
EVENTS = [
    {"ts": "2026-10-10T10:00:00+00:00", "kind": "issue_taken", "issue": A, "how": "claimed"},
    {"ts": "2026-10-10T11:00:00+00:00", "kind": "orch_end", "issue": A, "session": "aaaa1111",
     "outcome": "closed", "why": "done"},
    {"ts": "2026-10-10T11:00:00+00:00", "kind": "issue_taken", "issue": B, "how": "claimed"},
    {"ts": "2026-10-10T14:00:00+00:00", "kind": "orch_end", "issue": B, "session": "aaaa1111",
     "outcome": "closed", "why": "done"},
    {"ts": "2026-10-11T09:00:00+00:00", "kind": "issue_taken", "issue": C, "how": "claimed"},
    {"ts": "2026-10-11T09:30:00+00:00", "kind": "orch_end", "issue": C, "session": "bbbb2222",
     "outcome": "closed", "why": "done"},
]


def _closed(*numbers: int) -> list[dict]:
    return [{"number": n, "closedAt": "2026-10-13T00:00:00Z"} for n in numbers]


def _write_projects(projects_dir: Path) -> None:
    projects_dir.mkdir(parents=True, exist_ok=True)
    for session, title, turns in TRANSCRIPTS:
        lines: list[dict] = []
        if title is not None:
            lines.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        lines.append({"type": "user",
                      "message": {"role": "user", "content": FIRST_MESSAGE.get(session, "continue the batch")},
                      "timestamp": "2026-10-10T09:55:00Z"})
        for message_id, stamp, usage in turns:
            lines.append({"type": "assistant",
                          "message": {"id": message_id, "model": "claude-sonnet-5", "usage": usage},
                          "timestamp": stamp})
        (projects_dir / f"{session}.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


@pytest.fixture()
def projects_dir(tmp_path: Path) -> Path:
    path = tmp_path / "projects"
    _write_projects(path)
    return path


def _run(projects_dir: Path, closed: list[dict], events: list[dict] = EVENTS) -> CostBuckets:
    return cost_buckets(projects_dir, closed, events, SINCE, UNTIL, PRICES)


def test_buckets_of_the_fixture_to_the_cent(projects_dir: Path):
    buckets = _run(projects_dir, _closed(A, B, C))
    assert buckets.closed == 3
    assert round(buckets.spent_usd, 2) == 9.00
    assert round(buckets.attributed_usd, 2) == 8.00
    assert round(buckets.unowned_usd, 2) == 1.00
    assert buckets.unowned_share == pytest.approx(1 / 9)


def test_time_held_split_gives_25_75_for_the_two_issue_session(projects_dir: Path):
    # S1 held #9101 for 1 h and #9102 for 3 h: its $7.00 splits 1.75 / 5.25 (25 % / 75 %).
    # One ticket at a time: closed=[A] leaves A's share of S1's cost as its attributed cost.
    cost_a = _run(projects_dir, _closed(A)).attributed_usd
    cost_b = _run(projects_dir, _closed(B)).attributed_usd
    assert round(cost_a, 2) == 1.75
    assert round(cost_b, 2) == 5.25
    assert round(cost_a / (cost_a + cost_b), 2) == 0.25


def test_full_session_id_in_events_maps_the_same_way(projects_dir: Path):
    full = [dict(event, session=S1) if event.get("session") == "aaaa1111" else event for event in EVENTS]
    assert round(_run(projects_dir, _closed(A), events=full).attributed_usd, 2) == 1.75
    assert round(_run(projects_dir, _closed(B), events=full).attributed_usd, 2) == 5.25


def test_unknown_session_prefix_is_unused_and_keeps_the_equal_split(projects_dir: Path):
    # no transcript starts with zzzz9999: S1's weights are not used, so #9101 and #9102 split 3.50 each
    events = [dict(e, session="zzzz9999") if e.get("session") == "aaaa1111" else e for e in EVENTS]
    assert round(_run(projects_dir, _closed(A), events=events).attributed_usd, 2) == 3.50
    assert round(_run(projects_dir, _closed(B), events=events).attributed_usd, 2) == 3.50
    buckets = _run(projects_dir, _closed(A, B, C), events=events)
    assert round(buckets.attributed_usd, 2) == 8.00
    assert round(buckets.median_usd, 2) == 3.50


def test_per_ticket_mean_median_p90_max_of_the_fixture(projects_dir: Path):
    # per ticket (attributed): #9101 1.75, #9102 5.25, #9103 1.00
    # mean = all spend over the closed tickets: 9.00 / 3
    buckets = _run(projects_dir, _closed(A, B, C))
    assert round(buckets.mean_usd, 2) == 3.00
    # median of 1.00, 1.75, 5.25 is the middle one
    assert round(buckets.median_usd, 2) == 1.75
    # nearest rank: ceil(0.9 * 3) = 3, the third of three sorted values
    assert round(buckets.p90_usd, 2) == 5.25
    assert round(buckets.max_usd, 2) == 5.25


def test_a_closed_ticket_with_no_cost_counts_as_zero(projects_dir: Path):
    # #9104 is closed in the window but no turn names it: it adds a zero to the per-ticket values
    buckets = _run(projects_dir, _closed(A, B, C, 9104))
    assert buckets.closed == 4
    assert round(buckets.mean_usd, 2) == 2.25          # 9.00 / 4
    # median of 0, 1.00, 1.75, 5.25 is (1.00 + 1.75) / 2
    assert buckets.median_usd == pytest.approx(1.375)
    assert round(buckets.p90_usd, 2) == 5.25           # ceil(0.9 * 4) = 4th of 0, 1.00, 1.75, 5.25
    assert round(buckets.attributed_usd, 2) == 8.00


def test_no_closed_tickets_gives_zeros(projects_dir: Path):
    buckets = _run(projects_dir, [])
    assert buckets.closed == 0
    assert round(buckets.spent_usd, 2) == 9.00
    assert buckets.attributed_usd == 0.0
    assert buckets.unowned_usd == pytest.approx(9.00)
    assert buckets.unowned_share == 1.0
    assert buckets.mean_usd == 0.0
    assert buckets.median_usd == 0.0
    assert buckets.p90_usd == 0.0
    assert buckets.max_usd == 0.0


def test_result_is_a_frozen_dataclass(projects_dir: Path):
    buckets = _run(projects_dir, _closed(A, B, C))
    with pytest.raises(FrozenInstanceError):
        buckets.closed = 99  # type: ignore[misc]


# ------------------------------------------------ unowned_by_source (three sources)

P1 = "eeee0001-0000-4000-8000-000000000011"   # ORCH session on #9101, which closes in the window
P2 = "ffff0002-0000-4000-8000-000000000012"   # ORCH session on #9301, a ticket still open
P3 = "abcd0003-0000-4000-8000-000000000013"   # brain chat turn, names no ticket

# Fixture for the three unowned sources, window SINCE..UNTIL, same prices:
#   P1  title "orch #9101", held #9101 (closed in the window): $2.00 (1 M input)   -> owned
#   P2  title "orch #9301", held #9301 (never closed here):    $3.00 (1.5 M input) -> "ticket still open"
#   P3  brain chat title, no ticket:                           $4.00 (0.4 M output) -> "brain chat"
#   U   no title, no ticket:                                   $1.00 (0.1 M output) -> "no ticket named"
# spent $10.00; closed=[9101] attributes $2.00; unowned $8.00 = 3.00 + 4.00 + 1.00.
THREE_TRANSCRIPTS = [
    (P1, "orch #9101", "take #9101 and finish it", [
        ("m5a", "2026-10-10T10:00:00Z", {**ZERO, "input_tokens": 1_000_000}),
    ]),
    (P2, "orch #9301", "take #9301 and finish it", [
        ("m6a", "2026-10-11T10:00:00Z", {**ZERO, "input_tokens": 1_500_000}),
    ]),
    (P3, token_meter.BRAIN_TITLE_LITERAL, "continue the batch", [
        ("m7a", "2026-10-12T10:00:00Z", {**ZERO, "output_tokens": 400_000}),
    ]),
    (U, None, "continue the batch", [
        ("m3a", "2026-10-12T08:00:00Z", {**ZERO, "output_tokens": 100_000}),
    ]),
]

THREE_EVENTS = [
    {"ts": "2026-10-10T10:00:00+00:00", "kind": "issue_taken", "issue": 9101, "how": "claimed"},
    {"ts": "2026-10-10T11:00:00+00:00", "kind": "orch_end", "issue": 9101, "session": "eeee0001",
     "outcome": "closed", "why": "done"},
    {"ts": "2026-10-11T10:00:00+00:00", "kind": "issue_taken", "issue": 9301, "how": "claimed"},
    {"ts": "2026-10-11T11:00:00+00:00", "kind": "orch_end", "issue": 9301, "session": "ffff0002",
     "outcome": "parked", "why": "waits"},
]

SOURCES = ("ticket still open", "brain chat", "no ticket named")


def _write_three(projects_dir: Path) -> None:
    projects_dir.mkdir(parents=True, exist_ok=True)
    for session, title, first, turns in THREE_TRANSCRIPTS:
        lines: list[dict] = []
        if title is not None:
            lines.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        lines.append({"type": "user", "message": {"role": "user", "content": first},
                      "timestamp": "2026-10-10T09:55:00Z"})
        for message_id, stamp, usage in turns:
            lines.append({"type": "assistant",
                          "message": {"id": message_id, "model": "claude-sonnet-5", "usage": usage},
                          "timestamp": stamp})
        (projects_dir / f"{session}.jsonl").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_unowned_splits_into_the_three_sources_to_the_cent(tmp_path: Path):
    _write_three(tmp_path / "three")
    buckets = cost_buckets(tmp_path / "three", _closed(9101), THREE_EVENTS, SINCE, UNTIL, PRICES)
    assert round(buckets.spent_usd, 2) == 10.00
    assert round(buckets.attributed_usd, 2) == 2.00
    assert round(buckets.unowned_usd, 2) == 8.00
    assert set(buckets.unowned_by_source) == set(SOURCES)
    assert round(buckets.unowned_by_source["ticket still open"], 2) == 3.00
    assert round(buckets.unowned_by_source["brain chat"], 2) == 4.00
    assert round(buckets.unowned_by_source["no ticket named"], 2) == 1.00
    assert round(sum(buckets.unowned_by_source.values()), 2) == round(buckets.unowned_usd, 2)


def test_split_turn_remainder_uses_the_time_held_weights(projects_dir: Path):
    # the original fixture with only #9101 closed: S1's #9102 share (75 % of $7.00 = 5.25) is still
    # open, S2's $1.00 sits on #9103 (open here), U's $1.00 names no ticket: 6.25 / 0.00 / 1.00.
    buckets = _run(projects_dir, _closed(A))
    assert round(buckets.attributed_usd, 2) == 1.75
    assert round(buckets.unowned_usd, 2) == 7.25
    assert round(buckets.unowned_by_source["ticket still open"], 2) == 6.25
    assert round(buckets.unowned_by_source["brain chat"], 2) == 0.00
    assert round(buckets.unowned_by_source["no ticket named"], 2) == 1.00
    assert round(sum(buckets.unowned_by_source.values()), 2) == round(buckets.unowned_usd, 2)


def test_module_imports_nothing_from_src():
    import ast

    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    assert not [n for n in names if n.startswith("src")]

