"""#199 unit 3: the flow-fix missed-label count script, on a faked `run_gh` (no real gh).

Expected lines are written by hand from the fixture issues and timelines below (not recomputed by the script).
"""
import importlib.util
import json
import os


from tests._gh_guard import _no_real_gh  # noqa: F401

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "counts", "199", "flow_fix_missed.py")
REPO = "Rasslabsya4el/orchestration-layer"
SINCE, UNTIL = "2026-10-10", "2026-10-17"


def load():
    spec = importlib.util.spec_from_file_location("flow_fix_missed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def labeled(name, at):
    return {"event": "labeled", "label": {"name": name}, "created_at": at}


def issue(number, created, title, labels):
    return {"number": number, "title": title, "createdAt": created, "labels": [{"name": n} for n in labels]}


class FakeGh:
    """Stands in for run_gh: the issue list and the issue timelines, nothing else."""

    def __init__(self, issues, timelines):
        self.issues = issues
        self.timelines = timelines
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        prefix = f"repos/{REPO}/issues/"
        if args[0] == "api" and args[1].startswith(prefix) and args[1].endswith("/timeline"):
            number = int(args[1][len(prefix):-len("/timeline")])
            return json.dumps(self.timelines[number])
        raise AssertionError(f"unexpected gh call: {args}")


def window_fixture():
    issues = [
        issue(101, "2026-10-11T09:00:00Z", "Flow fix: offer stalls", ["core-issue", "flow-fix"]),
        issue(102, "2026-10-12T10:00:00Z", "Core: add count script", ["core-issue", "ready-for-agent"]),
        issue(103, "2026-10-09T23:59:00Z", "Core: before the window", ["core-issue"]),
        issue(104, "2026-10-17T00:00:00Z", "Core: on the exclusive end day", ["core-issue"]),
        issue(105, "2026-10-13T10:00:00Z", "Not a core issue", ["bug"]),
    ]
    timelines = {
        101: [labeled("core-issue", "2026-10-11T09:00:00Z"), labeled("flow-fix", "2026-10-11T09:00:05Z")],
        102: [labeled("core-issue", "2026-10-12T10:00:01Z"), labeled("ready-for-agent", "2026-10-12T10:00:02Z")],
        103: [labeled("core-issue", "2026-10-09T23:59:00Z")],
        104: [labeled("core-issue", "2026-10-17T00:00:00Z")],
        105: [labeled("bug", "2026-10-13T10:00:00Z")],
    }
    return issues, timelines


def run(monkeypatch, tmp_path, capsys, fake, argv):
    mod = load()
    cfg = tmp_path / "orchestration.json"
    cfg.write_text(json.dumps({"repo": REPO}), encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG", str(cfg))
    monkeypatch.setattr(mod, "run_gh", fake)
    mod.main(argv)
    return capsys.readouterr().out.strip().splitlines()


def test_lists_only_the_core_issue_created_without_flow_fix(monkeypatch, tmp_path, capsys):
    fake = FakeGh(*window_fixture())
    lines = run(monkeypatch, tmp_path, capsys, fake, [SINCE, UNTIL])
    # #101 has flow-fix at creation: not listed; #102 is the only one left in the window
    assert lines == [
        "#102 2026-10-12T10:00:00Z Core: add count script | flow-fix later: no",
        "total: 1",
    ]


def test_issue_outside_the_window_is_not_listed(monkeypatch, tmp_path, capsys):
    fake = FakeGh(*window_fixture())
    lines = run(monkeypatch, tmp_path, capsys, fake, [SINCE, UNTIL])
    # #103 is created before since, #104 on until (exclusive), #105 has no core-issue label
    for number in ("#103", "#104", "#105"):
        assert not any(line.startswith(number + " ") for line in lines)
    assert lines[-1] == "total: 1"


def test_flow_fix_added_later_says_yes(monkeypatch, tmp_path, capsys):
    issues = [
        # flow-fix labeled a day after creation (outside the 120 s window)
        issue(201, "2026-10-14T12:00:00Z", "Core: parser fix", ["core-issue", "flow-fix"]),
        # flow-fix only in the current labels
        issue(202, "2026-10-13T08:00:00Z", "Core: flow-fix mark added by label", ["core-issue", "flow-fix"]),
    ]
    timelines = {
        201: [labeled("core-issue", "2026-10-14T12:00:00Z"), labeled("flow-fix", "2026-10-15T09:30:00Z")],
        202: [labeled("core-issue", "2026-10-13T08:00:00Z")],
    }
    lines = run(monkeypatch, tmp_path, capsys, FakeGh(issues, timelines), [SINCE, UNTIL])
    assert lines == [
        "#201 2026-10-14T12:00:00Z Core: parser fix | flow-fix later: yes",
        "#202 2026-10-13T08:00:00Z Core: flow-fix mark added by label | flow-fix later: yes",
        "total: 2",
    ]


def test_empty_window_prints_total_zero(monkeypatch, tmp_path, capsys):
    fake = FakeGh(*window_fixture())
    lines = run(monkeypatch, tmp_path, capsys, fake, ["2026-10-20", "2026-10-21"])
    assert lines == ["total: 0"]
