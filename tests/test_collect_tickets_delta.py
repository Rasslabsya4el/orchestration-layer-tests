"""#181 unit 1: the tickets payload reads GitHub as a delta (the 20 newest-updated issues, 1 point) on a snapshot of
every issue, and a full read only to start, every FULL_RECONCILE_S, or when the delta cannot be trusted.

No network: `subprocess.run` is a fake GitHub holding one set of issues; it answers the three reads the collector
makes (open pages, closed pages, the delta page) with the point cost each really has and counts them in call order.
The cache and the cost log live in tmp_path.

    python -m pytest -q -p no:faulthandler tests/test_collect_tickets_delta.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = next((ROOT / "mods").glob("*/bin/collect.py")).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

COST = {"open": 4, "closed": 2, "delta": 1}


def _stamp(sec: int) -> str:
    return f"2026-10-15T12:{sec // 60:02d}:{sec % 60:02d}Z"


def _node(n: int, *, state: str = "OPEN", updated: int = 0, labels=(), comments=(), parent=None, blockers=()) -> dict:
    return {
        "_blockers": list(blockers),
        "number": n, "title": f"title {n}", "state": state, "stateReason": "COMPLETED" if state == "CLOSED" else None,
        "updatedAt": _stamp(updated), "createdAt": _stamp(0), "author": {"login": "dev"},
        "labels": {"nodes": [{"name": x} for x in labels]},
        "parent": {"number": parent} if parent else None,
        "body": f"body {n}",
        "comments": {"nodes": [{"body": b, "createdAt": _stamp(updated)} for b in comments]},
        "assignees": {"totalCount": 0},
    }


class FakeGithub:
    """One repo of issues. `calls` lists the reads in order ("open", "closed", "delta"). A closed read returns the
    nodes without body, comments and assignees, as QUERY_CLOSED does; the delta page returns every node whole."""

    def __init__(self, nodes: list[dict], pages: int = 1) -> None:
        self.issues = {n["number"]: n for n in nodes}
        self.calls: list[str] = []
        self.pages = pages          # the open read answers this many JSON objects (a --paginate stream)
        self.fail_delta = False
        self.fail_all = False
        self.remaining = 4990

    def put(self, node: dict) -> None:
        self.issues[node["number"]] = node

    def _render(self, node: dict) -> dict:
        """The node as GitHub answers it: each blocker carries its state as of this read."""
        out = {k: v for k, v in node.items() if k != "_blockers"}
        out["blockedBy"] = {"nodes": [{"number": b, "state": self.issues[b]["state"]} for b in node["_blockers"]]}
        return out

    def _page(self, nodes: list[dict], cost: int) -> str:
        nodes = [self._render(n) for n in nodes]
        self.remaining -= cost
        return json.dumps({"data": {
            "rateLimit": {"cost": cost, "remaining": self.remaining},
            "repository": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}})

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 1, "", "not in this test")
        query = next(p for p in cmd if p.startswith("query="))
        if "UPDATED_AT" in query:
            kind = "delta"
        else:
            kind = "closed" if "states: [CLOSED]" in query else "open"
        self.calls.append(kind)
        if self.fail_all or (kind == "delta" and self.fail_delta):
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
        everything = list(self.issues.values())
        if kind == "delta":
            newest = sorted(everything, key=lambda n: n["updatedAt"], reverse=True)[:20]
            return subprocess.CompletedProcess(cmd, 0, self._page(newest, COST["delta"]), "")
        if kind == "open":
            nodes = sorted((n for n in everything if n["state"] == "OPEN"), key=lambda n: n["number"])
            chunks = [nodes[i::self.pages] for i in range(self.pages)]
            out = "".join(self._page(c, COST["open"]) for c in chunks)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        nodes = [{k: v for k, v in n.items() if k not in ("body", "comments", "assignees")}
                 for n in sorted(everything, key=lambda n: n["number"]) if n["state"] == "CLOSED"]
        return subprocess.CompletedProcess(cmd, 0, self._page(nodes, COST["closed"]), "")


def _timeline_unanswered(args, *a, **k):
    """The pipeline's first-`ready-for-agent` time reads a ticket's timeline through `gh_call.run_gh`; answered as a
    failure (no time, nothing cached), so no test asks GitHub for a timeline."""
    return subprocess.CompletedProcess(args, 1, "", "timeline not read in this test")


def _wire(monkeypatch, tmp_path: Path, gh: FakeGithub, name: str = "a") -> Path:
    cache = tmp_path / name / ".cache"
    monkeypatch.setattr(collect, "CACHE_DIR", cache)
    # #182: the pipeline paths resolve at use time beside fast_path(); pin fast_path into the tmp cache
    monkeypatch.setattr(collect, "fast_path", lambda: cache / "collector-fast.json")
    monkeypatch.setattr(collect, "GH_COST_LOG", tmp_path / name / "monitor_gh_cost.jsonl")
    monkeypatch.setattr(collect.subprocess, "run", gh)
    monkeypatch.setattr("tools.agentops.gh_call.run_gh", _timeline_unanswered)
    return cache


def _start_state() -> list[dict]:
    return [
        _node(1, labels=["wayfinder:map"], updated=10),
        _node(2, labels=["ready-for-agent"], updated=20, parent=1),
        _node(3, state="CLOSED", updated=30, parent=1),
        _node(4, labels=["needs-triage"], updated=40),
        _node(5, comments=["<!-- parked-on-run: x -->\nwaiting"], updated=50),
    ]


def _payload(out: dict) -> dict:
    return {k: v for k, v in out.items() if k != "at"}


def test_second_pull_is_one_delta_read_after_the_full_start(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    first = collect.tickets()
    assert gh.calls == ["open", "closed"]
    second = collect.tickets()
    assert gh.calls == ["open", "closed", "delta"]
    assert _payload(second) == _payload(first)


def test_snapshot_plus_delta_equals_a_full_read_of_the_same_github_state(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh, "delta")
    collect.tickets()                                   # the full start
    gh.put(_node(2, state="CLOSED", updated=60, parent=1, labels=["ready-for-agent"], comments=["done"]))  # closed
    gh.put(_node(3, state="OPEN", updated=61, parent=1, labels=["ready-for-agent"]))   # reopened
    gh.put(_node(4, labels=["needs-triage", "parked"], updated=62))                    # relabelled
    gh.put(_node(6, labels=["ready-for-agent"], updated=63, comments=["<!-- send-back -->"]))  # new
    via_delta = collect.tickets()
    assert gh.calls == ["open", "closed", "delta"]

    full_gh = FakeGithub(list(gh.issues.values()))
    _wire(monkeypatch, tmp_path, full_gh, "full")
    via_full = collect.tickets()
    assert full_gh.calls == ["open", "closed"]
    assert _payload(via_delta) == _payload(via_full)
    assert via_delta["issues"]["3"]["open"] is True and via_delta["issues"]["2"]["open"] is False


def test_a_closed_issue_read_by_delta_keeps_the_closed_node_shape(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    gh.put(_node(4, state="CLOSED", updated=60, comments=["a comment"]))
    collect.tickets()
    snap = json.loads((cache / "tickets-snapshot.json").read_text(encoding="utf-8"))
    closed = next(n for n in snap["nodes"] if n["number"] == 4)
    assert closed["state"] == "CLOSED" and not {"body", "comments", "assignees"} & set(closed)
    assert snap["watermark"] == _stamp(60)


def test_more_than_twenty_changes_fall_back_to_the_full_read(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    for n in range(10, 40):                             # 30 issues updated after the watermark: the page overflows
        gh.put(_node(n, updated=100 + n))
    out = collect.tickets()
    assert gh.calls == ["open", "closed", "delta", "open", "closed"]
    assert set(out["issues"]) >= {str(n) for n in range(10, 40)}


def test_a_snapshot_older_than_the_reconcile_interval_is_read_in_full(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    snap_path = cache / "tickets-snapshot.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["full_at"] -= collect.FULL_RECONCILE_S + 1
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    collect.tickets()
    assert gh.calls == ["open", "closed", "open", "closed"]
    assert json.loads(snap_path.read_text(encoding="utf-8"))["full_at"] > snap["full_at"]


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"full_at": 1, "nodes": 3}'])
def test_a_missing_or_unreadable_snapshot_is_read_in_full(monkeypatch, tmp_path, content):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    if content is not None:
        cache.mkdir(parents=True)
        (cache / "tickets-snapshot.json").write_text(content, encoding="utf-8")
    collect.tickets()
    assert gh.calls == ["open", "closed"]


def test_a_failed_delta_serves_the_full_read(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    first = collect.tickets()
    gh.fail_delta = True
    out = collect.tickets()
    assert gh.calls == ["open", "closed", "delta", "open", "closed"]
    assert _payload(out) == _payload(first)


def test_a_failed_delta_and_a_failed_full_read_raise_as_before(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    gh.fail_all = True
    with pytest.raises(RuntimeError):
        collect.tickets()


def test_each_graphql_read_appends_one_cost_line(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state(), pages=2)
    _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    collect.tickets()
    lines = [json.loads(x) for x in collect.GH_COST_LOG.read_text(encoding="utf-8").splitlines()]
    assert [(x["kind"], x["cost"]) for x in lines] == [("open", 4), ("open", 4), ("closed", 2), ("delta", 1)]
    assert [x["remaining"] for x in lines] == [4986, 4982, 4980, 4979]
    assert all(set(x) == {"at", "kind", "cost", "remaining"} for x in lines)
    assert lines[0]["at"][4] == "-" and lines[0]["at"][10] == " " and len(lines[0]["at"]) == 19


def test_a_failed_cost_log_write_is_ignored(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the log's folder should be", encoding="utf-8")
    monkeypatch.setattr(collect, "GH_COST_LOG", blocker / "sub" / "monitor_gh_cost.jsonl")
    assert collect.tickets()["issues"]


def test_a_read_without_rate_limit_logs_nothing_and_raises_nothing(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    _wire(monkeypatch, tmp_path, gh)
    real_page = gh._page

    def page_without_rate_limit(nodes, cost):
        data = json.loads(real_page(nodes, cost))["data"]
        return json.dumps({"data": {"repository": data["repository"]}})

    monkeypatch.setattr(gh, "_page", page_without_rate_limit)
    assert collect.tickets()["issues"]
    assert collect.tickets()["issues"]
    assert not collect.GH_COST_LOG.exists()


def test_the_tickets_payload_is_shared_for_twelve_seconds(monkeypatch):
    seen = {}
    monkeypatch.setattr(collect, "shared_result", lambda mode, fresh_for, compute: seen.update({mode: fresh_for}) or "{}")
    monkeypatch.setattr(sys, "argv", ["collect.py", "tickets"])
    with pytest.raises(SystemExit):
        collect.main()
    assert seen["tickets"] == 12


def _blocked_repo() -> FakeGithub:
    """#10 (open map), #20 blocked by #10, and 25 older fillers, so a delta page of 20 never holds #20."""
    gh = FakeGithub([_node(10, labels=["wayfinder:map"], updated=6),
                     _node(20, labels=["ready-for-agent"], updated=5, blockers=[10])])
    for k in range(25):
        gh.put(_node(100 + k, updated=10 + k))
    return gh


def test_a_blocker_closed_in_the_delta_unblocks_the_ticket_it_blocked(monkeypatch, tmp_path):
    gh = _blocked_repo()
    _wire(monkeypatch, tmp_path, gh, "delta")
    assert collect.tickets()["issues"]["20"]["state"] == "blocked"
    gh.put(_node(10, state="CLOSED", labels=["wayfinder:map"], updated=60))   # only #10 is on the delta page
    via_delta = collect.tickets()
    assert gh.calls == ["open", "closed", "delta"]
    assert via_delta["issues"]["20"]["state"] == "ready"
    full_gh = FakeGithub(list(gh.issues.values()))
    _wire(monkeypatch, tmp_path, full_gh, "full")
    assert _payload(via_delta) == _payload(collect.tickets())


def test_an_issue_opened_and_closed_inside_the_window_survives_the_reconcile(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    gh.put(_node(7, state="CLOSED", updated=60, parent=1))                    # created and closed after the full start
    via_delta = collect.tickets()
    assert "7" in via_delta["issues"] and via_delta["issues"]["7"]["open"] is False
    snap_path = cache / "tickets-snapshot.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["full_at"] -= collect.FULL_RECONCILE_S + 1
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    gh.calls.clear()
    after = collect.tickets()
    assert gh.calls == ["open", "closed"]                                    # a due full read refreshes the closed list
    assert _payload(after) == _payload(via_delta)


def test_a_first_start_with_no_snapshot_may_use_the_closed_cache(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    collect.tickets()
    (cache / "tickets-snapshot.json").unlink()
    gh.calls.clear()
    collect.tickets()
    assert gh.calls == ["open"]


def test_an_older_snapshot_never_replaces_a_newer_one_on_disk(monkeypatch, tmp_path):
    gh = FakeGithub(_start_state())
    cache = _wire(monkeypatch, tmp_path, gh)
    cache.mkdir(parents=True)
    newer = [_node(1, updated=90)]
    older = [_node(1, updated=40)]
    collect._write_snapshot(newer, 1000.0)
    collect._write_snapshot(older, 1000.0)                                   # a slower writer, same full read
    snap = json.loads((cache / "tickets-snapshot.json").read_text(encoding="utf-8"))
    assert snap["watermark"] == _stamp(90) and snap["nodes"][0]["updatedAt"] == _stamp(90)
    collect._write_snapshot(older, 2000.0)                                   # a later full read is the truth (a deleted newest issue)
    snap = json.loads((cache / "tickets-snapshot.json").read_text(encoding="utf-8"))
    assert snap["watermark"] == _stamp(40)
