"""#320 unit 2: the brain-only rules of three hooks fire only for a brain session.

Seam: each hook's ``run(payload)`` (the entry Claude Code's stdin payload reaches), with the settings, log and
marker folders redirected by env into tmp. The role of the session comes from ``tools/hooks/session_role.py``.

Rows:
1.  a titled brain session gets every rule of ``brain_reset_nudge`` (first-prompt hand-off, reply check,
    context-size nudge, wayfinder-close nudge), ``brain_read_nudge`` (untagged call; #368 removed the third read) and
    ``brain_state_inject`` (the hand-off injection).
1b. the same hooks give 0 output for an ORCH session (``agentSetting`` orch), a worker subagent
    (``agent_type`` worker) and a session with no title.
3.  the 21.10 audit-session replay: a cloud session (no ``session_title``, no ``custom-title``, no
    ``agentSetting``) gets 0 text from ``brain_reset_nudge`` (UserPromptSubmit and PostToolUse),
    ``brain_read_nudge`` and ``brain_state_inject``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from tools.hooks import brain_read_nudge, brain_reset_nudge, brain_state_inject  # noqa: E402

TITLE = _load_settings()["brain_chat_title"]
AREA_TITLE = "Brain: fake area"
BLOCK = "Reply check:\n- Answer first."
READ = ("Read", {"file_path": "x.py"})
SHELL = ("Bash", {"command": "echo hi"})


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    settings = {"repo": "o/n", "brain_chat_title": TITLE, "brain_chats": {"area:x": AREA_TITLE}, "context_line": 1000,
                "context_ceiling": 5000, "claude_projects_dir": "x", "bridge_dir": str(tmp_path / "b"),
                "bridge_lock": "var/locks/bridge.lock", "local_modules": []}
    for path in (tmp_path / "settings.json", project / "config" / "orchestration.json"):
        path.write_text(json.dumps(settings), encoding="utf-8")
    (project / "docs").mkdir()
    (project / "docs" / "STATE.md").write_text("# S\n\nUpdated (brain) 23.10, entry\n", encoding="utf-8")
    brain_md = tmp_path / "brain.md"
    brain_md.write_text(f"<!-- reply-check:start -->\n{BLOCK}\n<!-- reply-check:end -->\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / "hook.log"))
    monkeypatch.setenv("AGENTOPS_BRAIN_MD", str(brain_md))
    monkeypatch.setenv("AGENTOPS_BRAIN_INJECT_DIR", str(tmp_path / "inject"))
    monkeypatch.setenv("AGENTOPS_SESSION_PAYLOAD_LOG", str(tmp_path / "payloads.jsonl"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(brain_reset_nudge, "_gh_labels", lambda n, timeout=5: ["wayfinder:map"])
    monkeypatch.setattr(brain_state_inject, "_git", lambda *a, **k: None)


@pytest.fixture
def reply_check_on(monkeypatch):
    """#347: the reply check's switch is off by default; the checks that assert the block run with it on."""
    monkeypatch.setattr(brain_reset_nudge, "REPLY_CHECK_ENABLED", True, raising=True)


def _transcript(tmp_path: Path, kind: str, *, first_prompt: bool, tools: int = 0) -> Path:
    """A transcript of a session ``kind``: brain (custom-title), area (custom-title of a ``brain_chats`` chat),
    orch (agentSetting orch), autopilot (agentSetting brain, no title), worker and cloud (neither). ``first_prompt`` leaves out every assistant turn; otherwise one assistant turn with a 2000 token
    usage (over the test's context line of 1000) and ``tools`` read calls follow."""
    first = {"type": "user", "message": {"content": "hi"}}
    if kind in ("orch", "autopilot"):
        first["agentSetting"] = "orch" if kind == "orch" else "brain"
    records = [first]
    if kind in ("brain", "area"):
        records.append({"type": "custom-title", "customTitle": TITLE if kind == "brain" else AREA_TITLE,
                        "sessionId": "s1"})
    if not first_prompt:
        records.append({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 2000}}})
        for i in range(tools):
            records.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": f"f{i}.py"}}]}})
            records.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"r{i}", "content": "ok"}]}})
    path = tmp_path / f"{kind}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _payload(kind: str, event: str, transcript: Path, **extra) -> dict:
    payload = {"hook_event_name": event, "transcript_path": str(transcript), "session_id": f"s-{kind}",
               "cwd": str(transcript.parent)}
    if kind == "worker":
        payload["agent_type"] = "worker"
    payload.update(extra)
    return payload


def _text(output: dict | None) -> str:
    return "" if output is None else output["hookSpecificOutput"]["additionalContext"]


# -- brain_reset_nudge -------------------------------------------------------------------------------


def test_row1_reset_nudge_brain_gets_title_text_reply_check_and_size_nudge(tmp_path, reply_check_on):
    first = _text(brain_reset_nudge.run(_payload("brain", "UserPromptSubmit",
                                                 _transcript(tmp_path, "brain", first_prompt=True))))
    assert "you are the brain" in first and BLOCK in first
    later = _text(brain_reset_nudge.run(_payload("brain", "UserPromptSubmit",
                                                 _transcript(tmp_path, "brain", first_prompt=False))))
    assert BLOCK in later and "over the context" in later


def test_row1_reset_nudge_brain_gets_the_wayfinder_close_nudge(tmp_path):
    payload = _payload("brain", "PostToolUse", _transcript(tmp_path, "brain", first_prompt=False),
                       tool_response={"stdout": "✓ Closed issue o/n#5"})
    assert "A wayfinder ticket closed (#5)" in _text(brain_reset_nudge.run(payload))


@pytest.mark.parametrize("kind", ("orch", "worker", "cloud"))
def test_row1b_reset_nudge_other_sessions_get_nothing(tmp_path, kind):
    for first_prompt in (True, False):
        transcript = _transcript(tmp_path, kind, first_prompt=first_prompt)
        assert brain_reset_nudge.run(_payload(kind, "UserPromptSubmit", transcript)) is None
    payload = _payload(kind, "PostToolUse", transcript, tool_response={"stdout": "✓ Closed issue o/n#5"})
    assert brain_reset_nudge.run(payload) is None


def test_row3_audit_session_replay_reset_nudge_cloud_session_0_text(tmp_path):
    """A cloud session with no session_title, no custom-title and no agentSetting, over the context line."""
    transcript = _transcript(tmp_path, "cloud", first_prompt=False)
    payload = _payload("cloud", "UserPromptSubmit", transcript)
    assert "session_title" not in payload
    assert _text(brain_reset_nudge.run(payload)) == ""
    post = _payload("cloud", "PostToolUse", transcript, tool_response={"stdout": "✓ Closed issue o/n#5"})
    assert _text(brain_reset_nudge.run(post)) == ""


# -- brain_read_nudge --------------------------------------------------------------------------------


def _pre_tool(kind: str, tmp_path: Path, tool: tuple[str, dict], tools: int) -> dict:
    return _payload(kind, "PreToolUse", _transcript(tmp_path, kind, first_prompt=False, tools=tools),
                    tool_name=tool[0], tool_input=tool[1], tool_use_id="cur")


def test_row1_read_nudge_brain_gets_untagged_nudge_and_no_third_read_text(tmp_path):
    assert brain_read_nudge.run(_pre_tool("brain", tmp_path, READ, 2)) is None
    assert brain_read_nudge.UNTAGGED_TEXT in _text(brain_read_nudge.run(_pre_tool("brain", tmp_path, SHELL, 0)))


@pytest.mark.parametrize("kind", ("orch", "worker", "cloud"))
def test_read_nudge_orch_worker_and_cloud_sessions_get_nothing(tmp_path, kind):
    assert brain_read_nudge.run(_pre_tool(kind, tmp_path, READ, 2)) is None
    assert brain_read_nudge.run(_pre_tool(kind, tmp_path, SHELL, 0)) is None


# -- brain_state_inject ------------------------------------------------------------------------------


def _inject_payload(kind: str, event: str, tmp_path: Path, title: str | None) -> dict:
    payload = _payload(kind, event, _transcript(tmp_path, kind, first_prompt=True))
    if event == "SessionStart":
        payload["source"] = "startup"
    if title is not None:
        payload["session_title"] = title
    return payload


@pytest.mark.parametrize("event", ("SessionStart", "UserPromptSubmit", "PreToolUse"))
def test_row1_state_inject_brain_gets_the_hand_off(tmp_path, event):
    out = brain_state_inject.run(_inject_payload("brain", event, tmp_path, TITLE))
    assert "This chat is the brain" in _text(out)


@pytest.mark.parametrize("event", ("SessionStart", "UserPromptSubmit", "PreToolUse"))
@pytest.mark.parametrize("kind", ("orch", "worker"))
def test_state_inject_orch_and_worker_with_a_brain_title_get_nothing(tmp_path, kind, event):
    """A subagent's or an ORCH's payload can carry the brain's title; its own role still wins."""
    assert brain_state_inject.run(_inject_payload(kind, event, tmp_path, TITLE)) is None


@pytest.mark.parametrize("event", ("SessionStart", "UserPromptSubmit", "PreToolUse"))
def test_state_inject_cloud_session_without_a_title_gets_nothing(tmp_path, event):
    assert brain_state_inject.run(_inject_payload("cloud", event, tmp_path, None)) is None


# -- review additions: brain_chats titles, the autopilot, the payload-only title, the first prompt ---------


@pytest.mark.parametrize("event", ("SessionStart", "UserPromptSubmit", "PreToolUse"))
def test_area_chat_title_from_brain_chats_fires_state_inject(tmp_path, event):
    out = brain_state_inject.run(_inject_payload("area", event, tmp_path, AREA_TITLE))
    assert "This chat is the brain" in _text(out)


def test_area_chat_title_from_brain_chats_fires_reset_and_read_rules(tmp_path, reply_check_on):
    later = _text(brain_reset_nudge.run(_payload("area", "UserPromptSubmit",
                                                 _transcript(tmp_path, "area", first_prompt=False))))
    assert BLOCK in later and "over the context" in later  # reply check and context-size nudge
    post = _payload("area", "PostToolUse", _transcript(tmp_path, "area", first_prompt=False),
                    tool_response={"stdout": "✓ Closed issue o/n#5"})
    assert "A wayfinder ticket closed (#5)" in _text(brain_reset_nudge.run(post))
    assert brain_read_nudge.run(_pre_tool("area", tmp_path, READ, 2)) is None
    assert brain_read_nudge.UNTAGGED_TEXT in _text(brain_read_nudge.run(_pre_tool("area", tmp_path, SHELL, 0)))


def test_autopilot_session_gets_the_title_free_rules_exactly_as_before(tmp_path):
    """``claude --agent brain`` (transcript head ``agentSetting: brain``, no ``custom-title``) is the brain by its
    role. Before #320 it got the rules that need no title and none of the ones that read the title; the same now:
    gets the context-size nudge and the wayfinder-close nudge (no third-read nudge since #368); gets no first-prompt
    hand-off, no reply check, no untagged-call nudge and no state injection (all four read the chat title)."""
    over = _text(brain_reset_nudge.run(_payload("autopilot", "UserPromptSubmit",
                                                _transcript(tmp_path, "autopilot", first_prompt=False))))
    assert "over the context" in over and BLOCK not in over
    first = brain_reset_nudge.run(_payload("autopilot", "UserPromptSubmit",
                                           _transcript(tmp_path, "autopilot", first_prompt=True)))
    assert first is None
    post = _payload("autopilot", "PostToolUse", _transcript(tmp_path, "autopilot", first_prompt=False),
                    tool_response={"stdout": "✓ Closed issue o/n#5"})
    assert "A wayfinder ticket closed (#5)" in _text(brain_reset_nudge.run(post))
    assert brain_read_nudge.run(_pre_tool("autopilot", tmp_path, READ, 2)) is None
    assert brain_read_nudge.run(_pre_tool("autopilot", tmp_path, SHELL, 0)) is None
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
        assert brain_state_inject.run(_inject_payload("autopilot", event, tmp_path, None)) is None


@pytest.mark.parametrize("event", ("SessionStart", "UserPromptSubmit", "PreToolUse"))
def test_state_inject_with_only_a_payload_title_and_no_custom_title_record_injects(tmp_path, event):
    """A cleared chat's SessionStart carries ``session_title`` before any transcript record names the title."""
    transcript = _transcript(tmp_path, "cloud", first_prompt=True)
    assert "custom-title" not in transcript.read_text(encoding="utf-8")
    out = brain_state_inject.run(_inject_payload("cloud", event, tmp_path, TITLE))
    assert "This chat is the brain" in _text(out)


def test_first_prompt_before_any_custom_title_record_gets_no_hand_off_as_before(tmp_path):
    """The first prompt of a chat whose ``custom-title`` record is not written yet: ``_brain_title_context`` and the
    reply check read the title from the transcript (``git show i320-merge:tools/hooks/brain_reset_nudge.py``: both
    return None on a transcript with no ``custom-title``), so nothing is injected whatever the payload's
    ``session_title`` says. The role gate changes none of it."""
    transcript = _transcript(tmp_path, "cloud", first_prompt=True)
    for extra in ({}, {"session_title": TITLE}):
        assert brain_reset_nudge.run(_payload("cloud", "UserPromptSubmit", transcript, **extra)) is None
