"""The hooks, scripts and agent prompts that touch issues name the tickets repo.

Sample settings: ``tickets_repo`` = owner/work, ``repo`` = owner/code. Every gh call a file makes is recorded
(a monkeypatched ``subprocess.run``, or the script's own runner) and judged: the tickets repo appears, the code
repo never does. Without the setting the tickets repo is the repo itself (today's behaviour).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tools.agentops import core_settings  # noqa: E402

WORK = "owner/work"
CODE = "owner/code"
AREAS = ["area:orchestration", "area:builds", "area:runs", "area:crafting"]
GOOD_TITLE = "Tracker guard judges the tickets repo"
HUMAN = "\n".join([
    "## For the human",
    "- **What:** the tracker guard reads the tickets repo.",
    "- **Goal:** tickets live in their own repo, the owner's word of 2026-10-23.",
    "- **Plan:** one setting names the repo; the guard reads it.",
    "- **Result:** a create on the tickets repo needs its area label; shown by a test.",
    "- **Undo:** two-way door (one revert); touches the guard only.",
    "",
    "## For the agent",
    "Spec line.",
    "Class: none (test fixture)",
    "Same object: none (test fixture)",
    "",
])


class Gh:
    """Records each argv handed to ``subprocess.run`` and answers with ``stdout`` (str or bytes)."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, args, *a, **kw):
        self.calls.append([str(x) for x in args])
        empty = b"" if isinstance(self.stdout, bytes) else ""
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr=empty)


def assert_tickets_only(calls):
    assert calls, "no gh call was made"
    for call in calls:
        text = " ".join(call)
        assert CODE not in text, text
        assert WORK in text, text


def _settings_file(tmp_path, tickets=None, areas=None):
    data = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    data["repo"] = CODE
    data.pop("tickets_repo", None)
    data.pop("notes_repo", None)
    if tickets is not None:
        data["tickets_repo"] = tickets
    if areas is not None:
        data["areas"] = areas
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def sample(tmp_path, monkeypatch):
    """The sample settings file (tickets_repo = owner/work), read by every core reader in these tests."""
    path = _settings_file(tmp_path, tickets=WORK)
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(path))
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(path))
    monkeypatch.setenv("TRACKER_GUARD_LOG", str(tmp_path / "guard.jsonl"))
    monkeypatch.delenv("AGENTOPS_HOOK_GH", raising=False)
    return path


# ---------------------------------------------------------------- tools/hooks/tracker_guard.py


def _guard(tmp_path, command):
    from tools.hooks import tracker_guard
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    out = tracker_guard.run(payload)
    return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None


def _body(tmp_path):
    """The body file's path with forward slashes: the guard splits the command POSIX-style."""
    path = tmp_path / "body.md"
    path.write_text(HUMAN, encoding="utf-8")
    return path.as_posix()


def test_guard_create_aimed_at_the_code_repo_is_not_judged_for_areas(sample, tmp_path, monkeypatch):
    settings = _settings_file(tmp_path, tickets=WORK, areas=AREAS)
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(settings))
    cmd = f'gh issue create -R {CODE} --title "{GOOD_TITLE}" --body-file {_body(tmp_path)}'
    assert _guard(tmp_path, cmd) is None


def test_guard_create_on_the_tickets_repo_needs_its_area_label(sample, tmp_path, monkeypatch):
    settings = _settings_file(tmp_path, tickets=WORK, areas=AREAS)
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(settings))
    cmd = f'gh issue create -R {WORK} --title "{GOOD_TITLE}" --body-file {_body(tmp_path)}'
    assert "exactly one area label" in _guard(tmp_path, cmd)


def test_guard_create_without_repo_flag_is_checked_as_the_tickets_repo(sample, tmp_path, monkeypatch):
    settings = _settings_file(tmp_path, tickets=WORK, areas=AREAS)
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(settings))
    cmd = f'gh issue create --title "{GOOD_TITLE}" --body-file {_body(tmp_path)}'
    assert "exactly one area label" in _guard(tmp_path, cmd)
    ok = f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {_body(tmp_path)}'
    assert _guard(tmp_path, ok) is None


def test_guard_without_the_setting_keeps_today_repo_rule(tmp_path, monkeypatch):
    settings = _settings_file(tmp_path, tickets=None, areas=AREAS)
    monkeypatch.setenv("TRACKER_GUARD_SETTINGS", str(settings))
    monkeypatch.setenv("TRACKER_GUARD_LOG", str(tmp_path / "guard.jsonl"))
    body = _body(tmp_path)
    # the repo itself: judged as before
    same = f'gh issue create -R {CODE} --title "{GOOD_TITLE}" --body-file {body}'
    assert "exactly one area label" in _guard(tmp_path, same)
    # another repo: not judged for areas, as before
    other = f'gh issue create -R owner/other --title "{GOOD_TITLE}" --body-file {body}'
    assert _guard(tmp_path, other) is None


# ---------------------------------------------------------------- tools/hooks/autopilot_checkpoint.py


def test_autopilot_checkpoint_reads_issue_comments_from_the_tickets_repo(sample, monkeypatch):
    from tools.hooks import autopilot_checkpoint
    gh = Gh('{"comments": []}')
    monkeypatch.setattr(autopilot_checkpoint.subprocess, "run", gh)
    autopilot_checkpoint._gh_comments(7)
    assert_tickets_only(gh.calls)


# ---------------------------------------------------------------- tools/hooks/brain_reset_nudge.py


def test_autopilot_checkpoint_failed_read_tells_the_session_the_tickets_repo(sample, monkeypatch):
    from tools.hooks import autopilot_checkpoint
    monkeypatch.setattr(autopilot_checkpoint, "_gh_comments", lambda n, timeout=5: None)
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_title": "autopilot #7"}
    out = autopilot_checkpoint.handle_session_start(payload)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert f"repos/{WORK}/issues/7/comments" in context, context
    assert CODE not in context, context


def test_brain_reset_nudge_reads_issue_labels_from_the_tickets_repo(sample, monkeypatch):
    from tools.hooks import brain_reset_nudge
    gh = Gh('{"labels": []}')
    monkeypatch.setattr(brain_reset_nudge.subprocess, "run", gh)
    brain_reset_nudge._gh_labels(7)
    assert_tickets_only(gh.calls)


# ---------------------------------------------------------------- scripts/audit_dirty_worktrees.py


def test_audit_dirty_worktrees_lists_issue_states_from_the_tickets_repo(sample, monkeypatch):
    adw = _load_script(monkeypatch, "audit_dirty_worktrees_323", "audit_dirty_worktrees.py")
    gh = Gh("[]")
    monkeypatch.setattr(adw.subprocess, "run", gh)
    adw.load_issue_states()
    assert_tickets_only(gh.calls)


# ---------------------------------------------------------------- scripts/tracker_streams.py


def _plan(tmp_path):
    plan = {"repo": CODE, "maps": [{"map": 1, "rows": [
        {"kind": "stream", "title": "Stream one", "holds": "holds", "numbers": []}]}]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _load_script(monkeypatch, name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _streams_gh(monkeypatch, module):
    """Every api call of the streams script is recorded; the list call answers an empty page, the rest one issue."""
    calls: list[list[str]] = []

    def run(args, *a, **kw):
        calls.append([str(x) for x in args])
        out = b"[[]]" if "--paginate" in args else b'{"number": 9, "id": 99}'
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_tracker_streams_default_repo_is_the_tickets_repo(sample, tmp_path, monkeypatch, capsys):
    module = _load_script(monkeypatch, "tracker_streams_323", "tracker_streams.py")
    calls = _streams_gh(monkeypatch, module)
    assert module.main(["--plan", str(_plan(tmp_path))]) == 0
    assert calls, "no gh call was made"
    assert_tickets_only(calls)


def test_tracker_streams_repo_flag_still_wins(sample, tmp_path, monkeypatch, capsys):
    module = _load_script(monkeypatch, "tracker_streams_323", "tracker_streams.py")
    calls = _streams_gh(monkeypatch, module)
    assert module.main(["--plan", str(_plan(tmp_path)), "--repo", "owner/other"]) == 0
    assert calls and all("owner/other" in " ".join(c) for c in calls)


# ---------------------------------------------------------------- scripts/token_meter.py


def _token_meter_default(monkeypatch, name):
    return _load_script(monkeypatch, name, "token_meter.py")


def test_token_meter_default_issues_repo_is_the_tickets_repo(sample, monkeypatch):
    tm = _token_meter_default(monkeypatch, "token_meter_323_work")
    assert tm.DEFAULT_REPO == WORK
    assert tm.build_arg_parser().parse_args([]).repo == WORK
    seen: list[list[str]] = []

    def runner(args):
        seen.append([str(x) for x in args])
        return "[]"

    tm.fetch_closed_issues(runner=runner)
    assert seen and "--repo" in seen[0] and seen[0][seen[0].index("--repo") + 1] == WORK


def test_token_meter_without_the_setting_defaults_to_the_repo(tmp_path, monkeypatch):
    path = _settings_file(tmp_path, tickets=None)
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    tm = _token_meter_default(monkeypatch, "token_meter_323_code")
    assert tm.DEFAULT_REPO == CODE


# ---------------------------------------------------------------- .claude/agents/orch.md, unblocker.md


@pytest.mark.parametrize("name", ["orch.md", "unblocker.md"])
def test_agent_prompt_gh_issue_lines_name_the_tickets_repo(name):
    text = (ROOT / ".claude" / "agents" / name).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if "gh issue" in line]
    assert lines, f"{name} has no gh issue line"
    for line in lines:
        assert "-R <tickets_repo>" in line, line
