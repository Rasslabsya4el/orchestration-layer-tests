"""#1054: `python -m tools.agentops finishing` posts ONE parking note on every open issue the session still
holds whose newest own comment does not park or close, so the dispatcher's end path finds a parking checkpoint
instead of the "ended without a closing or parking checkpoint" alarm. Replays case A (this repo, session
b6fb9a50 held #910: progress checkpoint, no parking word in its headline) and case B (Other, session f255b442
held #356: checkpoint signed with another id). Fake `gh` / `claude.exe` / clock only, the real
`GhCli.own_checkpoint_parks` underneath; no real `gh`, no real dispatcher."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import ALARM_COMMENT_TEXT, GhCli, _parks_or_closes, run_finishing_command

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue
from tests.test_agentops_dispatch_core_issue import FakeCoreGh, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)

CASE_A = ("ORCH (<id>), #910 checkpoint 3: units 2 and 3 done and merged on main, the suite is green; "
          "unit 4 is next.")
CASE_B = ("**Checkpoint (ORCH a89d26) — unit 1 merge red-suite (5 min): the merge went red on the suite; "
          "next: rebuild on main… re-queue merge**")
PARKED = ("<!-- parked-on-run: merge of 910-c -->\n"
          "Checkpoint 4 (ORCH <id>, parked `needs-info`): waits on the brain's answer.")


class ThreadGh(FakeGh):
    """`FakeGh` whose comment thread is real: the real `GhCli.own_checkpoint_parks` reads it, `comment` appends
    to it. `fail` makes every `gh` read and write fail."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: dict[int, list[tuple[float, str]]] = {}
        self.now_min = 20.0
        self.fail = False

    def _cli(self, number: int) -> GhCli:
        def runner(args, stdin=None):
            if self.fail:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="gh down")
            out = json.dumps({"comments": [
                {"body": b, "createdAt": (T0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")}
                for m, b in self.thread.get(number, [])]})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        return GhCli(runner=runner)

    def own_checkpoint_parks(self, number, session_ids=(), held_from="", held_until="", other_ids=None):
        return self._cli(number).own_checkpoint_parks(number, session_ids, held_from, held_until, other_ids)

    def issue_status(self, number: int):
        if self.fail:
            raise RuntimeError("gh issue view failed")
        return super().issue_status(number)

    def comment(self, number: int, body: str) -> bool:
        if self.fail:
            return False
        self.thread.setdefault(number, []).append((self.now_min, body))
        return super().comment(number, body)


class Rig:
    """The session starts at T0, claims `number` at T0+1 min; `thread` is (minutes after T0, body)."""

    def __init__(self, tmp_path: Path, number: int, thread: list[tuple[float, str]]) -> None:
        self.tmp_path, self.number = tmp_path, number
        now = [T0]
        self.claude, self.gh, self.core_gh = FakeClaude(), ThreadGh(), FakeCoreGh()
        self.d = _dispatcher(tmp_path, claude=self.claude, gh=self.gh, core_gh=self.core_gh)
        self.d.timestamp = lambda: now[0]
        self.now = now
        self.gh._candidates = [_issue(number, 9)]
        self.d.tick()
        self.sid = self.claude.list_sessions()[0]["id"]
        self.gh.claim(number)
        now[0] = T0 + timedelta(minutes=1)
        self.d.tick()  # writes the state file: the session holds `number`
        self.gh.thread[number] = [(m, b.replace("<id>", self.sid)) for m, b in thread]
        self.logs: list[str] = []

    def finishing(self, session: str | None = None) -> str:
        return run_finishing_command(session or self.sid, finishing_dir=self.tmp_path / "finishing",
                                     log=self.logs.append, gh=self.gh,
                                     state_file=self.tmp_path / "orch_session.json",
                                     events_file=self.tmp_path / "events.jsonl")

    def notes(self) -> list[tuple[int, str]]:
        return [(n, b) for n, b in self.gh.comments if "finishing: still working" in b]

    def end(self) -> str:
        """The session's end: the dispatcher's own end path; returns the issue's outcome."""
        self.now[0] = T0 + timedelta(minutes=30)
        self.claude.set_state(self.sid, state="done", status="idle")
        return self.d.tick().outcomes[self.number]

    def alarms(self) -> list[tuple[int, str]]:
        return [(n, b) for n, b in self.gh.comments if ALARM_COMMENT_TEXT in b]


def test_the_note_text_passes_the_headline_check_and_names_the_session():
    from tools.agentops.dispatch import finishing_note

    note = finishing_note("b6fb9a50", ("b6fb9a50", "b6fb9a50-1111-2222-3333-444455556666"))
    lines = note.splitlines()
    assert lines[0] == ("ORCH b6fb9a50, finishing: still working this issue, takes nothing new; "
                        "if the session ends, the next one continues from the last checkpoint above")
    assert lines[1].startswith("<!-- parked-on-run: this session is finishing;")
    assert "b6fb9a50-1111-2222-3333-444455556666" in lines[2]
    assert _parks_or_closes(note)


def test_a_case_a_progress_checkpoint_gets_one_note_and_the_end_raises_no_alarm(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    # before the fix: the end path alarms on exactly this thread
    assert rig.gh.own_checkpoint_parks(910, (rig.sid,), "2026-10-12T08:01:00Z", "", ()) is False

    rig.finishing()

    assert [n for n, _ in rig.notes()] == [910]
    assert (tmp_path / "finishing" / f"{rig.sid}.json").is_file()
    assert rig.end() == "released"
    assert rig.alarms() == [] and rig.core_gh.calls == []


def test_a_case_b_checkpoint_signed_with_another_id_is_covered_through_the_holding_window(tmp_path):
    rig = Rig(tmp_path, 356, [(5, CASE_B)])

    rig.finishing()

    assert [n for n, _ in rig.notes()] == [356]
    assert rig.end() == "released"
    assert rig.alarms() == []


def test_an_issue_whose_newest_own_comment_already_parks_gets_no_note(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A), (6, PARKED)])
    rig.finishing()
    assert rig.notes() == [] and rig.gh.comments == []

    (tmp_path / "b").mkdir()
    rig = Rig(tmp_path / "b", 911, [(5, "Checkpoint 2 (ORCH, parked `needs-info`): waits on the brain")])
    rig.finishing()
    assert rig.gh.comments == []


def test_a_closed_issue_gets_no_note(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    rig.gh.close(910)
    rig.finishing()
    assert rig.gh.comments == []


def test_running_finishing_twice_posts_once_per_issue(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    rig.finishing()
    rig.finishing()
    assert [n for n, _ in rig.notes()] == [910]


def test_a_later_closing_comment_of_the_session_supersedes_the_note(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    rig.finishing()
    rig.gh.thread[910].append((25, "ORCH <id>: closing #910, merged.".replace("<id>", rig.sid)))
    assert rig.gh.own_checkpoint_parks(910, (rig.sid,), "2026-10-12T08:01:00Z", "", ()) is True
    rig.finishing()
    assert [n for n, _ in rig.notes()] == [910]


def test_a_failing_gh_is_no_note_one_log_line_and_the_mark_exists(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    rig.gh.fail = True

    sid = rig.finishing()

    assert sid == rig.sid and rig.gh.comments == []
    assert (tmp_path / "finishing" / f"{rig.sid}.json").is_file()
    assert len([l for l in rig.logs if "finishing:" in l and "#910" in l]) == 1


def test_the_note_changes_no_assignee_and_no_label(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    before = rig.gh.issues[910]

    rig.finishing()

    assert [n for n, _ in rig.notes()] == [910]
    assert rig.gh.removed_assignees == [] and rig.gh.added_labels == [] and rig.gh.removed_labels == []
    assert rig.gh.issues[910] == before


def test_a_missing_or_garbled_state_file_is_no_held_issues(tmp_path):
    gh = ThreadGh()
    kwargs = dict(finishing_dir=tmp_path / "finishing", log=lambda m: None, gh=gh,
                  events_file=tmp_path / "events.jsonl")
    run_finishing_command("abc12345", state_file=tmp_path / "nope.json", **kwargs)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    run_finishing_command("abc12345", state_file=tmp_path / "bad.json", **kwargs)
    assert gh.comments == [] and (tmp_path / "finishing" / "abc12345.json").is_file()
