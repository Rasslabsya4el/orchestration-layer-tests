"""#949: the outcomes report reads only the Claude transcripts that can hold a turn in its window,
and parses each main transcript once. Transcripts are built in tmp_path, in the repo's jsonl shape."""

from __future__ import annotations

import json
import os
import threading
from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.agentops import outcomes as oc
from tools.agentops import outcomes_report as rp
from tools.agentops import outcomes_transcripts as ot

tm = oc._token_meter()

SINCE_ARG = "2026-10-01T00:00:00+00:00"
UNTIL_ARG = "2026-10-08T00:00:00+00:00"
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 8, tzinfo=timezone.utc)
OLD_MTIME = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()  # before SINCE
IN_EPOCH = int(datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _no_gh(monkeypatch: pytest.MonkeyPatch):
    """#1068: with no --labels the report reads the issue list through gh; these tests check the transcript
    sections only, so the read is an empty list (the same {} the expected text is built from)."""
    monkeypatch.setattr(rp, "read_issue_list", lambda: ({}, None))


def _assistant(ts: str, mid: str, *, model: str = "claude-sonnet-5-5", skill_id: str | None = None) -> dict:
    content = [{"type": "tool_use", "id": skill_id, "name": "Skill", "input": {}}] if skill_id else []
    message = {"id": mid, "model": model, "usage": {"input_tokens": 10, "output_tokens": 1}, "content": content}
    return {"type": "assistant", "timestamp": ts, "message": message}


def _write(path: Path, objs: list[dict], mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _sub_meta(path: Path, agent_type: str) -> None:
    path.with_name(path.stem + ".meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")


class World:
    def __init__(self, root: Path):
        self.projects = root / "projects"
        self.log = root / "agentops_outcomes.jsonl"
        self.old_main = self.projects / "orch-old.jsonl"
        self.old_sub = self.projects / "oldsess" / "subagents" / "agent-b.jsonl"


def _world(root: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(root)
    # ORCH session with turns in the window; a streamed duplicate of one Skill block must count once
    _write(w.projects / "orch-in.jsonl", [
        {"type": "custom-title", "customTitle": "orch #949 #950"},
        {"type": "user", "timestamp": "2026-10-03T09:00:00Z", "message": {"role": "user", "content": "go #949"}},
        _assistant("2026-10-03T10:00:00Z", "m1", skill_id="s1"),
        _assistant("2026-10-03T10:00:00Z", "m1", skill_id="s1"),
        _assistant("2026-10-03T10:05:00Z", "m2", skill_id="s2"),
    ])
    # a plain worker-like session in the window
    _write(w.projects / "plain-in.jsonl", [
        {"type": "user", "timestamp": "2026-10-04T09:00:00Z", "message": {"role": "user", "content": "hi"}},
        _assistant("2026-10-04T10:00:00Z", "p1"),
    ])
    # an ORCH session last written before the window: never opened
    _write(w.old_main, [
        {"type": "custom-title", "customTitle": "orch #9"},
        _assistant("2026-08-20T10:00:00Z", "o1", skill_id="s9"),
    ], mtime=OLD_MTIME)
    # a subagent in the window, and one last written before it
    sub_in = w.projects / "sess" / "subagents" / "agent-a.jsonl"
    _write(sub_in, [_assistant("2026-10-03T11:00:00Z", "a1")])
    _sub_meta(sub_in, "worker")
    _write(w.old_sub, [_assistant("2026-08-20T11:00:00Z", "b1")], mtime=OLD_MTIME)
    _sub_meta(w.old_sub, "worker")
    # one outcome row in the window (no session field: the report reads no transcript for it)
    w.log.write_text(json.dumps({"ts": IN_EPOCH, "issue": 949, "route": "worker/sonnet-5.5",
                                 "class": "accepted"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(oc, "DEFAULT_OUTCOMES_LOG", w.log)
    monkeypatch.setattr(tm, "DEFAULT_DISPATCH_OUT", root / "none.out")
    monkeypatch.setattr(tm, "DEFAULT_ORCH_SESSION_JSON", root / "none.json")
    return w


def _record(monkeypatch: pytest.MonkeyPatch) -> dict:
    rec = {"opened": [], "parsed": Counter()}
    orig_read = tm._read_jsonl
    orig_parse = tm.parse_main_transcript

    def read(path, *a, **kw):
        rec["opened"].append(Path(path))
        return orig_read(path, *a, **kw)

    def parse(path, *a, **kw):
        rec["parsed"][Path(path)] += 1
        return orig_parse(path, *a, **kw)

    monkeypatch.setattr(tm, "_read_jsonl", read)
    monkeypatch.setattr(tm, "parse_main_transcript", parse)
    return rec


def _run_report(monkeypatch: pytest.MonkeyPatch, capsys, w: World, *, filter_on: bool) -> tuple[str, dict]:
    with monkeypatch.context() as mp:
        rec = _record(mp)
        if not filter_on:  # the old path: every transcript is considered, no mtime filter
            orig_main, orig_sub = tm.discover_main_transcripts, tm.discover_subagent_transcripts
            mp.setattr(tm, "discover_main_transcripts", lambda d, **kw: orig_main(d))
            mp.setattr(tm, "discover_subagent_transcripts", lambda d, **kw: orig_sub(d))
        args = Namespace(since=SINCE_ARG, until=UNTIL_ARG, labels=None, projects_dir=w.projects, max_seconds=0)
        assert rp.run_report(args) == 0
        text = capsys.readouterr().out
    return text, rec


def test_report_text_identical_and_old_files_not_opened(tmp_path: Path, monkeypatch, capsys):
    w = _world(tmp_path, monkeypatch)
    old_text, old = _run_report(monkeypatch, capsys, w, filter_on=False)
    new_text, new = _run_report(monkeypatch, capsys, w, filter_on=True)

    assert "Routes" in new_text and "worker/sonnet-5.5" in new_text
    assert new_text == old_text
    old_files, new_files = set(old["opened"]), set(new["opened"])
    assert len(new_files) < len(old_files), (len(new_files), len(old_files))
    assert w.old_main not in new_files
    assert w.old_sub not in new_files
    assert all(n == 1 for n in new["parsed"].values()), new["parsed"]


def test_skill_step_takes_the_scan_result(tmp_path: Path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    scanned = tm.scan_projects_dir(w.projects, dispatch_offers={}, orch_session_held={}, since=SINCE, until=UNTIL)
    assert ot.skill_calls_per_orch_session(projects_dir=w.projects, since=SINCE, until=UNTIL,
                                           scanned=scanned) == {"orch-in": 2}
    assert ot.skill_calls_per_orch_session(projects_dir=w.projects, since=SINCE, until=UNTIL) == {"orch-in": 2}
    assert w.old_main not in scanned.main_transcripts


# ---- unit 2 of 2 (#949): never silent -- sections printed as ready, a stderr line per step, a total bound

STEP_NAMES = ["routes", "scan", "skills"]


def _step_names(err: str) -> list[str]:
    return [line.split()[3] for line in err.splitlines() if line.startswith("outcomes report: step ")]


def _old_combined_text(w: World) -> str:
    """The text the report printed before unit 2: the Routes section, then the three tables."""
    prices = tm.load_prices(tm.DEFAULT_PRICES_PATH)
    log = oc.load_jsonl(oc.DEFAULT_OUTCOMES_LOG)
    window = [o for o in log if isinstance(o.get("ts"), (int, float))
              and SINCE.timestamp() <= o["ts"] < UNTIL.timestamp()]
    rows = oc.routes(outcomes=window, projects_dir=w.projects, prices=prices)
    drift = ot.drift_by_agent_type(projects_dir=w.projects, since=SINCE, until=UNTIL)
    scanned = tm.scan_projects_dir(
        w.projects, dispatch_offers=tm.load_dispatch_offers(tm.DEFAULT_DISPATCH_OUT),
        orch_session_held=tm.load_orch_session_held(tm.DEFAULT_ORCH_SESSION_JSON), since=SINCE, until=UNTIL)
    meter, totals = rp.meter_numbers(scanned.turns, prices, SINCE, UNTIL)
    skills = ot.skill_calls_per_orch_session(projects_dir=w.projects, since=SINCE, until=UNTIL, scanned=scanned)
    return ("Routes\n" + rp.format_routes_with_drift(rows, drift) + "\n" + "\n"
            + rp.build_report(outcomes=log, labels={}, meter=meter, totals=totals,
                              skill_counts=skills, since=SINCE, until=UNTIL) + "\n")


def test_no_expiry_same_text_rc0_and_step_lines_in_order(tmp_path: Path, monkeypatch, capsys):
    w = _world(tmp_path, monkeypatch)
    expected = _old_combined_text(w)
    capsys.readouterr()  # drop the reference run's output
    args = Namespace(since=SINCE_ARG, until=UNTIL_ARG, labels=None, projects_dir=w.projects, max_seconds=50)
    assert rp.run_report(args) == 0
    cap = capsys.readouterr()
    assert cap.out == expected
    assert _step_names(cap.err) == STEP_NAMES, cap.err


def test_out_of_time_keeps_printed_sections_names_the_step_and_exits_2(tmp_path: Path, monkeypatch, capsys):
    w = _world(tmp_path, monkeypatch)
    release = threading.Event()

    def slow_scan(*a, **kw):  # stands in for the 53 s scan; the bound passes while it waits
        release.wait(5)
        raise RuntimeError("abandoned after the test")

    monkeypatch.setattr(tm, "scan_projects_dir", slow_scan)
    args = Namespace(since=SINCE_ARG, until=UNTIL_ARG, labels=None, projects_dir=w.projects, max_seconds=0.3)
    try:
        rc = rp.run_report(args)
        cap = capsys.readouterr()
    finally:
        release.set()
    assert rc == 2
    assert "Routes" in cap.out and "worker/sonnet-5.5" in cap.out  # the section done before the bound stays
    assert "Per issue" not in cap.out and "Pilot" not in cap.out  # nothing after the bound is printed
    assert _step_names(cap.err) == ["routes", "scan"], cap.err
    out_lines = [line for line in cap.err.splitlines() if line.startswith("outcomes report: out of time")]
    assert out_lines == ["outcomes report: out of time (0.3 s) in step scan; partial output above"], cap.err
