"""Issue #320, unit 1: the shared session-role resolver (`tools/hooks/session_role.py`).

Every hook rule names the roles it binds; `fires` decides whether a session of a role runs it. Rows: the
`agent_type` names of subagents, the `agentSetting` launch fact of a main session's transcript head (`--agent orch`,
the autopilot's `--agent brain`), the brain chat title (payload `session_title`, else the transcript's last
`custom-title` record, read from its 256 KB tail first), the `brain_chats` titles of the settings file
(`AGENTOPS_HOOK_SETTINGS`), a cloud session with no title (`unknown`, never brain), an unreadable transcript
(`unknown`), and the typo guard of `fires`.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.hooks import session_role as session_role_module  # noqa: E402
from tools.hooks.session_role import ROLES, fires, session_role  # noqa: E402

BRAIN = "Brain: orch layer"
KB = 1024
MB = 1024 * 1024


def _write(tmp_path: Path, lines: list[str], name: str = "chat.jsonl") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _user() -> str:
    return json.dumps({"type": "user", "message": "hi"})


def _title(value: str) -> str:
    return json.dumps({"type": "custom-title", "customTitle": value, "sessionId": "s"})


def _setting(value: str) -> str:
    return json.dumps({"agentSetting": value, "sessionId": "s"})


def _padding(total_bytes: int) -> list[str]:
    """Assistant records of about ``total_bytes`` in all, none of them a custom-title."""
    line = json.dumps({"type": "assistant", "message": "x" * 1000})
    return [line] * (total_bytes // (len(line) + 1) + 1)


def _settings(tmp_path: Path, brain_chats: dict) -> Path:
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps({"repo": "x/y", "brain_chat_title": BRAIN, "brain_chats": brain_chats}),
                    encoding="utf-8")
    return path


class _CountingFile:
    """A binary file that counts the bytes its reads hand back (the title read's cost)."""

    def __init__(self, fh, counter: list[int]):
        self._fh = fh
        self._counter = counter

    def seek(self, *args):
        return self._fh.seek(*args)

    def read(self, *args):
        data = self._fh.read(*args)
        self._counter[0] += len(data)
        return data

    def __iter__(self):
        for line in self._fh:
            self._counter[0] += len(line)
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()


def test_roles_tuple():
    assert ROLES == ("brain", "orch", "worker", "reviewer", "researcher", "unblocker", "any")


@pytest.mark.parametrize(("agent_type", "role"), [
    ("worker", "worker"),
    ("worker-strong", "worker"),
    ("reviewer-spec", "reviewer"),
    ("researcher-find", "researcher"),
    ("orch", "orch"),
    ("brain", "brain"),
    ("unblocker", "unblocker"),
    ("odd", "unknown"),
])
def test_agent_type_names_the_role(agent_type, role):
    assert session_role({"agent_type": agent_type}, titles=()) == role


@pytest.mark.parametrize(("setting", "role"), [
    ("orch", "orch"),
    ("brain", "brain"),
    ("worker-strong", "worker"),
    ("unblocker", "unblocker"),
    ("odd", "unknown"),
])
def test_agentsetting_names_the_role_of_a_payload_without_agent_type(tmp_path, setting, role):
    payload = {"transcript_path": _write(tmp_path, [_setting(setting), _user()])}
    assert session_role(payload, titles=()) == role


def test_agentsetting_wins_over_the_title(tmp_path):
    payload = {"session_title": BRAIN, "transcript_path": _write(tmp_path, [_setting("orch"), _user()])}
    assert session_role(payload, titles=(BRAIN,)) == "orch"


def test_agent_type_wins_over_agentsetting(tmp_path):
    # a subagent's transcript can carry its parent's agentSetting (orch); its own agent_type decides
    payload = {"agent_type": "worker", "transcript_path": _write(tmp_path, [_setting("orch"), _user()])}
    assert session_role(payload, titles=()) == "worker"


def test_agent_type_wins_over_a_brain_title():
    payload = {"agent_type": "worker", "session_title": BRAIN}
    assert session_role(payload, titles=(BRAIN,)) == "worker"


def test_brain_title_from_payload():
    assert session_role({"session_title": BRAIN}, titles=(BRAIN,)) == "brain"


def test_brain_title_from_transcript_custom_title(tmp_path):
    payload = {"transcript_path": _write(tmp_path, [_user(), _title(BRAIN)])}
    assert session_role(payload, titles=(BRAIN,)) == "brain"


def test_last_custom_title_wins(tmp_path):
    renamed_away = {"transcript_path": _write(tmp_path, [_user(), _title(BRAIN), _title("Renamed")])}
    assert session_role(renamed_away, titles=(BRAIN,)) == "unknown"
    renamed_to = {"transcript_path": _write(tmp_path, [_user(), _title("Old"), _title(BRAIN)], "b.jsonl")}
    assert session_role(renamed_to, titles=(BRAIN,)) == "brain"


def test_title_in_first_kb_with_300kb_of_records_after_it(tmp_path):
    lines = [_user(), _title(BRAIN)] + _padding(300 * KB)
    payload = {"transcript_path": _write(tmp_path, lines)}
    assert session_role(payload, titles=(BRAIN,)) == "brain"


def test_last_title_in_the_tail_wins_over_an_older_one_in_the_head(tmp_path):
    old_then_brain = [_title("Old")] + _padding(300 * KB) + [_title(BRAIN)]
    assert session_role({"transcript_path": _write(tmp_path, old_then_brain)}, titles=(BRAIN,)) == "brain"
    brain_then_renamed = [_title(BRAIN)] + _padding(300 * KB) + [_title("Renamed")]
    renamed = {"transcript_path": _write(tmp_path, brain_then_renamed, "b.jsonl")}
    assert session_role(renamed, titles=(BRAIN,)) == "unknown"


def test_title_read_stays_in_the_tail_of_a_multi_mb_transcript(tmp_path, monkeypatch):
    path = _write(tmp_path, _padding(3 * MB) + [_title(BRAIN)])
    counter = [0]
    real_open = builtins.open
    monkeypatch.setattr(session_role_module, "open",
                        lambda file, mode="r", *a, **k: _CountingFile(real_open(file, mode, *a, **k), counter),
                        raising=False)
    assert session_role({"transcript_path": path}, titles=(BRAIN,)) == "brain"
    assert counter[0] < MB, f"read {counter[0]} bytes of a 3 MB transcript for its title"


def test_brain_chats_title_is_brain(tmp_path, monkeypatch):
    settings = _settings(tmp_path, {"orchestration": "Brain: Sample"})
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(settings))
    assert session_role({"session_title": "Brain: Sample"}) == "brain"
    assert session_role({"session_title": "Some chat"}) == "unknown"


def test_missing_settings_gives_unknown_not_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(tmp_path / "missing.json"))
    assert session_role({"session_title": BRAIN}) == "unknown"


def test_no_title_no_agent_type_is_unknown(tmp_path):
    assert session_role({}, titles=(BRAIN,)) == "unknown"
    assert session_role({"transcript_path": _write(tmp_path, [_user()])}, titles=(BRAIN,)) == "unknown"


def test_unreadable_transcript_is_unknown(tmp_path):
    missing = str(tmp_path / "no-such-dir" / "chat.jsonl")
    assert session_role({"transcript_path": missing}, titles=(BRAIN,)) == "unknown"
    assert session_role({"transcript_path": ""}, titles=(BRAIN,)) == "unknown"
    assert session_role({"transcript_path": None}, titles=(BRAIN,)) == "unknown"


def test_fires_brain_rule_only_for_the_brain_session():
    brain_session = {"session_title": BRAIN}
    orch_session = {"agent_type": "orch"}
    worker_session = {"agent_type": "worker"}
    cloud_session = {}  # a cloud session with no title and no agent_type
    titles = (BRAIN,)
    assert fires(("brain",), brain_session, titles=titles) is True
    assert fires(("brain",), orch_session, titles=titles) is False
    assert fires(("brain",), worker_session, titles=titles) is False
    assert fires(("brain",), cloud_session, titles=titles) is False


def test_fires_any_rule_for_every_session():
    assert fires(("any",), {}) is True
    assert fires(("any",), {"agent_type": "odd"}) is True


def test_untitled_session_does_not_fire_rules_of_the_named_roles():
    named_roles = ("brain", "orch", "worker", "reviewer", "researcher", "unblocker")
    assert fires(named_roles, {}, titles=(BRAIN,)) is False


@pytest.mark.parametrize("roles", [("bogus",), ("brain", "brian"), ("unknown",)])
def test_fires_raises_on_a_role_name_not_in_roles(roles):
    with pytest.raises(ValueError):
        fires(roles, {})


def test_fires_accepts_any_alongside_named_roles():
    assert fires(("any", "brain"), {}) is True
