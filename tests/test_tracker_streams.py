"""scripts/tracker_streams.py on a fake gh client (no subprocess, no network). Every plan is built in tmp_path."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tracker_streams", ROOT / "scripts" / "tracker_streams.py")
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


class FakeClient:
    def __init__(self, issues, parents):
        self.issues = {n: dict(id=1000 + n, number=n, **v) for n, v in issues.items()}
        self.parents = dict(parents)
        self.next = 500
        self.writes = 0

    def list_titles(self):
        return {i["title"]: n for n, i in self.issues.items()}

    def get_issue(self, n):
        return self.issues[n]

    def get_parent(self, n):
        return self.parents.get(n)

    def create_issue(self, title, body, labels, closed):
        self.writes += 1
        self.next += 1
        self.issues[self.next] = dict(id=1000 + self.next, number=self.next, title=title, body=body)
        return self.issues[self.next]

    def attach(self, parent, child_id):
        self.writes += 1
        (n,) = [n for n, i in self.issues.items() if i["id"] == child_id]
        self.parents[n] = parent

    def edit_body(self, n, body):
        self.writes += 1
        self.issues[n]["body"] = body


PLAN = {"maps": [{"map": 1, "rows": [
    {"kind": "stream", "title": "Stream: a", "born": "open", "holds": "a.", "numbers": [11, 12, 14]},
    {"kind": "stream", "title": "Stream: b", "born": "closed", "holds": "b.", "numbers": [13, 15]},
]}]}


def fresh():
    issues = {1: dict(title="map", body="")}
    for n in (11, 12, 13):
        issues[n] = dict(title=f"t{n}", body="text\n")
    for n in (14, 15):
        issues[n] = dict(title=f"t{n}", body="Part of #1\n\nrest\n")
    return FakeClient(issues, {11: 1, 12: 1, 13: 1})


def test_row1_counts_second_run_and_dry_run():
    dry = ts.run(PLAN, fresh(), dry_run=True, out=lambda s: None)
    assert len(dry) == 9
    c = fresh()
    lines = ts.run(PLAN, c, out=lambda s: None)
    assert lines == dry
    assert sum(l.startswith("create") for l in lines) == 2
    assert sum(l.startswith("move") for l in lines) == 5
    assert sum(l.startswith("edit body") for l in lines) == 2
    a = c.list_titles()["Stream: a"]
    assert c.parents[11] == a and c.parents[14] == a and c.parents[a] == 1
    assert c.issues[14]["body"] == "rest\n"
    before = c.writes
    assert ts.run(PLAN, c, out=lambda s: None) == []
    assert c.writes == before
    print("\n".join(dry))


def test_dry_run_writes_nothing():
    c = fresh()
    ts.run(PLAN, c, dry_run=True, out=lambda s: None)
    assert c.writes == 0


def test_same_row_parent_stays():
    c = fresh()
    c.parents[12] = 11  # native parent is another ticket of the same row
    lines = ts.run(PLAN, c, out=lambda s: None)
    assert c.parents[12] == 11
    assert not any("#12" in l for l in lines)


def test_body_cases_byte_exact():
    assert ts.strip_part_of("Part of #63. Relates to #92 (the rule)\nmore\n") == "Relates to #92 (the rule)\nmore\n"
    assert ts.strip_part_of("Part of #63\n\nBody line\n\nend") == "Body line\n\nend"
    assert ts.strip_part_of("Part of #63\r\n\r\nBody\r\nx\r\n") == "Body\r\nx\r\n"
    assert ts.strip_part_of("Part of #63. Relates\r\nz\r\n") == "Relates\r\nz\r\n"
    assert ts.strip_part_of("no link\n") == "no link\n"
    # the forms met on a live tracker run
    assert ts.strip_part_of("Part of #108 (Autopilot map).\n\nBody\n") == "Body\n"
    assert ts.strip_part_of("Part of #139 (rehearsal, under the map #108). Worked by it.\nx\n") == "Worked by it.\nx\n"
    assert ts.strip_part_of("Part of #63; the work item of arm B.\n") == "the work item of arm B.\n"
    assert ts.strip_part_of("Part of #108 (a (#63, #61) note). The goal.\n") == "The goal.\n"
    assert ts.strip_part_of("Part of #63 and more\n") == "Part of #63 and more\n"
    late = "\n" * 9 + "Part of #63\n"
    assert ts.strip_part_of(late) == late


def test_plan_argument_is_read_and_passed_to_the_client(tmp_path, monkeypatch, capsys):
    # the plan is a file in tmp_path (no default plan): main reads it, names its repo, runs it dry
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"repo": "owner/name", **PLAN}), encoding="utf-8")
    seen = []

    def fake_client(repo):
        seen.append(repo)
        return fresh()

    monkeypatch.setattr(ts, "GhClient", fake_client)
    assert ts.main(["--plan", str(plan_path), "--dry-run"]) == 0
    assert seen == ["owner/name"]
    assert "9 action(s) (dry-run, nothing written)" in capsys.readouterr().out


def test_plan_argument_is_required():
    with pytest.raises(SystemExit) as exc:
        ts.main([])
    assert exc.value.code == 2


def test_root_row_only_drops_the_line_and_never_unlinks():
    # a root loses its `Part of` line and gets no parent; a root that has a native parent is named, never unlinked
    issues = {7: dict(title="root", body="Part of #108. Goal.\n"), 8: dict(title="linked root", body="x\n")}
    c = FakeClient(issues, {8: 3})
    plan = {"maps": [{"map": 108, "rows": [{"kind": "root", "numbers": [7, 8]}]}]}
    notes = []
    lines = ts.run(plan, c, out=notes.append)
    assert lines == ["edit body #7: drop the Part of line"]
    assert c.issues[7]["body"] == "Goal.\n" and 7 not in c.parents and c.parents[8] == 3
    assert any("#8 has the native parent #3" in s for s in notes)
    assert ts.run(plan, c, out=lambda s: None) == []
