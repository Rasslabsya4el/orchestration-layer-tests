"""#852 row 6: the seven real ends replayed. Each thread is the real comment list of the issue (createdAt,
body cut to 600 chars, the dispatcher's alarm and everything after it left out) from
`tests/fixtures/issue852_threads.json`, with the session's real claim and end times; it goes through the real
`GhCli.own_checkpoint_parks` and the dispatcher's end path."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agentops.dispatch import GhCli
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events, _issue
from tests.test_agentops_dispatch_core_issue import FakeCoreGh, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "issue852_threads.json").read_text(encoding="utf-8"))


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class RealThreadGh(FakeGh):
    def __init__(self, comments, session) -> None:
        super().__init__()
        self._comments, self._session = comments, session
        self.live_id = ""

    def own_checkpoint_parks(self, number, session_ids=(), held_from="", held_until="", other_ids=None):
        body = json.dumps({"comments": [
            {"createdAt": c["createdAt"], "body": c["body"].replace(self._session, self.live_id)}
            for c in self._comments]})
        runner = lambda args, stdin=None: subprocess.CompletedProcess(args=args, returncode=0, stdout=body, stderr="")
        return GhCli(runner=runner).own_checkpoint_parks(number, session_ids, held_from, held_until, other_ids)


def _replay(tmp_path, number: int):
    data = FIXTURE[str(number)]
    claimed, ended = _t(data["claimed"]), _t(data["ended"])
    now = [claimed - timedelta(minutes=1)]
    claude, gh, core_gh = FakeClaude(), RealThreadGh(data["comments"], data["session"]), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)
    d.timestamp = lambda: now[0]
    d.boot_time = lambda: None  # no machine restart: the replayed dates are not the real clock
    gh._candidates = [_issue(9, 9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.live_id = live_id
    gh.claim(9)
    now[0] = claimed
    d.tick()
    now[0] = ended
    claude.set_state(live_id, state="done", status="idle")
    outcome = d.tick().outcomes[9]
    return outcome, gh, core_gh, d


def test_the_seven_threads_are_read_from_the_json_file():
    assert sorted(FIXTURE, key=int) == ["736", "747", "768", "772", "775", "816", "821"]
    assert all(FIXTURE[k]["comments"] for k in FIXTURE)


@pytest.mark.parametrize("number", [747, 816, 821])
def test_own_parking_note_then_others_is_released(tmp_path, number):
    """#747 own `finishing` note then machine/keymaster/brain comments; #816 own checkpoint 5 (`needs-info`)
    then the brain's answer; #821 own checkpoint 2 (`parked`) then the brain answer and ORCH 5deb...'s note."""
    outcome, gh, core_gh, d = _replay(tmp_path, number)
    assert outcome == "released"
    assert gh.comments == [] and core_gh.calls == []
    assert d._consecutive_no_checkpoint(9) == 0
    assert [(e["issue"], e["outcome"]) for e in _events(tmp_path) if e["kind"] == "orch_end"] == [(9, "released")]


@pytest.mark.parametrize("number,reason", [
    (736, "claim note only, no parking words, session dropped mid-work"),
    (768, "claim/progress note of the 5.5 session, no parking words (older parking was another session's, before the claim)"),
    (772, "progress note 'orch-772 made ... unit 3 waits', no parking words, session dropped mid-work"),
    (775, "claim note, no parking words, session dropped mid-work"),
])
def test_claim_cut_or_progress_note_only_is_no_checkpoint(tmp_path, number, reason):
    outcome, gh, core_gh, d = _replay(tmp_path, number)
    assert outcome == "no_checkpoint", reason
    assert [n for n, _ in gh.comments] == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)
