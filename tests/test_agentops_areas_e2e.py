"""`areas apply` then `areas check` end to end on a stateful fake `gh` (issue #317, rows 1 and 3)."""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from tools.agentops import areas as A
from tools.agentops import core_settings

AREAS = ["area:orchestration", "area:builds", "area:runs", "area:crafting"]


class StatefulGh:
    """Answers the reads GhAreas makes from a live-like state and applies its writes to that state."""

    def __init__(self):
        # 2 roots (#1, #5) with 3 descendants each, one at depth 2; 2 loners (#9, #10); a PR #11.
        self.parent = {2: 1, 3: 1, 4: 3, 6: 5, 7: 5, 8: 7}
        self.labels = {n: ["bug"] if n == 2 else [] for n in range(1, 11)}
        self.calls = []

    def _ok(self, obj=None):
        out = json.dumps(obj).encode() if obj is not None else b""
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    def __call__(self, args, data=None):
        self.calls.append(list(args))
        if args[:2] == ["issue", "edit"]:
            n, flag, label = int(args[2]), args[5], args[6]
            if flag == "--add-label" and label not in self.labels[n]:
                self.labels[n].append(label)
            if flag == "--remove-label" and label in self.labels[n]:
                self.labels[n].remove(label)
            return self._ok()
        if args[:3] == ["api", "-X", "POST"]:
            body = json.loads(data)
            self.parent[body["sub_issue_id"] - 1000] = int(args[3].split("/")[-2])
            return self._ok({})
        path = args[1]
        if "/labels?" in path:
            return self._ok([{"name": a} for a in AREAS + ["bug"]])
        if "/sub_issues?" in path:
            p = int(path.split("/")[-2])
            return self._ok([{"number": c} for c, q in sorted(self.parent.items()) if q == p])
        if "/issues?" in path:
            kids = {}
            for c, q in self.parent.items():
                kids[q] = kids.get(q, 0) + 1
            items = [{"number": n, "id": 1000 + n, "state": "open",
                      "labels": [{"name": x} for x in self.labels[n]],
                      "sub_issues_summary": {"total": kids.get(n, 0)}} for n in range(1, 11)]
            items.append({"number": 11, "id": 1011, "state": "open", "labels": [], "pull_request": {}})
            return self._ok(items)
        raise AssertionError(f"unexpected gh call {args}")


def _run(cmd, gh, tmp_path=None, plan=None, dry_run=False):
    ns = argparse.Namespace(areas_command=cmd, dry_run=dry_run, plan=None)
    if plan is not None:
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(plan), encoding="utf-8")
        ns.plan = str(p)
    return A.run_areas(ns, A.GhAreas("o/r", runner=gh, pace_seconds=0, sleep=lambda s: None))


def test_apply_then_check_end_to_end(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core_settings, "load", lambda: {"repo": "o/r", "areas": AREAS})
    fake = StatefulGh()
    plan = {"roots": {"1": "area:orchestration", "5": "area:builds"}, "loners": {"9": "area:runs"}, "links": []}

    assert _run("apply", fake, tmp_path, plan, dry_run=True) == 0
    assert not [c for c in fake.calls if c[:2] == ["issue", "edit"] or "POST" in c]
    capsys.readouterr()

    assert _run("apply", fake, tmp_path, plan) == 0
    out = capsys.readouterr().out
    assert "not in the plan (1)" in out and "#10" in out.splitlines()
    assert "links 0, labelled 9, unchanged 0, not in the plan 1" in out
    assert sum(1 for n in range(1, 10) if len([x for x in fake.labels[n] if x in AREAS]) == 1) == 9
    assert fake.labels[2] == ["bug", "area:orchestration"]

    assert _run("check", fake) == 1
    assert "without an area (1): #10" in capsys.readouterr().out

    writes_before = len([c for c in fake.calls if c[0] == "issue" or "POST" in c])
    assert _run("apply", fake, tmp_path, plan) == 0
    assert len([c for c in fake.calls if c[0] == "issue" or "POST" in c]) == writes_before

    plan["loners"]["10"] = "area:crafting"
    assert _run("apply", fake, tmp_path, plan) == 0
    assert _run("check", fake) == 0

    # row 3: only area labels added/removed and the sub-issue POST; nothing closed, reopened or edited otherwise
    for c in fake.calls:
        if c[0] == "issue":
            assert c[:2] == ["issue", "edit"] and c[5] in ("--add-label", "--remove-label")
            assert c[6] in AREAS and len(c) == 7
        else:
            assert c[0] == "api" and (c[1] != "-X" or c[1:3] == ["-X", "POST"])


def test_link_moves_loner_then_area_follows(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core_settings, "load", lambda: {"repo": "o/r", "areas": AREAS})
    fake = StatefulGh()
    plan = {"roots": {"1": "area:orchestration", "5": "area:builds"}, "loners": {"9": "area:runs"},
            "links": [[10, 5]]}
    assert _run("apply", fake, tmp_path, plan) == 0
    assert fake.parent[10] == 5 and "area:builds" in fake.labels[10]
    assert _run("check", fake) == 0
    assert _run("apply", fake, tmp_path, plan) == 0
    assert "links 0, labelled 0, unchanged 10, not in the plan 0" in capsys.readouterr().out
