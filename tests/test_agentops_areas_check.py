"""`areas check` and the gh wrapper behind it (issue #317, unit 1). Fake runner, no network."""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from tools.agentops import areas as A
from tools.agentops import core_settings

AREAS = ["area:orchestration", "area:builds", "area:runs", "area:crafting"]


def _issue(n, labels=(), parent=None, state="open"):
    return A.Issue(n, 1000 + n, state, tuple(labels), parent)


def _ten(loner_area=None):
    o, b = "area:orchestration", "area:builds"
    return {
        1: _issue(1, [o]), 2: _issue(2, [o], 1), 3: _issue(3, [o], 1), 4: _issue(4, [o], 3, "closed"),
        5: _issue(5, [b]), 6: _issue(6, [b], 5), 7: _issue(7, [b], 5), 8: _issue(8, [b], 7, "closed"),
        9: _issue(9, ["area:runs"]), 10: _issue(10, [loner_area] if loner_area else []),
    }


def test_check_ten_tickets_then_fixed():
    r = A.check(_ten(), AREAS)
    assert r.no_area == [10] and not r.two_or_more and not r.off_parent
    assert "without an area (1): #10" in A.format_check(r, "o/r")
    assert sum(c["all"] for c in r.counts.values()) == 9 and r.total == 10
    r2 = A.check(_ten("area:crafting"), AREAS)
    assert r2.clean
    assert sum(c["all"] for c in r2.counts.values()) == 10
    assert r2.counts["area:orchestration"] == {"open": 3, "closed": 1, "all": 4}


def test_two_areas_and_off_parent():
    d = {1: _issue(1, ["area:runs", "area:builds"]), 2: _issue(2, ["area:builds"]),
         3: _issue(3, ["area:runs"], 2)}
    r = A.check(d, AREAS)
    assert r.two_or_more == [1] and r.off_parent == [3] and not r.clean
    out = A.format_check(r, "o/r")
    assert "two or more areas (1): #1" in out and "off the parent's area (1): #3" in out


def test_filter_link():
    out = A.format_check(A.check({}, AREAS), "owner/Target-Repo")
    assert ("https://github.com/owner/Target-Repo/issues?q=is%3Aissue+label%3Aarea%3Aorchestration"
            in out.splitlines())


class FakeRunner:
    def __init__(self, routes=None, fail=None):
        self.calls, self.routes, self.fail = [], routes or {}, list(fail or [])

    def __call__(self, args, data=None):
        self.calls.append((list(args), data))
        if self.fail:
            code, err = self.fail.pop(0)
            return SimpleNamespace(returncode=code, stdout=b"", stderr=err.encode())
        for key, out in self.routes.items():
            if key in args[1]:
                return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _raw(n, total=0, pr=False, labels=("area:runs",), state="open"):
    d = {"number": n, "id": 1000 + n, "state": state, "labels": [{"name": x} for x in labels],
         "sub_issues_summary": {"total": total}}
    if pr:
        d["pull_request"] = {}
    return d


def test_load_issues_drops_prs_reads_parents_multipage():
    page1 = json.dumps([_raw(1, total=2), _raw(2), _raw(3, pr=True)])
    page2 = json.dumps([_raw(4, total=1, state="closed"), _raw(5)])
    runner = FakeRunner({
        "issues?state=all": page1 + page2,
        "issues/1/sub_issues": json.dumps([{"number": 2}, {"number": 4}]),
        "issues/4/sub_issues": json.dumps([{"number": 5}]),
    })
    issues = A.GhAreas("o/r", runner, sleep=lambda s: None).load_issues()
    assert sorted(issues) == [1, 2, 4, 5]
    assert issues[2].parent == 1 and issues[4].parent == 1 and issues[5].parent == 4 and issues[1].parent is None
    assert issues[4].state == "closed" and issues[1].labels == ("area:runs",)
    sub_calls = [c[0][1] for c in runner.calls if "sub_issues" in c[0][1]]
    assert sub_calls == ["repos/o/r/issues/1/sub_issues?per_page=100", "repos/o/r/issues/4/sub_issues?per_page=100"]


def test_label_names_slurp_shape():
    runner = FakeRunner({"labels": json.dumps([[{"name": "a"}], [{"name": "b"}]])})
    assert A.GhAreas("o/r", runner).label_names() == {"a", "b"}


def test_writer_argv_and_pacing():
    sleeps, runner = [], FakeRunner()
    gh = A.GhAreas("o/r", runner, pace_seconds=1.5, sleep=sleeps.append)
    gh.add_label(7, "area:runs")
    gh.remove_label(7, "area:builds")
    gh.attach(3, 1007)
    assert runner.calls[0][0] == ["issue", "edit", "7", "--repo", "o/r", "--add-label", "area:runs"]
    assert runner.calls[1][0] == ["issue", "edit", "7", "--repo", "o/r", "--remove-label", "area:builds"]
    assert runner.calls[2][0] == ["api", "-X", "POST", "repos/o/r/issues/3/sub_issues", "--input", "-"]
    assert json.loads(runner.calls[2][1]) == {"sub_issue_id": 1007, "replace_parent": True}
    assert sleeps == [1.5, 1.5, 1.5]


def test_rate_limit_retries_then_succeeds_and_gives_up():
    sleeps = []
    runner = FakeRunner(fail=[(1, "You have exceeded a Secondary Rate Limit"), (1, "was submitted too quickly")])
    A.GhAreas("o/r", runner, pace_seconds=1, sleep=sleeps.append).add_label(1, "x")
    assert len(runner.calls) == 3 and sleeps.count(60) == 2
    runner = FakeRunner(fail=[(1, "secondary rate limit")] * 4)
    try:
        A.GhAreas("o/r", runner, sleep=lambda s: None).add_label(1, "x")
        raise AssertionError("should raise")
    except A.AreasError:
        pass
    assert len(runner.calls) == 4


class FakeGh:
    def __init__(self, issues, labels):
        self._i, self._l = issues, labels

    def load_issues(self):
        return self._i

    def label_names(self):
        return set(self._l)


def _args(cmd, plan="p.json"):
    return argparse.Namespace(areas_command=cmd, plan=plan, dry_run=False)


def test_run_check_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(core_settings, "load", lambda: {"repo": "o/r", "areas": AREAS})
    assert A.run_areas(_args("check"), FakeGh(_ten(), AREAS)) == 1
    assert A.run_areas(_args("check"), FakeGh(_ten("area:crafting"), AREAS)) == 0
    assert "total  10" in capsys.readouterr().out


def test_missing_label_and_empty_setting(monkeypatch, capsys):
    monkeypatch.setattr(core_settings, "load", lambda: {"repo": "o/r", "areas": AREAS})
    assert A.run_areas(_args("apply"), FakeGh({}, AREAS[:3])) == 2
    assert "area:crafting" in capsys.readouterr().out
    monkeypatch.setattr(core_settings, "load", lambda: {"repo": "o/r", "areas": []})
    for cmd in ("check", "apply"):
        assert A.run_areas(_args(cmd), FakeGh({}, [])) == 2
        assert "no areas in config/orchestration.json" in capsys.readouterr().out
