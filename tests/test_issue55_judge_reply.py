"""#55: the judge reads the three reply shapes seen on 2026-10-16 as delivered, keeps its
negated and failure precedence, and logs a fallthrough detail on one line."""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.agentops.dispatch import ClaudeCli
from tools.agentops.wake import wake

CHAT = "Brain: orch layer"

# The three replies of artifacts/runs/dispatch.out (2026-10-16), as the log kept them (200 chars).
REPLY_1509 = (
    'success":true,"message":"\u201c#46 A mod is installed or removed by one command, the owner\'s se: '
    'parked: parked-on-run: parked on merge D:\\Work\\orchestration-layer\\var\\agentops\\results\\20261016T150'
)
REPLY_1511 = (
    "The message went out once. The tool result, verbatim:\n\n"
    "\u201c#27 Two review roles, standards and spec, join the roles table w: closed: Closing (ORCH 4df0e484). "
    "Merge 27-docs landed on main 1297c96 (suite 8)"
)
REPLY_1543 = (
    "\u201c#33 The unslop mod is removed from the repo, the machine and the: closed: Closing (ORCH 6f3ff88f, #33). "
    "Rows read on `main` a264775 (after #46's merge).\u201d \u2192 Brain: orch layer (another Claude session o"
)
RECORDED = [REPLY_1509, REPLY_1511, REPLY_1543]


def _env(result, **extra):
    data = {"type": "result", "subtype": "success", "is_error": False, "result": result}
    data.update(extra)
    return json.dumps(data)


def _via_wake(stdout, chat=CHAT):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    return wake(chat, "hi", "claude.exe", run=run)


def _via_cli(monkeypatch, stdout):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    return ClaudeCli("claude.exe").wake("brain", "hi")[0]


@pytest.mark.parametrize("text", RECORDED)
def test_row1_recorded_replies_are_delivered(monkeypatch, text):
    assert _via_wake(_env(text))[0] is True
    assert _via_cli(monkeypatch, _env(text)) is True


def test_row1_three_of_three(monkeypatch):
    assert sum(_via_wake(_env(text))[0] for text in RECORDED) == 3


def test_arrow_names_the_target_title():
    assert _via_wake(_env("\u2192 Brain: orch layer"))[0] is True
    assert _via_wake(_env("\u2192 Brain: orch layer"), chat="brain")[0] is False


@pytest.mark.parametrize("text", [
    '{"success":false} The message went out once.',
    "error: could not deliver the message. The message went out once.",
    "error: could not deliver",
])
def test_negative_cases_stay_rejected(monkeypatch, text):
    out = _env(text)
    assert _via_wake(out)[0] is False
    assert _via_cli(monkeypatch, out) is False


def _via_cli_chat(monkeypatch, stdout, chat):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    return ClaudeCli("claude.exe").wake(chat, "hi")


def test_arrow_is_judged_through_claude_cli(monkeypatch):
    out = _env("→ Brain: orch layer")
    assert _via_cli_chat(monkeypatch, out, "Brain: orch layer")[0] is True
    assert _via_cli_chat(monkeypatch, out, "brain")[0] is False


def test_cut_flag_with_fail_stays_rejected():
    assert _via_wake(_env('success":true, the run did not fail'))[0] is False
    assert _via_wake(_env('success":true, the run did not fail'), chat="brain")[0] is False


def test_stderr_detail_is_one_line():
    stdout = _env("\n".join(["alpha beta gamma " * 9, "delta epsilon " * 15, "zeta eta theta " * 16]))

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout,
                                           stderr="stderr one\nstderr two\nstderr three")

    ok, detail = wake(CHAT, "hi", "claude.exe", run=run)
    assert ok is False
    assert "\n" not in detail
    assert len(detail) <= 400


def test_row3_fallthrough_detail_is_one_line():
    text = "\n".join(["alpha beta gamma " * 9, "delta epsilon " * 15, "zeta eta theta " * 16])
    assert len(text) >= 600 and text.count("\n") == 2
    ok, detail = _via_wake(_env(text))
    assert ok is False
    assert "\n" not in detail
    assert len(detail) <= 400
    assert " | " in detail
