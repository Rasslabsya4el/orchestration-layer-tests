"""#232 unit 4a: the github suite orchestrator, with fakes only (no network, no git push, no machine line)."""
from __future__ import annotations

import re

import pytest

from tools.agentops import github_suite as gs
from tools.agentops.test_copy_check import CheckError
from tools.agentops.test_copy_send import SendResult

COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}


class Fakes:
    def __init__(self, *, sent=True, poll=None, send_exc=None, dispatch_exc=None, poll_exc=None, record_exc=None,
                 machines=3, push_exc=None, acquire_exc=None):
        self.push_exc, self.acquire_exc = push_exc, acquire_exc
        self.sent, self.send_exc, self.dispatch_exc, self.poll_exc = sent, send_exc, dispatch_exc, poll_exc
        self.record_exc, self.machines = record_exc, machines
        self.poll_result = poll or {"run_id": 77, "parts": [{"part": 1, "seconds": 61}, {"part": 2, "seconds": 90}],
                                    "minutes_used": 5, "outcome": "green", "failed_ids": [], "full_recheck": False}
        self.calls: list[tuple] = []
        self.released = 0
        self.dispatch_kw, self.poll_kw = {}, {}
        self.records: list[dict] = []

    def send(self, root, branch, remote):
        self.calls.append(("send", root, branch, remote))
        if self.send_exc:
            raise self.send_exc
        return SendResult(self.sent, "abc" if self.sent else None)

    def acquire_machines(self, repo, run, want=None, **kw):
        self.calls.append(("acquire", repo, run, want, kw))
        if self.acquire_exc:
            raise self.acquire_exc
        fakes = self

        class Berth:
            machines = fakes.machines

            def release(self):
                fakes.released += 1

        return Berth()

    def dispatch(self, repo, branch, parts, *a, **kw):
        self.calls.append(("dispatch", repo, branch, parts))
        self.dispatch_kw = dict(kw)
        if self.dispatch_exc:
            raise self.dispatch_exc
        return 77

    def poll(self, repo, run_id, *a, **kw):
        self.calls.append(("poll", repo, run_id, kw.get("parts")))
        self.since = kw.get("since")
        self.poll_kw = dict(kw)
        if self.poll_exc:
            raise self.poll_exc
        return self.poll_result

    def push(self, root, branch, repo_slug):
        self.calls.append(("push", root, branch, repo_slug))
        if self.push_exc:
            raise self.push_exc

    def clock(self):
        self.calls.append(("clock",))
        return 1234.5

    def record(self, rec):
        self.records.append(rec)
        if self.record_exc:
            raise self.record_exc

    def deps(self):
        return gs.Deps(send=self.send, acquire_machines=self.acquire_machines, dispatch=self.dispatch,
                       poll=self.poll, record=self.record, push=self.push, clock=self.clock)

    def names(self):
        return [c[0] for c in self.calls]


def run(fakes, remote=COPY, branch="i232-u4a", own_repo=False, settings=SETTINGS):
    return gs.run_github_suite(root="C:/x/repo", branch=branch, repo_slug="me/repo", remote=remote,
                               repo_name="orch", priority=False, settings=settings, deps=fakes.deps(),
                               own_repo=own_repo)


def test_not_started_falls_back_with_exact_brain_line_and_releases():
    f = Fakes(poll={"run_id": 77, "parts": [], "minutes_used": 1, "outcome": "not-started", "failed_ids": [],
                    "full_recheck": False})
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "not-started")
    assert r.brain_line == "orch merge i232-u4a: GitHub not-started, suite on the PC"
    assert f.released == 1


def test_red_test_carries_the_ids():
    f = Fakes(poll={"run_id": 77, "parts": [], "minutes_used": 4, "outcome": "red-test",
                    "failed_ids": ["a", "b"], "full_recheck": False})
    r = run(f)
    assert (r.status, r.reason, r.failed_ids, r.full_recheck, r.brain_line) == ("red", "", ["a", "b"], False, "")


def test_red_test_full_recheck_passes_on():
    f = Fakes(poll={"run_id": 77, "parts": [], "minutes_used": 4, "outcome": "red-test",
                    "failed_ids": [], "full_recheck": True})
    r = run(f)
    assert r.status == "red" and r.full_recheck is True and r.failed_ids == []


def test_lost_is_fallback_not_red():
    f = Fakes(poll={"run_id": 77, "parts": [], "minutes_used": 2, "outcome": "lost", "failed_ids": [],
                    "full_recheck": False})
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "lost")
    assert r.brain_line == "orch merge i232-u4a: GitHub lost, suite on the PC"


def test_green_reports_machines_part_seconds_minutes_and_runs_against_the_copy():
    f = Fakes(machines=2)
    r = run(f)
    assert (r.status, r.reason, "GitHub", r.machines, r.part_seconds, r.minutes_used, r.run_id) == (
        "green", "", "GitHub", 2, [61, 90], 5, 77)
    assert r.brain_line == "" and r.failed_ids == [] and r.full_recheck is False
    acquire = next(c for c in f.calls if c[0] == "acquire")
    assert acquire[3] == 5
    assert ("dispatch", "owner/copy-repo", "i232-u4a", 2) in f.calls
    assert ("poll", "owner/copy-repo", 77, 2) in f.calls
    assert f.released == 1


def test_send_refusal_falls_back_and_never_dispatches():
    f = Fakes(sent=False)
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "refused")
    assert r.brain_line == "orch merge i232-u4a: GitHub refused, suite on the PC"
    assert f.names() == ["send"] and f.released == 0


@pytest.mark.parametrize("exc", [CheckError("scan"), RuntimeError("boom")])
def test_send_exception_is_send_failed(exc):
    f = Fakes(send_exc=exc)
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "send-failed")
    assert "acquire" not in f.names()


def test_dispatch_exception_falls_back_and_releases_once():
    f = Fakes(dispatch_exc=RuntimeError("no run"))
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "dispatch-failed")
    assert "poll" not in f.names() and f.released == 1


def test_poll_exception_falls_back_and_releases_once():
    f = Fakes(poll_exc=RuntimeError("gh dead"))
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "poll-failed")
    assert f.released == 1


def test_record_called_once_with_the_fields():
    f = Fakes()
    r = run(f)
    assert len(f.records) == 1
    rec = f.records[0]
    assert rec["repo"] == "orch" and rec["slug"] == "me/repo" and rec["minutes"] == 5
    assert rec["machines"] == 3 and rec["outcome"] == "green" and rec["run_id"] == 77
    assert re.fullmatch(r"\d{4}-\d{2}", rec["month"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", rec["time"])
    assert r.status == "green"


def test_record_outcome_is_the_reason_of_a_fallback_and_is_recorded_on_refusal_too():
    f = Fakes(sent=False)
    run(f)
    assert len(f.records) == 1 and f.records[0]["outcome"] == "refused" and f.records[0]["minutes"] == 0


def test_record_failure_is_swallowed():
    f = Fakes(record_exc=OSError("disk"))
    assert run(f).status == "green"


def test_next_call_after_a_fallback_asks_github_again():
    f = Fakes(poll={"run_id": 77, "parts": [], "minutes_used": 1, "outcome": "not-started", "failed_ids": [],
                    "full_recheck": False})
    run(f)
    f.poll_result = {"run_id": 78, "parts": [{"part": 1, "seconds": 5}], "minutes_used": 1, "outcome": "green",
                     "failed_ids": [], "full_recheck": False}
    assert run(f).status == "green"
    assert f.names().count("dispatch") == 2 and f.released == 2


def test_bare_slug_remote_is_accepted_and_empty_remote_raises():
    f = Fakes()
    run(f, remote="me/repo")
    assert ("dispatch", "me/repo", "i232-u4a", 3) in f.calls
    with pytest.raises(ValueError):
        run(Fakes(), remote="")


def test_brain_line_stays_within_160_chars():
    f = Fakes(sent=False)
    r = run(f, branch="b" * 300)
    assert len(r.brain_line) <= 160 and r.brain_line.endswith(": GitHub refused, suite on the PC")


def test_own_repo_never_sends_pushes_once_and_runs_on_its_own_slug():
    f = Fakes()
    r = run(f, remote="", own_repo=True)
    assert r.status == "green"
    assert "send" not in f.names() and f.names().count("push") == 1
    assert ("push", "C:/x/repo", "i232-u4a", "me/repo") in f.calls
    assert ("dispatch", "me/repo", "i232-u4a", 3) in f.calls and ("poll", "me/repo", 77, 3) in f.calls
    assert f.records[0]["own_runner"] is True and f.released == 1


def test_own_repo_push_failure_is_send_failed():
    f = Fakes(push_exc=RuntimeError("rejected"))
    r = run(f, remote="", own_repo=True)
    assert (r.status, r.reason) == ("fallback", "send-failed")
    assert "acquire" not in f.names() and f.records[0]["own_runner"] is True


def test_copy_run_records_own_runner_false_and_never_pushes():
    f = Fakes()
    run(f)
    assert f.records[0]["own_runner"] is False and "push" not in f.names()


def test_acquire_failure_is_no_places_with_nothing_to_release():
    f = Fakes(acquire_exc=RuntimeError("line broken"))
    r = run(f)
    assert (r.status, r.reason) == ("fallback", "no-places")
    assert r.brain_line == "orch merge i232-u4a: GitHub no-places, suite on the PC"
    assert "dispatch" not in f.names() and f.released == 0


def test_missing_settings_keys_use_the_defaults():
    f = Fakes()
    run(f, settings={})
    acquire = next(c for c in f.calls if c[0] == "acquire")
    assert acquire[3] == 5 and acquire[4]["total"] == 20 and acquire[4]["per_merge"] == 5


def test_poll_counts_the_start_bound_from_just_before_the_dispatch():
    f = Fakes()
    run(f)
    assert f.since == 1234.5
    names = f.names()
    assert names.index("clock") < names.index("dispatch") < names.index("poll")


def test_failure_after_dispatch_records_minutes_from_the_exception_or_zero():
    f = Fakes(poll_exc=RuntimeError("gh dead"))
    r = run(f)
    assert r.minutes_used == 0 and r.run_id == 77 and f.records[0]["minutes"] == 0
    err = RuntimeError("gh dead")
    err.minutes_used = 3
    f = Fakes(poll_exc=err)
    assert run(f).minutes_used == 3 and f.records[0]["minutes"] == 3


def test_result_has_no_place_field():
    assert not hasattr(run(Fakes()), "place")
