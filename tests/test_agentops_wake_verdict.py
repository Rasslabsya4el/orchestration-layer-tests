"""#302: one reply rule for the wake, used by `wake()` and `ClaudeCli.wake`."""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.agentops.dispatch import ClaudeCli
from tools.agentops.wake import wake


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
