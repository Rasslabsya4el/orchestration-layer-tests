"""#1113 unit 1: the monitor's shared cache makes one `gh api graphql` read per window for tickets, from every caller.

Before: a caller that found a live collector on the lock waited 10 s (50 polls of 0.2 s) and then computed beside it,
without a lock. A tickets read longer than 10 s therefore came out as 2-3 reads 17 s apart (02:40:36 / :53 / 02:41:09,
2026-10-16). A waiter now waits for the holder's file, and the slow mode's closed-issue lists are read once per process.

Send-back (#1113 unit 1): the stale-lock limit and the waiter's deadline are per mode (`shared_limits`); a dead holder's
lock is stolen at once; at the deadline a waiter serves the holder's last file, else computes itself, and never raises.

No PoB, no network: `subprocess.run` is a counting fake of `gh`, the cache and the lock live in tmp_path. The second
caller is a thread: the lock is a file, so it is the same contract as a second collector process.

    python -m pytest -q -p no:faulthandler tests/test_issue1113_monitor_reads.py
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

REAL_SLEEP = time.sleep
REAL_TIME = time.time
# one empty GraphQL page: tickets() reads no issue, the merge line is absent
GRAPHQL_PAGE = json.dumps({"data": {"repository": {"issues": {
    "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}})


class FakeGh:
    """Counts the `gh api graphql` (tickets) and `gh issue list` (closed issues) reads; the closed list is split by
    its reader (`fetch_closed_issues` asks for closedAt, `issue_info` for stateReason). The merge line's command fails
    at once, so `_live_merge_branches` reads it as absent. `hold` runs inside a read, before it answers."""

    def __init__(self, hold=None):
        self.graphql = 0  # open-issue reads of tickets(); the closed read (#18) is `closed_graphql`
        self.closed_graphql = 0
        self.issue_list = 0
        self.closed_fetch = 0
        self.closed_info = 0
        self.hold = hold
        self.lock = threading.Lock()

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "api", "graphql"]:
            closed = any("states: [CLOSED]" in part for part in cmd)  # #18: the closed read is apart from the open one
            with self.lock:
                if closed:
                    self.closed_graphql += 1
                    return subprocess.CompletedProcess(cmd, 0, GRAPHQL_PAGE, "")
                self.graphql += 1
            if self.hold:
                self.hold()
            return subprocess.CompletedProcess(cmd, 0, GRAPHQL_PAGE, "")
        if cmd[:3] == ["gh", "issue", "list"]:
            fetch = any("closedAt" in part for part in cmd)  # the real argv is one "number,closedAt" element
            with self.lock:
                self.issue_list += 1
                if fetch:
                    self.closed_fetch += 1
                else:
                    self.closed_info += 1
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return subprocess.CompletedProcess(cmd, 1, "", "no merge line in this test")


class Polls:
    """Stands in for time.sleep: counts the waiter's polls and sleeps `real` seconds of real time. With `cap`, the
    (cap+1)-th poll raises: a waiter that never gives up is a red test, not a hang."""

    def __init__(self, real=0.001, cap=None):
        self.n = 0
        self.real = real
        self.cap = cap
        self.lock = threading.Lock()

    def __call__(self, seconds):
        with self.lock:
            self.n += 1
            n = self.n
        if self.cap is not None and n > self.cap:
            raise AssertionError(f"the waiter polled more than {self.cap} times and never gave up")
        REAL_SLEEP(self.real)


def _age(path, seconds):
    t = REAL_TIME() - seconds
    os.utime(path, (t, t))


def test_sequential_callers_in_one_window_make_one_graphql_read(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    gh = FakeGh()
    monkeypatch.setattr(collect.subprocess, "run", gh)
    outs = [collect.shared_result("tickets", 170, collect.tickets) for _ in range(5)]
    assert gh.graphql == 1, f"5 calls inside one 3-minute window made {gh.graphql} tickets reads"
    assert len(set(outs)) == 1


def test_caller_arriving_while_a_collector_computes_waits_and_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    polls = Polls()
    monkeypatch.setattr(collect.time, "sleep", polls)
    entered = threading.Event()

    def hold():
        # the holder's read stays open until the waiter has polled 60 times (the old wait gave up at 50)
        entered.set()
        t0 = REAL_TIME()
        while polls.n < 60 and REAL_TIME() - t0 < 20:
            REAL_SLEEP(0.001)

    gh = FakeGh(hold=hold)
    monkeypatch.setattr(collect.subprocess, "run", gh)
    holder: dict = {}
    errors: list = []

    def collector():
        try:
            holder["text"] = collect.shared_result("tickets", 170, collect.tickets)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    thread = threading.Thread(target=collector, daemon=True)
    thread.start()
    assert entered.wait(10), "the holder never reached its read"
    waiter_text = collect.shared_result("tickets", 170, collect.tickets)
    thread.join(20)
    assert not thread.is_alive() and not errors, errors
    assert gh.graphql == 1, f"a waiter beside a live collector made the read again: {gh.graphql} tickets reads"
    assert polls.n >= 60, "the waiter gave up before the collector finished"
    assert waiter_text == holder["text"]


def test_closed_issue_list_is_read_once_per_slow_window(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    gh = FakeGh()
    monkeypatch.setattr(collect.subprocess, "run", gh)
    for _ in range(4):
        collect.shared_result("slow", 590, collect.issue_info)
    assert gh.issue_list == 1, f"4 slow calls inside one 10-minute window read the closed list {gh.issue_list} times"


# ---- send-back (#1113 unit 1): per-mode limits, a dead holder, the deadline, the slow path


def test_dead_holder_lock_is_stolen_at_once_and_read_once(tmp_path, monkeypatch):
    """(a) a crashed holder left its lock 200 s old: older than tickets' stale limit (150 s), no live holder.
    The waiter steals it on its first look and makes exactly one read."""
    cache = tmp_path / ".cache"
    cache.mkdir()
    monkeypatch.setattr(collect, "CACHE_DIR", cache)
    lock = cache / "shared-tickets.lock"
    lock.write_text("")
    _age(lock, 200)
    polls = Polls(real=0.01, cap=20)
    monkeypatch.setattr(collect.time, "sleep", polls)
    gh = FakeGh()
    monkeypatch.setattr(collect.subprocess, "run", gh)
    text = collect.shared_result("tickets", 170, collect.tickets)
    assert gh.graphql == 1, f"a dead holder's lock made {gh.graphql} tickets reads"
    assert isinstance(json.loads(text), dict)
    assert not lock.exists(), "the stolen lock was not released after the read"
    assert polls.n == 0, f"the waiter polled {polls.n} times beside a dead holder's lock"


def test_deadline_with_a_live_looking_lock_reads_itself_and_raises_nothing(tmp_path, monkeypatch):
    """(b) a lock that looks live (touched every 50 ms, never released) and no file: at the deadline the waiter
    computes its own read, returns it, and leaves the lock alone."""
    cache = tmp_path / ".cache"
    cache.mkdir()
    monkeypatch.setattr(collect, "CACHE_DIR", cache)
    monkeypatch.setattr(collect, "SHARED_LIMITS", {"tickets": 0.6}, raising=False)
    monkeypatch.setattr(collect, "SHARED_WAIT_MARGIN_S", 0.4, raising=False)
    lock = cache / "shared-tickets.lock"
    lock.write_text("")
    stop = threading.Event()

    def keep_alive():
        while not stop.is_set():
            _age(lock, 0)
            REAL_SLEEP(0.05)

    keeper = threading.Thread(target=keep_alive, daemon=True)
    keeper.start()
    monkeypatch.setattr(collect.time, "sleep", Polls(real=0.2, cap=30))  # the 1 s deadline is about 5 polls
    gh = FakeGh()
    monkeypatch.setattr(collect.subprocess, "run", gh)
    try:
        text = collect.shared_result("tickets", 170, collect.tickets)
    finally:
        stop.set()
        keeper.join(5)
    assert gh.graphql == 1, f"the deadline read made {gh.graphql} tickets reads, expected 1"
    assert isinstance(json.loads(text), dict)
    assert lock.exists(), "a waiter released a lock it does not hold"


def test_deadline_serves_the_holders_last_file_without_a_read(tmp_path, monkeypatch):
    """(b2) at the deadline with the holder's last file on disk, the waiter serves that file and reads nothing."""
    cache = tmp_path / ".cache"
    cache.mkdir()
    monkeypatch.setattr(collect, "CACHE_DIR", cache)
    monkeypatch.setattr(collect, "SHARED_LIMITS", {"tickets": 0.6}, raising=False)
    monkeypatch.setattr(collect, "SHARED_WAIT_MARGIN_S", 0.4, raising=False)
    last = '{"issues": {}, "last": true}'
    shared = cache / "shared-tickets.json"
    shared.write_text(last, encoding="utf-8")
    _age(shared, 1000)  # not fresh: the 170 s window is over
    lock = cache / "shared-tickets.lock"
    lock.write_text("")
    stop = threading.Event()

    def keep_alive():
        while not stop.is_set():
            _age(lock, 0)
            REAL_SLEEP(0.05)

    keeper = threading.Thread(target=keep_alive, daemon=True)
    keeper.start()
    monkeypatch.setattr(collect.time, "sleep", Polls(real=0.2, cap=30))
    gh = FakeGh()
    monkeypatch.setattr(collect.subprocess, "run", gh)
    try:
        text = collect.shared_result("tickets", 170, collect.tickets)
    finally:
        stop.set()
        keeper.join(5)
    assert text == last
    assert gh.graphql == 0, f"the deadline read the tickets again ({gh.graphql} reads) instead of serving the file"


def test_fast_mode_has_the_short_limits():
    """(c) the stale limit and the waiter's deadline (stale + a few seconds) per mode; fast keeps its 90 s."""
    assert collect.shared_limits("fast") == (90, 95)
    assert collect.shared_limits("tickets") == (150, 155)
    assert collect.shared_limits("orch") == (200, 205)
    assert collect.shared_limits("slow") == (330, 335)


# ---- #18 unit 2: tickets() reads OPEN issues on every compute; the closed ones come from a disk cache (30 min)

FIXTURES = ROOT / "tests" / "fixtures"
MARK = "<!-- parked-on-run: job 7 -->\nwaiting"


def _node(n, state="OPEN", parent=None, labels=(), blockers=(), comments=(), assignees=0, reason=None):
    return {"number": n, "title": f"t{n}", "state": state, "stateReason": reason or ("COMPLETED" if state == "CLOSED" else None),
            "updatedAt": f"2026-10-{1 + n % 9:02d}T10:00:00Z", "createdAt": f"2026-09-{1 + n % 9:02d}T10:00:00Z",
            "author": {"login": "owner"}, "labels": {"nodes": [{"name": l} for l in labels]},
            "parent": {"number": parent} if parent else None,
            "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blockers]},
            "comments": {"nodes": [{"body": c, "createdAt": "2026-10-01T10:00:00Z"} for c in comments]},
            "assignees": {"totalCount": assignees}}


def dataset():
    """open roots with closed and open kids, a closed grand-kid under a closed kid, an open kid of a closed parent
    (a root), blockers (open and closed), parked flags, a closed ticket nobody reads"""
    return [
        _node(1, labels=["ready-for-agent"]),
        _node(2, "CLOSED", parent=1),
        _node(3, "CLOSED", parent=2, reason="NOT_PLANNED", labels=["wontfix"]),
        _node(4, parent=1, labels=["needs-info"]),
        _node(5, "CLOSED"),                                    # closed, no parent: nobody reads it
        _node(6, parent=5, labels=["parked"]),                 # open kid of a closed parent: a root
        _node(7, parent=6, blockers=[(4, "OPEN"), (2, "CLOSED"), (5, "CLOSED")], comments=[MARK]),
        _node(8, "CLOSED", parent=7, blockers=[(4, "OPEN")]),  # a closed issue holding an open blocker
        _node(9, labels=["needs-triage"], blockers=[(1, "OPEN")]),
        _node(10, comments=[MARK], assignees=1),
        _node(11, "CLOSED", parent=3),
    ]


class RepoGh:
    """`gh api graphql` over a dataset: serves the nodes the query's `states:` asks for (all when it has none, the
    old query), 100 per page like --paginate, and counts the calls and the pages (GitHub points: 1 per page here)."""

    def __init__(self, nodes):
        self.nodes = sorted(nodes, key=lambda n: n["number"])
        self.calls = []  # (kind, pages)

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 1, "", "no merge line in this test")
        query = next(p for p in cmd if p.startswith("query="))
        kind = "open" if "states: [OPEN]" in query else "closed" if "states: [CLOSED]" in query else "all"
        rows = [n for n in self.nodes if kind == "all" or n["state"] == kind.upper()]
        pages = [rows[i:i + 100] for i in range(0, len(rows), 100)] or [[]]
        self.calls.append((kind, len(pages)))
        out = "".join(json.dumps({"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": i < len(pages) - 1, "endCursor": f"c{i}"}, "nodes": p}}}}) for i, p in enumerate(pages))
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def points(self):
        return sum(p for _, p in self.calls)


def _tab(monkeypatch, tmp_path, gh):
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    monkeypatch.setattr(collect, "FULL_RECONCILE_S", 0)  # #181: these tests pin the full read; no snapshot is ever young
    (tmp_path / ".cache" / "tickets-snapshot.json").unlink(missing_ok=True)  # and each pull is a first start (closed cache)
    monkeypatch.setattr(collect.subprocess, "run", gh)
    out = collect.tickets()
    out.pop("at")
    return json.loads(json.dumps(out))


def test_tab_output_equals_the_one_before_the_change(tmp_path, monkeypatch):
    """golden: `tests/fixtures/issue18_tickets_golden.json` was captured from the all-issues query on `dataset()`"""
    golden = json.loads((FIXTURES / "issue18_tickets_golden.json").read_text(encoding="utf-8"))
    assert _tab(monkeypatch, tmp_path, RepoGh(dataset())) == golden


def test_computes_in_one_closed_window_make_open_only_reads_and_one_closed_read(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    for _ in range(5):
        _tab(monkeypatch, tmp_path, gh)
    kinds = [k for k, _ in gh.calls]
    assert kinds.count("open") == 5 and kinds.count("closed") == 1 and "all" not in kinds, kinds


def test_closed_cache_is_read_again_after_30_minutes(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    _tab(monkeypatch, tmp_path, gh)
    cache = tmp_path / ".cache" / "closed-tickets.json"
    assert cache.exists()
    _age(cache, 29 * 60)
    _tab(monkeypatch, tmp_path, gh)
    assert [k for k, _ in gh.calls].count("closed") == 1
    _age(cache, 31 * 60)
    _tab(monkeypatch, tmp_path, gh)
    assert [k for k, _ in gh.calls].count("closed") == 2


def test_250_open_issues_are_3_pages_and_the_closed_part_is_not_paged_on_a_cached_compute(tmp_path, monkeypatch):
    nodes = [_node(n) for n in range(1, 251)] + [_node(n, "CLOSED") for n in range(251, 1001)]
    gh = RepoGh(nodes)
    _tab(monkeypatch, tmp_path, gh)
    assert gh.calls == [("open", 3), ("closed", 8)]
    gh.calls.clear()
    _tab(monkeypatch, tmp_path, gh)
    assert gh.calls == [("open", 3)], "a compute inside the closed window made a closed read"
    # before: every compute paged all 1000 issues = 10 pages
    old = RepoGh(nodes)
    old(["gh", "api", "graphql", "-f", "query=query { issues(first:100) }"])
    assert old.points() == 10


def test_a_failed_closed_read_serves_the_stale_cache_and_no_cache_raises(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    want = _tab(monkeypatch, tmp_path, gh)
    _age(tmp_path / ".cache" / "closed-tickets.json", 99 * 60)

    def broken_closed(cmd, *a, **k):
        if any("states: [CLOSED]" in p for p in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "rate limited")
        return gh(cmd, *a, **k)

    assert _tab(monkeypatch, tmp_path, broken_closed) == want
    fresh = tmp_path / "other"
    monkeypatch.setattr(collect, "CACHE_DIR", fresh / ".cache")
    try:
        collect.tickets()
    except RuntimeError as exc:
        assert "rate limited" in str(exc)
    else:
        raise AssertionError("no closed cache and a failed closed read must raise")


def test_a_ticket_closed_inside_the_window_forces_one_closed_read_and_shows_as_closed(tmp_path, monkeypatch):
    """(a) compute 1 has issue 7 open; 2 min later (inside the TTL) 7 is closed: the open read lacks it and the closed
    cache lacks it too, so compute 2 makes exactly 1 extra closed read and 7 is in the tab as closed"""
    gh = RepoGh(dataset())
    first = _tab(monkeypatch, tmp_path, gh)
    assert first["issues"]["7"]["state"] != "closed"
    _age(tmp_path / ".cache" / "closed-tickets.json", 2 * 60)
    next(n for n in gh.nodes if n["number"] == 7)["state"] = "CLOSED"
    second = _tab(monkeypatch, tmp_path, gh)
    assert [k for k, _ in gh.calls] == ["open", "closed", "open", "closed"], gh.calls
    assert second["issues"]["7"]["state"] == "closed"
    third = _tab(monkeypatch, tmp_path, gh)  # 7 is in the closed cache now and in no open read: no more forcing
    assert [k for k, _ in gh.calls].count("closed") == 2
    assert third == second


def test_no_ticket_lost_means_no_extra_closed_read(tmp_path, monkeypatch):
    """(b) a new open ticket appears, none is lost: the closed cache is not read again"""
    gh = RepoGh(dataset())
    _tab(monkeypatch, tmp_path, gh)
    gh.nodes.append(_node(12))
    _tab(monkeypatch, tmp_path, gh)
    assert [k for k, _ in gh.calls] == ["open", "closed", "open"], gh.calls


# ---- #18 unit 2, second send-back: reopen, retry backoff, corrupt cache, failed cache write


def _breaking(gh):
    """gh whose closed read fails (rate limited) and counts its tries; the open read goes on"""
    box = SimpleNamespace(closed_tries=0)

    def run(cmd, *a, **k):
        if any("states: [CLOSED]" in p for p in cmd):
            box.closed_tries += 1
            return subprocess.CompletedProcess(cmd, 1, "", "rate limited")
        return gh(cmd, *a, **k)

    run.box = box
    return run


def test_a_ticket_reopened_inside_the_window_is_open_in_the_tab(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    first = _tab(monkeypatch, tmp_path, gh)
    assert first["issues"]["5"]["state"] if "5" in first["issues"] else True  # 5 is closed, not in the tab
    next(n for n in gh.nodes if n["number"] == 5)["state"] = "OPEN"  # reopened; the closed cache still holds a copy
    again = _tab(monkeypatch, tmp_path, gh)
    assert again["issues"]["5"]["open"] is True and again["issues"]["5"]["state"] != "closed"
    assert [k for k, _ in gh.calls] == ["open", "closed", "open"], gh.calls


def test_a_failed_closed_refresh_is_retried_no_sooner_than_5_minutes(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    want = _tab(monkeypatch, tmp_path, gh)
    cache = tmp_path / ".cache" / "closed-tickets.json"
    _age(cache, 99 * 60)
    bad = _breaking(gh)
    assert _tab(monkeypatch, tmp_path, bad) == want and bad.box.closed_tries == 1
    attempt = tmp_path / ".cache" / "closed-tickets.attempt"
    for minutes in (1, 2):
        _age(attempt, minutes * 60)
        assert _tab(monkeypatch, tmp_path, bad) == want
        assert bad.box.closed_tries == 1, f"{minutes} min after a failed refresh the closed read was tried again"
    _age(attempt, 6 * 60)
    assert _tab(monkeypatch, tmp_path, bad) == want
    assert bad.box.closed_tries == 2


def test_a_corrupt_closed_cache_is_absent_not_a_crash(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    want = _tab(monkeypatch, tmp_path, gh)
    cache = tmp_path / ".cache" / "closed-tickets.json"
    for junk in ("{}", "not json", '[1, 2]', ""):
        cache.write_text(junk, encoding="utf-8")
        assert _tab(monkeypatch, tmp_path, gh) == want, junk
        assert isinstance(json.loads(cache.read_text(encoding="utf-8")), list)
    cache.write_text("{}", encoding="utf-8")
    try:
        _tab(monkeypatch, tmp_path, _breaking(gh))
    except RuntimeError as exc:
        assert "rate limited" in str(exc)
    else:
        raise AssertionError("a corrupt cache and a failed read must raise RuntimeError")


def test_a_failed_cache_write_is_ignored(tmp_path, monkeypatch):
    gh = RepoGh(dataset())
    want = _tab(monkeypatch, tmp_path / "ref", RepoGh(dataset()))

    def boom(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(collect.os, "replace", boom)
    assert _tab(monkeypatch, tmp_path, gh) == want
