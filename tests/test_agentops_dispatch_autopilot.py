"""tools/agentops/autopilot.py: the autopilot slot beside the one ORCH (issue #116, D108), a local
module joined to the core dispatcher since #150 (ADR 0109 point 4). Fake `claude.exe` and `gh`
layers, a fake clock -- no real `claude --bg` or `gh` call, and ORCH's own behaviour (tested in
test_agentops_dispatch.py) is never touched here. Test names carry the issue's "Numbers" item they
reproduce where one applies."""

from __future__ import annotations

import pytest

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.autopilot import (
    AUTOPILOT_CHECKPOINT_PREFIX,
    AUTOPILOT_DEADLINE_GRACE_MINUTES,
    AUTOPILOT_IDLE_BACKSTOP_MINUTES,
    AUTOPILOT_STUCK_LABEL,
    AUTOPILOT_TERMS_PREFIX,
    MAX_AUTOPILOT_RESTARTS,
    WORKSPACE_NOT_TRUSTED,
    build_autopilot_command,
    build_autopilot_prompt,
    parse_autopilot_terms,
)
from tools.agentops.dispatch import (
    Dispatcher,
    TICK_SECONDS,
    build_wake_command,
    build_wake_prompt,
)
from tools.agentops.worktree import ROOT
from tests._area_rule import _no_area_rule  # noqa: F401


class FakeClaude:
    """A fake `claude.exe`: `start`/`start_autopilot` hand out sequential session ids,
    `list_sessions` reports whatever state the test sets."""

    def __init__(self) -> None:
        self.cwd = ROOT
        self.started: list[list[int]] = []
        self.autopilot_started: list[tuple[int, list[str]]] = []
        self.stopped: list[str] = []
        self.ended_pids: list[int] = []
        self.start_autopilot_fails: dict[int, str] = {}
        self._sessions: dict[str, dict] = {}
        self._next_id = 1
        #: wake(chat, message) calls, in order; `wake_results[chat] = (success, raw_text)` overrides
        #: the default success reply.
        self.wake_calls: list[tuple[str, str]] = []
        self.wake_results: dict[str, tuple[bool, str]] = {}

    def start(self, offered: list[int]) -> tuple[str, list[str]]:
        session_id = f"orch-{self._next_id}"
        self._next_id += 1
        self.started.append(list(offered))
        self._sessions[session_id] = {"id": session_id, "kind": "background",
                                      "state": "working", "status": "busy", "name": "orch"}
        return session_id, ["claude.exe", "--bg"]

    def start_autopilot(self, goal: int) -> tuple[str, list[str]]:
        if goal in self.start_autopilot_fails:
            raise RuntimeError(self.start_autopilot_fails[goal])
        session_id = f"ap-{self._next_id}"
        self._next_id += 1
        cmd = build_autopilot_command(goal, "claude.exe")
        self.autopilot_started.append((goal, cmd))
        self._sessions[session_id] = {"id": session_id, "kind": "background", "state": "working",
                                      "status": "busy", "name": f"autopilot #{goal}"}
        return session_id, cmd

    def list_sessions(self) -> list[dict]:
        return list(self._sessions.values())

    def stop(self, session_id: str) -> bool:
        self.stopped.append(session_id)
        self._sessions.pop(session_id, None)
        return True

    def end_process(self, pid: int) -> bool:
        self.ended_pids.append(pid)
        return True

    def wake(self, chat: str, message: str) -> tuple[bool, str]:
        self.wake_calls.append((chat, message))
        return self.wake_results.get(chat, (True, '{"success":true,"message":"queued"}'))

    # test helpers
    def set_state(self, session_id: str, *, state: str, status: str = "idle") -> None:
        self._sessions[session_id].update(state=state, status=status)

    def add_session(self, session_id: str, **fields) -> None:
        self._sessions[session_id] = {"id": session_id, "kind": "background", **fields}


class FakeGh:
    """A fake `gh`: no candidates for ORCH (never offered here) plus the autopilot surface --
    `autopilot_goals`, `latest_checkpoint_comment`, `add_label`, `comment`."""

    def __init__(self) -> None:
        self.goals: dict[int, set[str]] = {}  # goal number -> label set (open only while present)
        self.checkpoints: dict[int, list[str]] = {}
        self.terms: dict[int, list[str]] = {}
        self.comment_calls: list[tuple[int, str]] = []
        self.added_labels: list[tuple[int, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        self.removed_assignees: list[int] = []
        #: #146: arbitrary comments on a goal issue, `{number: [{"id": ..., "body": ...}, ...]}`
        #: (oldest first) -- `checkpoints`/`terms` above stay a separate, older concept.
        self._goal_comments: dict[int, list[dict]] = {}
        self._next_comment_id = 1
        #: #146: `"Part of #{goal}"` child issues, `{goal: {number: {"state", "labels", "comments"}}}`.
        self._children: dict[int, dict[int, dict]] = {}
        #: `None` -> the next `comments`/`child_issues` call for this number/goal fails once (#146
        #: "a `gh` failure never itself counts as a change").
        self.comments_fail: set[int] = set()
        self.children_fail: set[int] = set()

    # ORCH surface: never exercised by these tests, kept minimal
    def ready_for_agent_issues(self):
        return []

    def claimed_issues(self):
        return []

    # autopilot surface
    def autopilot_goal(self, number: int, labels: tuple[str, ...] = ("autopilot",)) -> None:
        self.goals[number] = set(labels)

    def close_goal(self, number: int) -> None:
        self.goals.pop(number, None)

    def remove_autopilot_label(self, number: int) -> None:
        self.goals.get(number, set()).discard("autopilot")

    def autopilot_goals(self) -> list[int]:
        return sorted(n for n, labels in self.goals.items()
                      if "autopilot" in labels and AUTOPILOT_STUCK_LABEL not in labels)

    def set_checkpoints(self, number: int, bodies: list[str]) -> None:
        self.checkpoints[number] = list(bodies)

    def latest_checkpoint_comment(self, number: int) -> str | None:
        bodies = self.checkpoints.get(number) or []
        for body in reversed(bodies):
            if body.startswith("Autopilot checkpoint"):
                return body
        return None

    def set_terms(self, number: int, bodies: list[str]) -> None:
        """`bodies`: full `Autopilot terms` comment bodies, oldest first (#127)."""

        self.terms[number] = list(bodies)

    def latest_terms_comment(self, number: int) -> str | None:
        bodies = self.terms.get(number) or []
        for body in reversed(bodies):
            if body.startswith("Autopilot terms"):
                return body
        return None

    def add_label(self, number: int, label: str) -> bool:
        self.added_labels.append((number, label))
        self.goals.setdefault(number, set()).add(label)
        return True

    def remove_label(self, number: int, label: str) -> bool:
        self.removed_labels.append((number, label))
        self.goals.get(number, set()).discard(label)
        return True

    def comment(self, number: int, body: str) -> bool:
        self.comment_calls.append((number, body))
        return True

    def remove_assignee(self, number: int, login: str = "@me") -> bool:
        self.removed_assignees.append(number)
        return True

    def issue_status(self, number: int):  # never called with no ORCH issue held
        raise AssertionError("issue_status should not be called by these autopilot-only tests")

    # -- #146: the goal's own comments and its "Part of #N" children ------------------------------

    def add_goal_comment(self, number: int, body: str) -> None:
        cid = f"c{self._next_comment_id}"
        self._next_comment_id += 1
        self._goal_comments.setdefault(number, []).append({"id": cid, "body": body})

    def comments(self, number: int, *, fresh: bool = False) -> list[dict] | None:  # #1113: GhCli.comments' flag
        if number in self.comments_fail:
            self.comments_fail.discard(number)
            return None
        return list(self._goal_comments.get(number, []))

    def add_child(self, goal: int, number: int, *, state: str = "OPEN",
                 labels: tuple[str, ...] = ()) -> None:
        self._children.setdefault(goal, {})[number] = {
            "state": state, "labels": list(labels), "comments": [],
        }

    def close_child(self, goal: int, number: int) -> None:
        self._children[goal][number]["state"] = "CLOSED"

    def reopen_child(self, goal: int, number: int) -> None:
        self._children[goal][number]["state"] = "OPEN"

    def add_label_to_child(self, goal: int, number: int, label: str) -> None:
        self._children[goal][number]["labels"].append(label)

    def remove_label_from_child(self, goal: int, number: int, label: str) -> None:
        self._children[goal][number]["labels"].remove(label)

    def add_comment_to_child(self, goal: int, number: int, body: str = "an update") -> None:
        self._children[goal][number]["comments"].append({"body": body})

    def child_issues(self, goal: int) -> list[dict] | None:
        if goal in self.children_fail:
            self.children_fail.discard(goal)
            return None
        out = []
        for number, child in self._children.get(goal, {}).items():
            out.append({
                "number": number, "state": child["state"],
                "labels": [{"name": lbl} for lbl in child["labels"]],
                "comments": list(child["comments"]),
            })
        return out


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTimestamp:
    """A fake wall clock for `Dispatcher(timestamp=...)`, independent of `FakeClock` (which is a
    fake `time.monotonic`) -- the terms deadline is compared against this one (#127)."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def _terms_body(**fields: str) -> str:
    """One `Autopilot terms` comment body with a fenced `key: value` block (#127)."""

    lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"{AUTOPILOT_TERMS_PREFIX}\n\nthe owner's words, echoed.\n\n```\n{lines}\n```"


def _dispatcher(tmp_path: Path, claude: FakeClaude, gh: FakeGh, **kwargs) -> Dispatcher:
    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=kwargs.pop("clock", None) or FakeClock(), **kwargs)


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- build_autopilot_command: the exact command line ---------------------------------------------

def test_build_autopilot_command_matches_the_spec(tmp_path):
    settings = tmp_path / "autopilot_settings.json"
    cmd = build_autopilot_command(116, "claude.exe", settings)
    assert cmd == [
        "claude.exe", "--bg", "--name", "autopilot #116", "--agent", "brain",
        "--permission-mode", "bypassPermissions", "--settings", str(settings),
        "Autopilot on #116. Follow docs/agents/autopilot.md.",
    ]
    assert build_autopilot_prompt(116) == "Autopilot on #116. Follow docs/agents/autopilot.md."


# -- #270 acceptance row 7: the protocol names what wakes a waiting autopilot --------------------

def test_autopilot_doc_names_the_three_wake_changes_and_not_a_child_comment():
    text = (ROOT / "docs" / "agents" / "autopilot.md").read_text(encoding="utf-8")

    assert "closed or reopened" in text
    assert "labels" in text and "changed" in text
    assert "non-checkpoint comment on the goal" in text
    assert "progress comment on a child" in text and "does not wake it" in text


# -- number 1: two autopilot goals + one ready ORCH issue: 3 sessions started --------------------

def test_1_two_autopilot_goals_and_one_orch_ready_issue_start_three_sessions(tmp_path, monkeypatch):
    import tools.agentops.autopilot as autopilot_mod

    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(201)
    gh.autopilot_goal(202)

    class GhWithOneOrchCandidate(FakeGh):
        def ready_for_agent_issues(self):
            from tools.agentops.dispatch import IssueSummary
            return [IssueSummary(number=54, created_at="2026-09-01T00:00:00Z")]

    gh2 = GhWithOneOrchCandidate()
    gh2.autopilot_goal(201)
    gh2.autopilot_goal(202)
    d = _dispatcher(tmp_path, claude, gh2)

    d.tick()

    assert len(claude.started) == 1 and claude.started[0] == [54]
    assert sorted(goal for goal, _ in claude.autopilot_started) == [201, 202]
    for goal, cmd in claude.autopilot_started:
        assert cmd == build_autopilot_command(goal, "claude.exe", autopilot_mod.AUTOPILOT_SETTINGS_PATH)


# -- number 2 (#146): idle with a new checkpoint comment: 1 stop, nothing else changed -> the next --
# -- tick does NOT restart; the goal waits for a signal or the backstop, ORCH is untouched -----------

def test_2_idle_with_new_checkpoint_but_nothing_else_changed_does_not_restart(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(50)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()
    assert len(claude.autopilot_started) == 1
    session_id = d.autopilots[50].session

    gh.set_checkpoints(50, ["Autopilot checkpoint: step 1 done"])
    claude.set_state(session_id, state="working", status="idle")  # ended its turn

    d.tick()

    assert claude.stopped == [session_id]
    assert 50 not in d.autopilots  # not restarted the same tick
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end"]
    assert ends[-1]["why"] == "idle" and ends[-1]["checkpoint_new"] is True

    d.tick()  # nothing it waits on changed and 15 min have not passed: still waiting, not restarted

    assert len(claude.autopilot_started) == 1
    assert claude.started == []  # ORCH never touched


# -- #146 numbers item 3: the five idle-wait cases --------------------------------------------------

def _idle_goal(tmp_path, goal: int, clock: FakeClock,
               before_idle=lambda claude, gh: None) -> tuple[FakeClaude, FakeGh, Dispatcher]:
    """One goal started and immediately idled: the standard starting point for every case below.
    `before_idle` runs after the session starts but before it goes idle -- like a ticket the running
    session itself wrote (e.g. #143's "tickets written" step): it is part of the snapshot the
    waiting rule compares against, exactly as `_end_autopilot` takes it at end time."""

    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(goal)
    d = _dispatcher(tmp_path, claude, gh, clock=clock)
    d.tick()
    before_idle(claude, gh)
    session_id = d.autopilots[goal].session
    claude.set_state(session_id, state="working", status="idle")
    d.tick()  # ends idle, begins waiting
    assert goal not in d.autopilots
    return claude, gh, d


def test_3a_nothing_changed_for_14_min_starts_nothing(tmp_path):
    clock = FakeClock()
    goal = 200
    claude, gh, d = _idle_goal(tmp_path, goal, clock)

    clock.advance(14 * 60)
    d.tick()

    assert len(claude.autopilot_started) == 1  # only the very first start


def test_3b_a_child_closed_starts_one_session_the_next_tick(tmp_path):
    clock = FakeClock()
    goal = 201
    claude, gh, d = _idle_goal(tmp_path, goal, clock,
                              before_idle=lambda c, g: g.add_child(goal, 300))  # "Part of #201", open

    clock.advance(60)
    gh.close_child(goal, 300)
    d.tick()

    assert len(claude.autopilot_started) == 2  # the initial start, plus the one after the close


def test_3c_only_an_assignee_added_starts_nothing(tmp_path):
    clock = FakeClock()
    goal = 202
    claude, gh, d = _idle_goal(tmp_path, goal, clock,
                              before_idle=lambda c, g: g.add_child(goal, 301))  # unclaimed at the end

    clock.advance(60)
    # ORCH claims #301: an assignee is not part of state/labels/comments, so the signature is
    # unchanged -- nothing for the waiting goal to react to.
    d.tick()

    assert len(claude.autopilot_started) == 1  # only the initial start


def test_3d_15_min_with_nothing_starts_one_session(tmp_path):
    clock = FakeClock()
    goal = 203
    claude, gh, d = _idle_goal(tmp_path, goal, clock)

    clock.advance(AUTOPILOT_IDLE_BACKSTOP_MINUTES * 60)
    d.tick()

    assert len(claude.autopilot_started) == 2


def test_3e_a_new_non_checkpoint_comment_on_the_goal_starts_one_session(tmp_path):
    clock = FakeClock()
    goal = 204
    claude, gh, d = _idle_goal(tmp_path, goal, clock)

    clock.advance(60)
    gh.add_goal_comment(goal, "any update from the owner")
    d.tick()

    assert len(claude.autopilot_started) == 2


def test_3f_a_new_checkpoint_style_comment_alone_does_not_restart(tmp_path):
    """A comment starting with `Autopilot checkpoint` on the goal (the hook's own write) is never
    the "new comment" the rule reacts to -- only a comment that is not one."""

    clock = FakeClock()
    goal = 205
    claude, gh, d = _idle_goal(tmp_path, goal, clock)

    clock.advance(60)
    gh.add_goal_comment(goal, f"{AUTOPILOT_CHECKPOINT_PREFIX}: still working")
    d.tick()

    assert len(claude.autopilot_started) == 1


# -- #270: a child's signature is (open, labels) -- a plain comment on a child never wakes it -------

def test_3g_a_new_comment_on_a_child_alone_does_not_restart(tmp_path):
    """Acceptance row 1 (#270): state and labels unchanged, only a new comment on the child, 3 min
    after the wait began -- `_waiting_is_over` stays false, so no restart."""

    clock = FakeClock()
    goal = 206
    claude, gh, d = _idle_goal(tmp_path, goal, clock,
                              before_idle=lambda c, g: g.add_child(goal, 302))

    clock.advance(3 * 60)
    gh.add_comment_to_child(goal, 302, "ORCH's progress on it")
    d.tick()

    assert len(claude.autopilot_started) == 1  # only the initial start


def test_3h_a_label_added_to_a_child_starts_one_session(tmp_path):
    """Acceptance row 2 (#270): the same kind of child gets a label (e.g. `needs-info`) -> true."""

    clock = FakeClock()
    goal = 207
    claude, gh, d = _idle_goal(tmp_path, goal, clock,
                              before_idle=lambda c, g: g.add_child(goal, 303))

    clock.advance(60)
    gh.add_label_to_child(goal, 303, "needs-info")
    d.tick()

    assert len(claude.autopilot_started) == 2


def test_3i_a_child_closed_or_reopened_starts_one_session(tmp_path):
    """Acceptance row 3 (#270): a child closed -> true; another child reopened -> true."""

    clock = FakeClock()
    goal = 208
    claude, gh, d = _idle_goal(
        tmp_path, goal, clock,
        before_idle=lambda c, g: (g.add_child(goal, 304), g.add_child(goal, 305, state="CLOSED")))

    clock.advance(60)
    gh.close_child(goal, 304)
    d.tick()
    assert len(claude.autopilot_started) == 2  # closing #304 woke it

    session_id = d.autopilots[goal].session
    claude.set_state(session_id, state="working", status="idle")
    d.tick()  # ends idle again, begins waiting again
    assert goal not in d.autopilots

    clock.advance(60)
    gh.reopen_child(goal, 305)
    d.tick()
    assert len(claude.autopilot_started) == 3  # reopening #305 woke it


# -- number 3: blocked 3 times with no new checkpoint -> autopilot-stuck, no further start --------

def test_3_three_blocked_restarts_with_no_new_checkpoint_labels_stuck(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(60)
    gh.set_checkpoints(60, ["Autopilot checkpoint: same state"])  # never changes across restarts
    d = _dispatcher(tmp_path, claude, gh)

    for i in range(MAX_AUTOPILOT_RESTARTS):
        d.tick()
        assert 60 in d.autopilots, f"round {i}: expected a live session"
        session_id = d.autopilots[60].session
        claude.set_state(session_id, state="blocked", status="idle")
        d.tick()  # ends it
        assert 60 not in d.autopilots

    assert (60, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert gh.comment_calls and gh.comment_calls[-1][0] == 60

    started_before = len(claude.autopilot_started)
    d.tick()  # the goal is no longer offered: no further start
    assert len(claude.autopilot_started) == started_before
    assert 60 not in gh.autopilot_goals()


def test_3_a_new_checkpoint_between_blocked_restarts_resets_the_streak(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(61)
    d = _dispatcher(tmp_path, claude, gh)

    for i, checkpoint in enumerate(["Autopilot checkpoint: a", "Autopilot checkpoint: a",
                                    "Autopilot checkpoint: b"]):
        d.tick()
        gh.set_checkpoints(61, [checkpoint])
        session_id = d.autopilots[61].session
        claude.set_state(session_id, state="blocked", status="idle")
        claude._sessions[session_id].update(waitingFor="permission prompt")  # a real block (#935)
        d.tick()

    # the third restart's checkpoint ("b") differs from the second's ("a"): the streak reset, so
    # no autopilot-stuck label was ever added even though 3 blocked ends happened in total.
    assert (61, AUTOPILOT_STUCK_LABEL) not in gh.added_labels
    d.tick()
    assert 61 in d.autopilots


# -- number 4: the goal loses its `autopilot` label while its session runs: left to finish, --------
# -- 0 restarts after it ends ----------------------------------------------------------------------

def test_4_label_removed_mid_session_is_left_to_finish_and_not_restarted(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(70)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()
    session_id = d.autopilots[70].session
    gh.remove_autopilot_label(70)  # the goal is no longer eligible while the session still runs

    d.tick()  # still live: nothing changes
    assert 70 in d.autopilots and claude.stopped == []

    claude.set_state(session_id, state="working", status="idle")  # it finishes its turn
    d.tick()

    assert claude.stopped == [session_id] and 70 not in d.autopilots

    started_before = len(claude.autopilot_started)
    d.tick()  # goal no longer offered: no restart
    assert len(claude.autopilot_started) == started_before


# -- adoption: a live `autopilot #N` session is adopted, never double-started ----------------------

def test_a_live_autopilot_session_is_adopted_and_not_double_started(tmp_path):
    claude = FakeClaude()
    claude.add_session("abc123", name="autopilot #80", kind="background", state="working",
                       status="busy", startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    gh.autopilot_goal(80)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()

    assert claude.autopilot_started == [] and 80 in d.autopilots and d.autopilots[80].session == "abc123"
    assert any(e["kind"] == "autopilot_adopt" for e in _events(tmp_path))


# -- workspace-not-trusted: marked stuck immediately, never retried -------------------------------

def test_workspace_not_trusted_marks_stuck_without_retry(tmp_path):
    claude = FakeClaude()
    claude.start_autopilot_fails[90] = "claude --bg failed: Workspace not trusted"
    gh = FakeGh()
    gh.autopilot_goal(90)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()

    assert (90, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert WORKSPACE_NOT_TRUSTED in gh.comment_calls[-1][1]
    assert 90 not in gh.autopilot_goals()
    d.tick()
    assert claude.autopilot_started == []


def test_workspace_not_trusted_marks_stuck_and_never_wakes_the_chat_brain(tmp_path):
    # #1105: a `chat:` line is ignored; the goal is still labelled stuck, with no wake.
    claude = FakeClaude()
    claude.start_autopilot_fails[92] = "claude --bg failed: Workspace not trusted"
    gh = FakeGh()
    gh.autopilot_goal(92)
    gh.set_terms(92, [_terms_body(chat="brain")])
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()
    d.tick()

    assert (92, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert claude.wake_calls == []
    assert claude.autopilot_started == []


def test_an_ordinary_start_failure_is_logged_but_not_marked_stuck(tmp_path):
    claude = FakeClaude()
    claude.start_autopilot_fails[91] = "claude --bg failed: some other transient error"
    gh = FakeGh()
    gh.autopilot_goal(91)
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh)
    d.log = messages.append

    d.tick()

    assert gh.added_labels == [] and any("claude --bg failed" in m for m in messages)


# -- --dry-run: nothing is actually started ---------------------------------------------------------

def test_dry_run_starts_nothing_but_logs(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(95)
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, dry_run=True)
    d.log = messages.append

    d.tick()

    assert claude.autopilot_started == [] and 95 not in d.autopilots
    assert any("dry-run" in m and "#95" in m for m in messages)


# -- parse_autopilot_terms / build_wake_command: unit-level ---------------------------------------

def test_parse_autopilot_terms_reads_the_fenced_block():
    body = _terms_body(scope="#63", deadline="2026-09-28T09:00:00+00:00", quiet="yes",
                       usage="15%", lifted="cloud credits <= $20", chat="brain")
    terms = parse_autopilot_terms(body)
    assert terms.scope == "#63" and terms.quiet is True and terms.usage == "15%"
    assert terms.lifted == "cloud credits <= $20" and terms.chat == "brain"
    assert terms.deadline == datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    assert terms.parse_error is None


def test_parse_autopilot_terms_bad_deadline_names_the_line():
    terms = parse_autopilot_terms(_terms_body(deadline="tomorrow 9", chat="brain"))
    assert terms.parse_error == "deadline: tomorrow 9"


def test_parse_autopilot_terms_missing_chat_is_not_a_parse_error():
    # #1105: `chat:` is no longer required; a terms block without it parses.
    terms = parse_autopilot_terms(_terms_body(scope="#63"))
    assert terms.parse_error is None


def test_build_wake_command_matches_the_researched_one_shot_sender():
    cmd = build_wake_command("brain", "hi", "claude.exe")
    assert cmd[:7] == ["claude.exe", "-p", "--model", "haiku", "--output-format", "json",
                       "--allowedTools=SendMessage"]
    assert cmd[7:9] == ["--name", "dispatcher"]
    assert 'to="brain"' in cmd[-1] and 'message="hi"' in cmd[-1]
    assert cmd[-1] == build_wake_prompt("brain", "hi")


# -- numbers item 2: no start of an `autopilot #N` at or past its deadline ------------------------

def test_2_starts_before_the_deadline(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(100)
    gh.set_terms(100, [_terms_body(deadline="2026-09-28T09:00:00+00:00", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 8, 59, tzinfo=timezone.utc))
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)

    d.tick()

    assert sorted(g for g, _ in claude.autopilot_started) == [100]


def test_2_no_start_at_the_deadline_and_logs_once(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(101)
    gh.set_terms(101, [_terms_body(deadline="2026-09-28T09:00:00+00:00", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)
    d.log = messages.append

    d.tick()
    assert claude.autopilot_started == [] and 101 not in d.autopilots
    assert any("autopilot #101 past deadline" in m for m in messages)

    d.tick()  # logged once, not every tick
    assert sum("past deadline" in m for m in messages) == 1


# -- numbers item 3: a live session 15 min past its deadline is stopped ---------------------------

def _live_goal_past_deadline(tmp_path, minutes_past: float):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(110)
    gh.set_terms(110, [_terms_body(deadline="2026-09-28T09:00:00+00:00", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)
    d.tick()
    session_id = d.autopilots[110].session
    ts.advance(hours=1, minutes=minutes_past)
    d.tick()
    return claude, d, session_id


def test_3_a_live_session_16_min_past_deadline_is_stopped(tmp_path):
    claude, d, session_id = _live_goal_past_deadline(tmp_path, AUTOPILOT_DEADLINE_GRACE_MINUTES + 1)
    assert claude.stopped == [session_id]
    assert 110 not in d.autopilots


def test_3_a_live_session_14_min_past_deadline_is_not_stopped(tmp_path):
    claude, d, session_id = _live_goal_past_deadline(tmp_path, AUTOPILOT_DEADLINE_GRACE_MINUTES - 1)
    assert claude.stopped == []
    assert 110 in d.autopilots


# -- numbers item 4: `quiet: yes` at the deadline logs it; no wake to the chat brain (#1105) -------

def test_4_quiet_at_the_deadline_logs_once_and_never_wakes_the_chat_brain(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(120)
    gh.set_terms(120, [_terms_body(deadline="2026-09-28T09:00:00+00:00", quiet="yes", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)
    d.log = messages.append

    d.tick()
    assert claude.wake_calls == []
    assert any("autopilot #120 past deadline" in m for m in messages)

    d.tick()
    assert claude.wake_calls == []
    assert sum("past deadline" in m for m in messages) == 1


def test_4_no_quiet_no_wake_at_the_deadline(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(121)
    gh.set_terms(121, [_terms_body(deadline="2026-09-28T09:00:00+00:00", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)

    d.tick()

    assert claude.wake_calls == []


# -- numbers item 5: marking a goal `autopilot-stuck` labels, comments and logs; no wake (#1105) ----
# -- and a wake that would fail is never even attempted ------------------------------------------

def test_5_stuck_after_three_blocked_restarts_labels_and_never_wakes_the_chat_brain(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(130)
    gh.set_checkpoints(130, ["Autopilot checkpoint: same state"])  # never changes across restarts
    gh.set_terms(130, [_terms_body(chat="brain")])
    d = _dispatcher(tmp_path, claude, gh)

    for _ in range(MAX_AUTOPILOT_RESTARTS):
        d.tick()
        session_id = d.autopilots[130].session
        claude.set_state(session_id, state="blocked", status="idle")
        d.tick()

    assert (130, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert any(e["kind"] == "autopilot_stuck" for e in _events(tmp_path))
    assert claude.wake_calls == []


def test_5_a_stuck_goal_attempts_no_wake_even_when_the_wake_would_fail(tmp_path):
    claude = FakeClaude()
    claude.wake_results["brain"] = (False, '{"success":false,"message":"no agent"}')
    gh = FakeGh()
    gh.autopilot_goal(131)
    gh.set_checkpoints(131, ["Autopilot checkpoint: same state"])
    gh.set_terms(131, [_terms_body(chat="brain")])
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh)
    d.log = messages.append

    for _ in range(MAX_AUTOPILOT_RESTARTS):
        d.tick()
        session_id = d.autopilots[131].session
        claude.set_state(session_id, state="blocked", status="idle")
        d.tick()

    assert (131, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert claude.wake_calls == []
    assert not any("wake" in m and "failed" in m for m in messages)
    assert not any(e["kind"] == "autopilot_wake" for e in _events(tmp_path))


# -- numbers item 6: an unparseable `deadline` labels the goal stuck, quoting the line -------------

def test_6_bad_deadline_line_marks_stuck_and_starts_nothing(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(140)
    gh.set_terms(140, [_terms_body(deadline="tomorrow 9", chat="brain")])
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()

    assert (140, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert gh.comment_calls and "deadline: tomorrow 9" in gh.comment_calls[-1][1]
    assert claude.autopilot_started == []


def test_backgrounded_session_id_is_read_through_ansi_colour_codes() -> None:
    """#116 live smoke: claude.exe wrapped the id in colour codes and start_autopilot raised."""
    from tools.agentops import claude_bg

    coloured = "backgrounded · \x1b[36ma97c7c60\x1b[39m · autopilot #999"
    plain = "backgrounded · a97c7c60 · autopilot #999"
    assert claude_bg.SESSION_ID_RE.search(coloured).group(1) == "a97c7c60"
    assert claude_bg.SESSION_ID_RE.search(plain).group(1) == "a97c7c60"


# -- #146 numbers item 4: the two replays for the closing comment --------------------------------

def replay_143(tmp_path) -> int:
    """Replay #143's rehearsal timeline (`artifacts/runs/dispatch.out` lines 82-90: tickets written
    13:18, claimed 13:19, #145 closed 13:20:17, #144 closed 13:21:56) through the #146 rule. Returns
    the number of autopilot sessions started between 13:16 and 13:24 (measured: 4)."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh()
    goal = 108  # the Autopilot map, #143's rehearsal goal
    gh.autopilot_goal(goal)
    d = Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                  state_file=tmp_path / "state.json", projects_dir=tmp_path / "projects", clock=clock)

    d.tick()  # 13:16: session 1 starts
    session_id = d.autopilots[goal].session
    gh.add_child(goal, 144)
    gh.add_child(goal, 145)  # 13:18: the two tickets, written while the session is still live
    claude.set_state(session_id, state="working", status="idle")
    d.tick()  # the session ends its turn idle; the waiting snapshot has both, open

    clock.advance(60.0)  # ~13:19: ORCH claims both -- assignee only, no signature change
    d.tick()

    clock.advance(77.0)  # ~13:20:17: #145 closes
    gh.close_child(goal, 145)
    clock.advance(300.0)  # #318: the waiting check reads children at most every 5 min
    d.tick()  # session 2 starts
    session_id = d.autopilots[goal].session
    claude.set_state(session_id, state="working", status="idle")
    d.tick()  # ends idle; the waiting snapshot now has #144 open, #145 closed

    clock.advance(99.0)  # ~13:21:56: #144 closes
    gh.close_child(goal, 144)
    clock.advance(300.0)  # #318: same throttle
    d.tick()  # session 3 starts

    return len(claude.autopilot_started)


def test_replay_143_gives_at_most_three_sessions_against_four_measured(tmp_path):
    count = replay_143(tmp_path)
    assert count <= 3, f"replay of #143 gave {count} sessions, expected at most 3 (measured: 4)"


def simulate_five_tickets_thirty_minutes_apart(tmp_path) -> int:
    """A made-up 3 h goal with 5 "Part of #N" tickets closing 30 min apart, ticked every
    `TICK_SECONDS`; a started session is modelled as idling by the very next tick -- the fastest
    possible churn once the goal has nothing left to wait on but the backstop. Returns the total
    number of autopilot sessions started across the 3 h (#146 numbers item 4: "at most 6 starts +
    12 backstop starts", i.e. at most 18 in total)."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh()
    goal = 300
    gh.autopilot_goal(goal)
    d = Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                  state_file=tmp_path / "state.json", projects_dir=tmp_path / "projects", clock=clock)

    for number in range(500, 505):
        gh.add_child(goal, number)  # 5 tickets, all open when the first session ends

    closes_at_minute = {30: 500, 60: 501, 90: 502, 120: 503, 150: 504}
    total_ticks = int(3 * 3600 / TICK_SECONDS)  # 3 h
    for tick_idx in range(total_ticks):
        minute = tick_idx * TICK_SECONDS / 60.0
        for target in [t for t in closes_at_minute if minute >= t]:
            gh.close_child(goal, closes_at_minute.pop(target))
        d.tick()
        if goal in d.autopilots:  # the fastest possible churn: idle it right away
            claude.set_state(d.autopilots[goal].session, state="working", status="idle")
        clock.advance(TICK_SECONDS)

    return len(claude.autopilot_started)


def test_replay_five_tickets_30_min_apart_over_3h_stays_within_6_plus_12_backstop_starts(tmp_path):
    count = simulate_five_tickets_thirty_minutes_apart(tmp_path)
    assert count <= 6 + 12, f"3 h/5-ticket replay gave {count} sessions, expected at most 18"


def test_5_claude_cli_wake_reads_success_from_result_not_the_exit_code(monkeypatch):
    """#123 trap 4: rc 0 and `is_error: false` while SendMessage itself failed."""

    import subprocess as _sp

    from tools.agentops import dispatch as _d

    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        envelope = {"is_error": False, "result": '{"success":false,"message":"no agent named brain"}'}
        return _sp.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(_d.subprocess, "run", fake_run)
    ok, detail = _d.ClaudeCli("C:/full/path/claude.exe").wake("brain", "autopilot #1: stuck")
    assert ok is False and "no agent" in detail
    assert calls[0]["cmd"][0] == "C:/full/path/claude.exe"
    assert not str(calls[0]["cwd"]).replace("\\", "/").lower().startswith(str(_d.ROOT).replace("\\", "/").lower())

    def fake_ok(cmd, **kwargs):
        envelope = {"is_error": False, "result": 'Sent. {"success":true,"message":"queued"}'}
        return _sp.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(_d.subprocess, "run", fake_ok)
    assert _d.ClaudeCli("claude.exe").wake("brain", "x")[0] is True


# -- #319: an autopilot goal holds exactly one area label ------------------------------------------

def _goal_gh(labels_by_number: dict[int, list[str]]):
    import json as _json
    import subprocess as _sp
    from tools.agentops.dispatch import GhCli

    data = [{"number": n, "labels": [{"name": x} for x in labels]} for n, labels in labels_by_number.items()]
    return GhCli(runner=lambda args, stdin: _sp.CompletedProcess(args, 0, _json.dumps(data), ""))


def test_319_autopilot_goal_without_an_area_is_not_started_one_line_over_two_ticks(monkeypatch, capsys):
    monkeypatch.setattr("tools.agentops.autopilot.area_labels", lambda: ["area:runs"])
    gh = _goal_gh({7: ["autopilot"], 8: ["autopilot", "area:runs"]})
    assert gh.autopilot_goals() == [8]
    assert gh.autopilot_goals() == [8]
    err = capsys.readouterr().err
    assert err.count("autopilot #7 not started: no area") == 1


def test_319_autopilot_goal_starts_when_areas_is_empty(monkeypatch):
    monkeypatch.setattr("tools.agentops.autopilot.area_labels", lambda: [])
    assert _goal_gh({7: ["autopilot"]}).autopilot_goals() == [7]


# -- #337: a session whose goal is closed or unlabelled is neither adopted nor ended ----------------

def _stale_session_case(tmp_path, goal_labels):
    claude = FakeClaude()
    claude.add_session("stale1", name="autopilot #265", kind="background", state="working",
                       status="busy", startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    if goal_labels is not None:
        gh.autopilot_goal(265, labels=goal_labels)
    d = _dispatcher(tmp_path, claude, gh)
    for _ in range(3):
        d.tick()
    return d, claude


def test_a_live_session_of_a_closed_goal_is_not_adopted_nor_ended(tmp_path):
    d, claude = _stale_session_case(tmp_path, None)
    assert 265 not in d.autopilots and claude.autopilot_started == []
    assert "stale1" not in d.ended_sessions and claude.ended_pids == []


def test_a_live_session_of_a_goal_without_the_autopilot_label_is_not_adopted_nor_ended(tmp_path):
    # FakeGh.autopilot_goals lists only goals whose label set holds `autopilot`, like the real call
    d, claude = _stale_session_case(tmp_path, ())
    assert 265 not in d.autopilots and claude.autopilot_started == []
    assert "stale1" not in d.ended_sessions and claude.ended_pids == []


# -- #562: ONE `Autopilot checkpoint` comment that each session edits in place (same id, new body) --

class _InPlaceGh(FakeGh):
    """The checkpoint is read from the goal's own comments, so an edit keeps the comment id."""

    def latest_checkpoint_comment(self, number: int) -> str | None:
        for comment in reversed(self._goal_comments.get(number, [])):
            if str(comment["body"]).startswith(AUTOPILOT_CHECKPOINT_PREFIX):
                return comment["body"]
        return None

    def edit_goal_comment(self, number: int, body: str) -> None:
        self._goal_comments[number][0]["body"] = body


def _in_place_session(tmp_path, goal: int, edit: bool):
    clock = FakeClock()
    claude = FakeClaude()
    gh = _InPlaceGh()
    gh.autopilot_goal(goal)
    gh.add_goal_comment(goal, f"{AUTOPILOT_CHECKPOINT_PREFIX}: step 1")
    d = _dispatcher(tmp_path, claude, gh, clock=clock)
    d.tick()
    if edit:
        gh.edit_goal_comment(goal, f"{AUTOPILOT_CHECKPOINT_PREFIX}: step 2")
    claude.set_state(d.autopilots[goal].session, state="working", status="idle")
    d.tick()
    assert goal not in d.autopilots
    return claude, gh, d, clock


def test_562_in_place_edit_of_the_checkpoint_is_checkpoint_new(tmp_path):
    _in_place_session(tmp_path, 300, edit=True)
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end"]
    assert ends[-1]["checkpoint_new"] is True


def test_562_unchanged_checkpoint_body_is_not_checkpoint_new(tmp_path):
    _in_place_session(tmp_path, 301, edit=False)
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end"]
    assert ends[-1]["checkpoint_new"] is False


def test_562_edit_of_the_seen_checkpoint_while_waiting_never_wakes_before_backstop(tmp_path):
    claude, gh, d, clock = _in_place_session(tmp_path, 302, edit=False)
    clock.advance(60)
    gh.edit_goal_comment(302, f"{AUTOPILOT_CHECKPOINT_PREFIX}: the session's own edit")
    d.tick()
    assert len(claude.autopilot_started) == 1
    assert d._waiting_is_over(d.autopilot_waiting[302]) is False


# -- #544: a `done` end waits exactly like an `idle` end (24 of 38 sessions on #486 were nothing-moved
# -- restarts); the `started` line names its reason --------------------------------------------------

def _done_goal(tmp_path, goal: int, clock: FakeClock, logs: list[str],
               before_end=lambda claude, gh: None) -> tuple[FakeClaude, FakeGh, Dispatcher]:
    """One goal started, then ended `done` (state `done`, not `idle`), after `before_end`."""

    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(goal)
    d = _dispatcher(tmp_path, claude, gh, clock=clock, log=logs.append)
    d.tick()
    before_end(claude, gh)
    session_id = d.autopilots[goal].session
    claude.set_state(session_id, state="done", status="idle")
    d.tick()  # ends done, begins waiting
    assert goal not in d.autopilots
    assert goal in d.autopilot_waiting
    return claude, gh, d


def _started_lines(logs: list[str], goal: int) -> list[str]:
    return [m for m in logs if m.startswith(f"autopilot #{goal} started (")]


def test_544_row1_done_with_new_checkpoint_waits_exactly_the_backstop(tmp_path):
    clock = FakeClock()
    logs: list[str] = []
    claude, gh, d = _done_goal(
        tmp_path, 400, clock, logs,
        before_end=lambda c, g: g.set_checkpoints(400, ["Autopilot checkpoint: step 1 done"]))
    assert any(m.startswith("autopilot #400 reset (done, session ")
               and m.endswith("; waiting for a change (backstop 15 min)") for m in logs)
    assert not any("restarting next tick" in m for m in logs)

    for _ in range(3):  # a few 30 s ticks, nothing moved
        clock.advance(TICK_SECONDS)
        d.tick()
    clock.advance(AUTOPILOT_IDLE_BACKSTOP_MINUTES * 60 - 1 - 3 * TICK_SECONDS)  # 14 min 59 s since the end
    d.tick()
    assert len(claude.autopilot_started) == 1

    clock.advance(1)  # 15 min
    d.tick()
    assert len(claude.autopilot_started) == 2
    clock.advance(TICK_SECONDS)
    d.tick()
    assert len(claude.autopilot_started) == 2  # exactly one
    assert _started_lines(logs, 400)[-1].endswith("(backstop 15 min)")


def test_544_row2_done_then_a_child_label_change_starts_one_session_next_tick(tmp_path):
    clock = FakeClock()
    logs: list[str] = []
    claude, gh, d = _done_goal(tmp_path, 401, clock, logs,
                              before_end=lambda c, g: g.add_child(401, 310))
    clock.advance(TICK_SECONDS)
    gh.add_label_to_child(401, 310, "needs-info")
    d.tick()
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 401)[-1].endswith("(change: #310 labels)")


def test_544_row3_done_then_a_non_checkpoint_comment_starts_one_session_next_tick(tmp_path):
    clock = FakeClock()
    logs: list[str] = []
    claude, gh, d = _done_goal(tmp_path, 402, clock, logs)
    clock.advance(TICK_SECONDS)
    gh.add_goal_comment(402, f"{AUTOPILOT_CHECKPOINT_PREFIX}: the session's own, late")
    d.tick()
    assert len(claude.autopilot_started) == 1  # a checkpoint alone never wakes it
    clock.advance(TICK_SECONDS)
    gh.add_goal_comment(402, "any update from the owner")
    d.tick()
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 402)[-1].endswith("(change: comment on #402)")


def test_544_started_line_names_its_reason_and_keeps_the_old_prefix(tmp_path):
    # first session: no wait
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(410)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    (line,) = _started_lines(logs, 410)
    session = d.autopilots[410].session
    assert line.startswith(f"autopilot #410 started ({session}): ")
    assert line.endswith("(first session)")

    # a child closed, then another reopened
    clock = FakeClock()
    logs = []
    sub = tmp_path / "c"
    sub.mkdir()
    claude, gh, d = _done_goal(
        sub, 411, clock, logs,
        before_end=lambda c, g: (g.add_child(411, 320), g.add_child(411, 321, state="CLOSED")))
    clock.advance(TICK_SECONDS)
    gh.close_child(411, 320)
    d.tick()
    assert _started_lines(logs, 411)[-1].endswith("(change: #320 closed)")
    claude.set_state(d.autopilots[411].session, state="done", status="idle")
    d.tick()
    clock.advance(TICK_SECONDS)
    gh.reopen_child(411, 321)
    d.tick()
    assert _started_lines(logs, 411)[-1].endswith("(change: #321 reopened)")


def test_544_a_restart_after_a_blocked_end_says_so_not_first_session(tmp_path):
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(412)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    claude.set_state(d.autopilots[412].session, state="blocked", status="idle")
    d.tick()
    d.tick()
    lines = _started_lines(logs, 412)
    assert len(lines) == 2
    assert lines[0].endswith("(first session)")
    assert lines[1].endswith("(restart after blocked)")


# -- #934: the reset line of a blocked autopilot session names what it waited for ------------------

def _reset_line(tmp_path, goal: int, **row) -> str:
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(goal)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    session_id = d.autopilots[goal].session
    claude.set_state(session_id, state=row.pop("state"), status="idle")
    claude._sessions[session_id].update(row)
    d.tick()
    return next(line for line in logs if f"autopilot #{goal} reset (" in line)


def test_934_blocked_reset_line_names_the_waiting_reason(tmp_path):
    line = _reset_line(tmp_path, 934, state="blocked", waitingFor="permission prompt")
    assert "waiting for: permission prompt" in line


def test_934_blocked_reset_line_without_a_reason_says_so(tmp_path):
    line = _reset_line(tmp_path, 935, state="blocked")
    assert "waiting for: no reason given" in line


def test_934_idle_reset_line_has_no_waiting_for_part(tmp_path):
    line = _reset_line(tmp_path, 936, state="done", waitingFor="permission prompt")
    assert "waiting for:" not in line


# -- #935: a blocked verdict keeps the whole listing row in the log ----------------------------------

def test_935_blocked_reset_line_is_followed_by_the_whole_row(tmp_path):
    import json as _json
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(937)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    session_id = d.autopilots[937].session
    claude.set_state(session_id, state="blocked", status="idle")
    claude._sessions[session_id].update(waitingFor=None, extraKey="kept")
    row = dict(claude._sessions[session_id])
    d.tick()
    i = next(n for n, line in enumerate(logs) if "autopilot #937 reset (blocked" in line)
    nxt = logs[i + 1]
    assert nxt.startswith("autopilot #937 blocked row: ")
    kept = _json.loads(nxt.split("blocked row: ", 1)[1])
    assert set(kept) == set(row)
    assert kept["extraKey"] == "kept"


def test_935_an_idle_end_logs_no_blocked_row(tmp_path):
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(938)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    claude.set_state(d.autopilots[938].session, state="done", status="idle")
    d.tick()
    assert not any("blocked row" in line for line in logs)


# -- #935 unit 2: a finished turn listed as `blocked` is not restarted as blocked ----------------------

def _blocked_end(tmp_path, goal: int, *, new_checkpoint: bool, **row):
    claude, gh = FakeClaude(), FakeGh()
    gh.autopilot_goal(goal)
    gh.set_checkpoints(goal, ["Autopilot checkpoint: before"])
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    d.tick()
    session_id = d.autopilots[goal].session
    if new_checkpoint:
        gh.set_checkpoints(goal, ["Autopilot checkpoint: patched by the session"])
    claude.set_state(session_id, state="blocked", status="idle")
    claude._sessions[session_id].update(row)
    d.tick()
    return claude, d, logs


def test_935_a_blocked_row_without_waiting_for_and_a_new_checkpoint_is_not_restarted_as_blocked(tmp_path):
    claude, d, logs = _blocked_end(tmp_path, 940, new_checkpoint=True)
    d.tick()
    assert len(claude.autopilot_started) == 1  # waits for a change or the backstop, no restart
    assert 940 in d.autopilot_waiting
    assert not any("reset (blocked" in line for line in logs)
    assert any("handled as idle" in line and '"state": "blocked"' in line for line in logs)
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end"]
    assert ends[-1]["why"] == "idle" and ends[-1]["checkpoint_new"] is True


def test_935_a_blocked_row_waiting_for_a_permission_prompt_still_restarts_as_blocked(tmp_path):
    claude, d, logs = _blocked_end(tmp_path, 941, new_checkpoint=True, waitingFor="permission prompt")
    d.tick()
    assert len(claude.autopilot_started) == 2
    assert any("reset (blocked" in line and "waiting for: permission prompt" in line for line in logs)
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end"]
    assert ends[-1]["why"] == "blocked"


def test_935_a_blocked_row_without_waiting_for_and_no_new_checkpoint_still_restarts_as_blocked(tmp_path):
    claude, d, logs = _blocked_end(tmp_path, 942, new_checkpoint=False)
    d.tick()
    assert len(claude.autopilot_started) == 2
    assert any("reset (blocked" in line and "no reason given" in line for line in logs)
