"""tools/agentops/core_table.py (issue #153): `core table --repos <path> ...`, run at the home.
No network: two fixture repos under `tmp_path`, the cost column driven by fixture `Turn`s handed
straight to `scripts/token_meter.cost_per_closed_issue` -- the same function the table itself
calls, so acceptance 7's "to the cent" is a real equality, not a tolerance. The "no hand of the
brain" and "label to merge" columns come from a fixture GitHub issue timeline (`gh api .../
timeline`), stubbed through one small reader (`core_table.GhIssueReader`), never events.jsonl
substring matches."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import token_meter as tm  # noqa: E402

from tools.agentops import core_table

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 30, tzinfo=timezone.utc)

PRICES = {
    "claude-sonnet-5": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.2, "output": 10.0},
}

REPO_SLUG = "someone/repo-a"


def _turn(*, message_id, issue, input_tokens, output_tokens) -> tm.Turn:
    return tm.Turn(
        session_id="sess1", agent_id=None, agent_type=None, message_id=message_id,
        model="claude-sonnet-5", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc),
        input_tokens=input_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        output_tokens=output_tokens, issue=issue, title=None, turn_index=0,
    )


def _init_repo(tmp_path: Path, name: str, *, repo_slug: str | None = None) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "config").mkdir()
    (repo / "config" / "orchestration.json").write_text(json.dumps({
        "repo": repo_slug or f"someone/{name}",
        "brain_chat_title": "Brain",
        "context_line": 150000,
        "claude_projects_dir": name,
        "bridge_dir": str(repo / "var" / "agentops"),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
        "home": "someone/home-repo",
        "core_version": "deadbeef",
        "update_rule": "word",
    }, indent=2), encoding="utf-8")

    (repo / "artifacts" / "runs").mkdir(parents=True)
    return repo


def _write_events(repo: Path, issue_numbers: list[int]) -> None:
    events = [
        {"ts": "2026-09-10T00:00:00", "kind": "orch_end", "issue": n, "outcome": "closed", "why": "done"}
        for n in issue_numbers
    ]
    (repo / "artifacts" / "runs" / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _write_outcomes(repo: Path, records: list[dict]) -> None:
    (repo / "artifacts" / "runs" / "agentops_outcomes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _fixture_turns() -> list[tm.Turn]:
    return [
        _turn(message_id="m1", issue=10, input_tokens=1000, output_tokens=200),
        _turn(message_id="m2", issue=20, input_tokens=500, output_tokens=100),
    ]


def _fixture_closed_issues() -> list[dict]:
    return [
        {"number": 10, "closedAt": "2026-09-10T00:00:00Z"},
        {"number": 20, "closedAt": "2026-09-12T00:00:00Z"},
    ]


def _empty_timeline_gh_runner(repo_slug: str, issue_numbers: list[int]):
    """A `gh` stand-in (issue-timeline calls only) that answers every named issue with an empty
    timeline -- for tests that only exercise the cost column."""

    table = {
        ("api", f"repos/{repo_slug}/issues/{n}/timeline", "--paginate"): "[]\n" for n in issue_numbers
    }

    def runner(args):
        key = tuple(args)
        if key not in table:
            raise AssertionError(f"unexpected gh call: {args}")
        return table[key]

    return runner


# -- acceptance 7: the table ------------------------------------------------------------------

def test_two_fixture_repos_print_two_rows_with_seven_columns(tmp_path) -> None:
    repo_a = _init_repo(tmp_path, "repo-a")
    repo_b = _init_repo(tmp_path, "repo-b")
    _write_events(repo_a, [10, 20])
    _write_events(repo_b, [10, 20])
    _write_outcomes(repo_a, [{"issue": 10, "route": "worker/sonnet-5", "class": "redone", "why": "x"}])
    _write_outcomes(repo_b, [{"issue": 10, "route": "worker/sonnet-5", "class": "redone", "why": "x"}])

    row_a = core_table.compute_repo_row(
        repo_a, turns=_fixture_turns(), closed_issues=_fixture_closed_issues(),
        prices=PRICES, since=SINCE, until=UNTIL,
        gh_runner=_empty_timeline_gh_runner("someone/repo-a", [10, 20]),
    )
    row_b = core_table.compute_repo_row(
        repo_b, turns=_fixture_turns(), closed_issues=_fixture_closed_issues(),
        prices=PRICES, since=SINCE, until=UNTIL,
        gh_runner=_empty_timeline_gh_runner("someone/repo-b", [10, 20]),
    )

    rendered = core_table.format_table([row_a, row_b])
    lines = rendered.splitlines()
    # header + 2 rows + one "not checked: no leak words" line per repo without config/core_leak_patterns.json (issue 93)
    assert len(lines) == 5

    for row in (row_a, row_b):
        assert row.issues_closed == 2
        assert row.sent_back == 1
        # an empty timeline names no `ready-for-agent`, so nothing is flagged, but nothing merges
        assert row.closed_no_brain_hand == 2
        assert row.label_to_merge_median is None
        # the seven columns of the issue's table
        assert {
            "issues_closed", "closed_no_brain_hand", "cost_per_closed_issue",
            "label_to_merge_median", "sent_back", "leaks", "core_version",
        } <= set(vars(row))


def test_cost_per_closed_issue_equals_the_meters_own_number_to_the_cent(tmp_path) -> None:
    repo = _init_repo(tmp_path, "solo-repo")
    _write_events(repo, [10, 20])
    turns = _fixture_turns()
    closed_issues = _fixture_closed_issues()

    row = core_table.compute_repo_row(
        repo, turns=turns, closed_issues=closed_issues, prices=PRICES, since=SINCE, until=UNTIL,
        gh_runner=_empty_timeline_gh_runner("someone/solo-repo", [10, 20]),
    )

    # the meter's own number, called directly -- never a reimplementation
    meter_costs = tm.cost_per_closed_issue(turns, closed_issues, PRICES, since=SINCE, until=UNTIL)
    expected = round(sum(c.cost_usd for c in meter_costs) / len(meter_costs), 2)

    assert round(row.cost_per_closed_issue, 2) == expected


# -- the "no hand of the brain" / "label to merge" columns: GitHub issue timelines ------------

def _timeline_clean(ready_at: str, closed_at: str) -> list[dict]:
    return [
        {"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": ready_at},
        {"event": "closed", "created_at": closed_at, "commit_id": None},
    ]


def _timeline_needs_info(ready_at: str, needs_info_at: str, closed_at: str) -> list[dict]:
    return [
        {"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": ready_at},
        {"event": "labeled", "label": {"name": "needs-info"}, "created_at": needs_info_at},
        {"event": "closed", "created_at": closed_at, "commit_id": None},
    ]


def _timeline_reopened(ready_at: str, reopened_at: str, closed_at: str, commit_id: str) -> list[dict]:
    return [
        {"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": ready_at},
        {"event": "reopened", "created_at": reopened_at},
        {"event": "closed", "created_at": closed_at, "commit_id": commit_id},
    ]


def test_gh_timeline_three_issues_one_needs_info_one_reopened_one_clean(tmp_path) -> None:
    repo = _init_repo(tmp_path, "timeline-repo", repo_slug=REPO_SLUG)
    _write_events(repo, [100, 101, 102])

    timelines = {
        100: _timeline_clean("2026-09-10T00:00:00Z", "2026-09-10T01:00:00Z"),  # 3600s
        101: _timeline_needs_info(
            "2026-09-11T00:00:00Z", "2026-09-11T00:30:00Z", "2026-09-11T03:00:00Z",
        ),  # 10800s
        102: _timeline_reopened(
            "2026-09-12T00:00:00Z", "2026-09-12T00:10:00Z", "2026-09-12T02:00:00Z", "deadbeef",
        ),
    }
    commit_committer_date = "2026-09-12T02:05:00Z"  # 7500s from ready

    table = {
        ("api", f"repos/{REPO_SLUG}/issues/{n}/timeline", "--paginate"): json.dumps(tl)
        for n, tl in timelines.items()
    }
    table[("api", f"repos/{REPO_SLUG}/commits/deadbeef", "--jq", ".commit.committer.date")] = (
        commit_committer_date + "\n"
    )

    def gh_runner(args):
        key = tuple(args)
        if key not in table:
            raise AssertionError(f"unexpected gh call: {args}")
        return table[key]

    no_hand, merge_median = core_table._brain_hand_and_merge_columns(repo, {100, 101, 102}, gh_runner=gh_runner)

    assert no_hand == 1  # only #100 has no needs-info label and was never reopened
    # deltas: #100 = 3600s, #101 = 10800s, #102 = 7500s (committer date, not created_at) -> median 7500
    assert merge_median == 7500.0


def test_reader_is_stubbable_with_a_fixture_and_never_touches_the_network() -> None:
    calls = []

    def gh_runner(args):
        calls.append(args)
        return "[]"

    reader = core_table.GhIssueReader(REPO_SLUG, gh_runner=gh_runner)
    assert reader.timeline(1) == []
    assert calls == [["api", f"repos/{REPO_SLUG}/issues/1/timeline", "--paginate"]]
