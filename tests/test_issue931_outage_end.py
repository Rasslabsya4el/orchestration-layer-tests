"""#931 unit 2: a session whose transcript ends in an API error (an outage, a spent limit, an auth error)
is an `outage` ending, not a `no_checkpoint` one: no comment, no streak, the issue back in the pool
with its label, and no new session for 10 minutes. Fakes only: no heavy tool, no real gh or claude."""

from __future__ import annotations

import json
from pathlib import Path

from tools.agentops import dispatch as dispatch_mod
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _events, _issue
from tests._area_rule import _no_area_rule  # noqa: F401

#: the wait the ticket names (10 minutes), written out here so a red test shows the behaviour, not a name.
TEN_MINUTES = 600
MARK_ERROR = "oauth_org_not_allowed"


def _transcript(projects: Path, session_id: str, *, mark: bool) -> None:
    """The session's transcript (`<projects>/<sessionId>.jsonl`): a normal turn, then -- when `mark` --
    the API-error assistant record the measured Claude Code 2.x transcripts end with (#931 unit 1)."""

    projects.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "timestamp": "2026-10-12T10:00:00Z", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "timestamp": "2026-10-12T10:00:05Z",
         "message": {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "done"}]}},
    ]
    if mark:
        rows.append({"type": "assistant", "isApiErrorMessage": True, "error": MARK_ERROR,
                     "timestamp": "2026-10-12T10:01:00Z",
                     "message": {"model": "<synthetic>",
                                 "content": [{"type": "text", "text": "Your organization has disabled ..."}]}})
    (projects / f"uuid-{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _end_one(tmp_path: Path, d, gh: FakeGh, claude: FakeClaude, clock: FakeClock, issue: int,
             mark: bool | None) -> None:
    """One whole session on `issue`: started by a tick, the issue claimed, the transcript written
    (`mark` True: API error, False: normal turn, None: no transcript file), the session ends, and the
    clock moves past the outage wait so the next start is never blocked by it."""

    gh._candidates = [_issue(issue)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(issue)
    if mark is not None:
        _transcript(tmp_path / "projects", live_id, mark=mark)
    claude.set_state(live_id, state="done", status="idle")
    d.tick()
    clock.advance(TEN_MINUTES)


def _orch_outcomes(tmp_path: Path, issue: int) -> list[str]:
    return [e["outcome"] for e in _events(tmp_path) if e["kind"] == "orch_end" and e["issue"] == issue]


def _fresh(tmp_path: Path):
    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    logs: list[str] = []
    d.log = logs.append
    return d, gh, claude, clock, logs


def test_row1_three_outage_endings_in_a_row_leave_the_issue_ready_and_uncommented(tmp_path: Path):
    d, gh, claude, clock, logs = _fresh(tmp_path)

    for _ in range(3):
        _end_one(tmp_path, d, gh, claude, clock, 9, mark=True)

    assert gh.comments == []
    assert gh.added_labels == []
    assert gh.removed_labels == []
    assert "ready-for-agent" in gh.issues[9].labels
    assert _orch_outcomes(tmp_path, 9) == ["outage"] * 3
    assert len([m for m in logs if "ended by an API error" in m]) == 3


def test_row2_outage_endings_do_not_count_toward_the_needs_info_streak(tmp_path: Path):
    d, gh, claude, clock, logs = _fresh(tmp_path)
    # outage, no_checkpoint, outage, no_checkpoint, no_checkpoint
    sequence = [True, False, True, False, False]

    for i, mark in enumerate(sequence, start=1):
        _end_one(tmp_path, d, gh, claude, clock, 9, mark=mark)
        if i < 5:
            assert gh.added_labels == [], f"needs-info set too early at ending {i}"

    assert _orch_outcomes(tmp_path, 9) == ["outage", "no_checkpoint", "outage", "no_checkpoint", "no_checkpoint"]
    assert gh.added_labels == [(9, "needs-info")]
    assert gh.removed_labels == [(9, "ready-for-agent")]
    assert [n for n, _ in gh.comments] == [9, 9, 9]  # one alarm comment per no_checkpoint, none for outage


def test_row3_three_no_checkpoint_endings_without_the_mark_still_label_needs_info(tmp_path: Path):
    # a normal last assistant record, and a missing transcript file: both are no mark
    for label, mark in (("normal", False), ("missing", None)):
        sub = tmp_path / label
        sub.mkdir()
        d, gh, claude, clock, logs = _fresh(sub)

        for _ in range(3):
            _end_one(sub, d, gh, claude, clock, 9, mark=mark)

        assert _orch_outcomes(sub, 9) == ["no_checkpoint"] * 3, label
        assert gh.added_labels == [(9, "needs-info")], label
        assert gh.removed_labels == [(9, "ready-for-agent")], label
        assert len([m for m in logs if "ended by an API error" in m]) == 0, label


def test_row4a_after_an_outage_that_held_nothing_no_session_starts_for_ten_minutes(tmp_path: Path):
    d, gh, claude, clock, logs = _fresh(tmp_path)
    gh._candidates = [_issue(1)]
    d.tick()  # starts a session offered #1
    live_id = claude.list_sessions()[0]["id"]
    _transcript(tmp_path / "projects", live_id, mark=True)
    claude.set_state(live_id, state="done", status="idle")  # ends without claiming #1

    d.tick()
    assert [e["outcome"] for e in _events(tmp_path) if e["kind"] == "orch_end"] == ["nothing_claimed"]
    assert len(claude.started) == 1

    d.tick()  # clock 0: still inside the wait
    assert len(claude.started) == 1
    clock.advance(TEN_MINUTES - 1)
    d.tick()
    assert len(claude.started) == 1
    clock.advance(1)
    d.tick()  # 600 s passed: a new session starts
    assert len(claude.started) == 2


def test_row4b_after_an_outage_that_held_an_issue_no_session_starts_for_ten_minutes(tmp_path: Path):
    d, gh, claude, clock, logs = _fresh(tmp_path)
    gh._candidates = [_issue(9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    _transcript(tmp_path / "projects", live_id, mark=True)
    claude.set_state(live_id, state="done", status="idle")

    d.tick()
    assert _orch_outcomes(tmp_path, 9) == ["outage"]
    assert len(claude.started) == 1

    d.tick()
    assert len(claude.started) == 1
    clock.advance(TEN_MINUTES - 1)
    d.tick()
    assert len(claude.started) == 1
    clock.advance(1)
    d.tick()
    assert len(claude.started) == 2
    assert dispatch_mod.OUTAGE_WAIT_S == TEN_MINUTES


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _assistant(text: str, *, timestamp: str) -> dict:
    return {"type": "assistant", "timestamp": timestamp,
            "message": {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}]}}


def _api_error(error: str | None, *, timestamp: str) -> dict:
    rec = {"type": "assistant", "isApiErrorMessage": True, "timestamp": timestamp,
           "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Your organization ..."}]}}
    if error is not None:
        rec["error"] = error
    return rec


def test_row5_a_marked_tail_returns_the_error_string(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_rows(path, [_assistant("hi", timestamp="2026-10-12T10:00:00Z"),
                       _api_error(MARK_ERROR, timestamp="2026-10-12T10:01:00Z")])
    assert dispatch_mod.last_api_error(path) == MARK_ERROR


def test_row5_a_marked_record_without_an_error_field_reads_as_api_error(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_rows(path, [_api_error(None, timestamp="2026-10-12T10:01:00Z")])
    assert dispatch_mod.last_api_error(path) == "api_error"


def test_row5_a_normal_tail_returns_none(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_rows(path, [{"type": "user", "timestamp": "2026-10-12T10:00:00Z", "message": {"content": "go"}},
                       _assistant("done", timestamp="2026-10-12T10:00:05Z")])
    assert dispatch_mod.last_api_error(path) is None


def test_row5_a_missing_file_returns_none(tmp_path: Path):
    assert dispatch_mod.last_api_error(tmp_path / "absent.jsonl") is None


def test_row5_a_marked_record_followed_by_a_normal_assistant_record_returns_none(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_rows(path, [_api_error(MARK_ERROR, timestamp="2026-10-12T10:01:00Z"),
                       _assistant("back again", timestamp="2026-10-12T10:02:00Z")])
    assert dispatch_mod.last_api_error(path) is None
