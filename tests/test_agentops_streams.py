import json
import subprocess

from tools.agentops.streams import READ_INTERVAL, StreamKeeper


def _t(n, state, closed=None):
    return {"number": n, "state": state, "closedAt": closed}


class FakeGh:
    def __init__(self):
        self.streams = {
            10: ("Stream: a", "OPEN", [_t(1, "CLOSED", "2026-01-01"), _t(2, "CLOSED", "2026-02-01")]),
            11: ("Stream: b", "CLOSED", [_t(3, "CLOSED", "2026-01-01"), _t(5, "OPEN"), _t(4, "OPEN")]),
            12: ("Stream: c", "OPEN", []),
            13: ("Stream: d", "OPEN", [_t(6, "OPEN")]),
            14: ("Notes", "OPEN", [_t(7, "CLOSED", "2026-01-01")]),
        }
        self.reads = 0
        self.writes = []
        self.comments = []

    def __call__(self, args, stdin=None):
        if args[0] == "api":
            self.reads += 1
            nodes = [{"number": n, "title": t, "state": s, "subIssues": {"nodes": k}}
                     for n, (t, s, k) in self.streams.items()]
            out = {"data": {"search": {"nodes": [{"number": 1, "subIssues": {"nodes": nodes}}]}}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        verb, num = args[1], int(args[2])
        t, _, k = self.streams[num]
        self.streams[num] = (t, "CLOSED" if verb == "close" else "OPEN", k)
        self.writes.append((verb, num))
        self.comments.append((num, args[args.index("--comment") + 1]))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_close_reopen_then_quiet():
    gh, now, logs = FakeGh(), [0.0], []
    k = StreamKeeper("o/r", runner=gh, clock=lambda: now[0], log=logs.append)
    k.tick()
    assert gh.writes == [("close", 10), ("reopen", 11)]
    assert len(gh.comments) == 2 and len(logs) == 2
    assert "#2" in gh.comments[0][1] and "#4" in gh.comments[1][1]
    now[0] = READ_INTERVAL + 1  # #1113: the interval is 15 min now; a read again past it
    k.tick()
    assert len(gh.writes) == 2 and gh.reads == 2


def test_reads_throttled():
    gh, now = FakeGh(), [0.0]
    k = StreamKeeper("o/r", runner=gh, clock=lambda: now[0], log=lambda m: None)
    for _ in range(10):
        k.tick()
        now[0] += 30
    assert gh.reads <= 5


def test_read_interval_is_fifteen_minutes_and_gates_reads():
    # #18: 900 s between two reads of the stream search (3 dispatchers, ~120 points/h of the 5,000/h budget)
    assert READ_INTERVAL == 900.0
    gh, now = FakeGh(), [0.0]
    k = StreamKeeper("o/r", runner=gh, clock=lambda: now[0], log=lambda m: None)
    k.tick()
    now[0] = 899.0
    k.tick()
    assert gh.reads == 1
    now[0] = 901.0
    k.tick()
    assert gh.reads == 2


def test_gh_error_logged_not_raised():
    logs = []
    k = StreamKeeper("o/r", runner=lambda a, s: subprocess.CompletedProcess(a, 1, "", "boom"), log=logs.append)
    k.tick()
    assert logs and "boom" in logs[0]
