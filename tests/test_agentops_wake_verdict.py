"""#302: one reply rule for the wake, used by `wake()` and `ClaudeCli.wake`."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from tools.agentops.brain_notice import BrainNotifier
from tools.agentops.dispatch import ClaudeCli
from tools.agentops.wake import judge_reply, wake


def _env(result, **extra):
    data = {"type": "result", "subtype": "success", "is_error": False, "result": result}
    data.update(extra)
    return json.dumps(data)


def _via_wake(rc, stdout):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=rc, stdout=stdout, stderr="")

    return wake("brain", "hi", "claude.exe", run=run)[0]


def _via_cli(monkeypatch, rc, stdout):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=rc, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    return ClaudeCli("claude.exe").wake("brain", "hi")[0]


def _both(monkeypatch, rc, stdout):
    return _via_wake(rc, stdout), _via_cli(monkeypatch, rc, stdout)


@pytest.mark.parametrize("text", ["Message sent.", "Message sent successfully to brain", "Message sent to brain"])
def test_row1_sent_texts_are_delivered(monkeypatch, text):
    assert _both(monkeypatch, 0, _env(text)) == (True, True)


def test_row2_nonzero_rc(monkeypatch):
    assert _both(monkeypatch, 1, _env("Message sent.")) == (False, False)


def test_row3_is_error(monkeypatch):
    assert _both(monkeypatch, 0, _env("Message sent.", is_error=True)) == (False, False)


@pytest.mark.parametrize("text", [
    "Failed to send the message: no such session",
    "I could not send the message.",
    "Error: SendMessage is not available",
])
def test_row4_failure_texts(monkeypatch, text):
    assert _both(monkeypatch, 0, _env(text)) == (False, False)


def test_row5_permission_denials(monkeypatch):
    out = _env("Message sent.", permission_denials=[{"tool_name": "SendMessage"}])
    assert _both(monkeypatch, 0, out) == (False, False)


def test_row6_success_envelopes(monkeypatch):
    assert _both(monkeypatch, 0, _env('{"success":true}')) == (True, True)
    assert _both(monkeypatch, 0, _env('{ "success": true }')) == (True, True)
    assert _both(monkeypatch, 0, _env('{"success":false}')) == (False, False)
    assert _both(monkeypatch, 0, "") == (False, False)
    assert _both(monkeypatch, 0, "not json") == (False, False)


#: #216: two real SendMessage replies (2026-10-19) that say the message reached the target's inbox.
BRAIN = "Brain: orch layer"
REPLY_A = (
    'The message went through to "Brain: orch layer". The tool reports it reached that session\'s inbox, '
    "but not that its Claude has read it yet. No delivery notice has come back, so I can't yet tell whether "
    "the session holds it for approval."
)
REPLY_B = (
    'success:true. The message is in the inbox of "Brain: orch layer" and has not been read yet. '
    "A delivery notice will follow if that session holds it or refuses it."
)


@pytest.mark.parametrize("text", [REPLY_A, REPLY_B])
def test_row8_inbox_replies_are_delivered(text):
    assert judge_reply(0, _env(text), BRAIN)[0] is True


@pytest.mark.parametrize("text", [REPLY_A, REPLY_B])
def test_row8_inbox_reply_is_one_send_no_retry(text):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=_env(text), stderr="")

    assert wake(BRAIN, "hi", "claude.exe", run=run)[0] is True
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    'No such session "Brain: orch layer" is running; the message was not delivered.',
    'success:false. The message is in the inbox of "Brain: orch layer".',
    'SendMessage failed: the message never went through to "Brain: orch layer".',
    'The message never went through to "Brain: orch layer"; it is not in its inbox.',
    'Error: the message did not reach the inbox of "Brain: orch layer".',
])
def test_row9_not_arrived_replies_are_not_delivered(text):
    assert judge_reply(0, _env(text), BRAIN)[0] is False


@pytest.mark.parametrize("text", [
    'I cannot find another Claude session named "Brain: orch layer".',
    'The message went out to "Brain: orch layer", but I can\'t access that session.',
    'The message isn\'t in the inbox of "Brain: orch layer" yet.',
    'The message hasn\'t reached the inbox of "Brain: orch layer".',
])
def test_row9b_negated_or_unreachable_replies_are_not_delivered(text):
    assert judge_reply(0, _env(text), BRAIN)[0] is False


def test_row9c_negation_of_read_is_not_negation_of_arrival():
    assert judge_reply(0, _env("Not yet read, but it reached the inbox."), BRAIN)[0] is True


NO_SESSION = 'No such session "Brain: orch layer" is running; the message was not delivered.'


def test_row10_failure_reply_keeps_the_one_retry(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=_env(NO_SESSION), stderr="")

    def send(title, message):  # as dispatch's _send_to_brain: a False wake raises, the notifier keeps one retry
        ok, detail = wake(title, message, "claude.exe", run=fake_run)
        if not ok:
            raise RuntimeError(f"wake of {title} failed: {detail}")

    logs = []
    notifier = BrainNotifier(
        state_path=tmp_path / "state.json",
        brain_title=BRAIN, send=send,
        issue_info=lambda n: ("Monitor card layout", "parked-on-run: x"),
        autopilot_live_for=lambda n: None, brain_live=lambda: True,
        log=logs.append, now=lambda: datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc),
    )
    assert notifier.observe({101: "parked"}) == []
    assert len(calls) == 1
    assert notifier.observe({}) == []  # the one retry, on a later call
    assert len(calls) == 2
    assert notifier.observe({}) == []  # never a third
    assert len(calls) == 2
    assert any("dropped" in line for line in logs)


@pytest.mark.parametrize("text,expected", [
    ("Message wasn't sent.", False),
    ("Message never sent", False),
    ("Message was not sent.", False),
    ("Failed to deliver the message.", False),
    ("Message sent to Error Review chat.", True),
    ("ok (delivery: delivered; message_id: 1)", True),
    ("Message sent (delivery: queued; message_id: 1)", True),
    ("Message sent (delivery: no such chat; message_id: 1)", False),
])
def test_row7_negation_and_delivery_field(monkeypatch, text, expected):
    assert _both(monkeypatch, 0, _env(text)) == (expected, expected)
