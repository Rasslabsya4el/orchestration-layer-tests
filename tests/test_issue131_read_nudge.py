"""Issue #131: the brain's read nudge (tools/hooks/brain_read_nudge.py).

Seam under test: the hook itself, run as a subprocess the way Claude Code runs it. A PreToolUse
payload goes in on stdin; a ``hookSpecificOutput`` JSON line (or nothing) comes out on stdout; one
log line lands in ``AGENTOPS_HOOK_LOG``. Transcripts are written by hand in the shape of Claude
Code's JSON lines (assistant ``tool_use`` blocks, user ``tool_result`` records).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "brain_read_nudge.py"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from tools.hooks.brain_read_nudge import neutral_scripts_from_settings  # noqa: E402

NUDGE_TEXT = ("third read in a row: hand the rest to researcher-find (where, list, grep) or "
              "researcher-how (how, why) with the issue number")

# Tool calls as (name, input). A call in a history is (tool_use_id, (name, input)).
READ = ("Read", {"file_path": "docs/agents/brain.md"})
GREP = ("Grep", {"pattern": "count_run", "path": "tools"})
GLOB = ("Glob", {"pattern": "tools/**/*.py"})
OUTCOME = ("Bash", {"command": "python -m tools.agentops outcome 131 --check agree"})
EDIT = ("Edit", {"file_path": "a.md", "old_string": "x", "new_string": "y"})
AGENT = ("Agent", {"prompt": "find x"})

ID_MODES = ["id_in_transcript", "id_not_in_transcript", "no_id"]


@pytest.fixture(autouse=True)
def _hook_log(tmp_path, monkeypatch):
    """Keep every test's log lines out of the real var/agentops log, and point the hook's project
    dir at an empty folder (no settings file: no neutral script) unless a test writes one."""
    path = tmp_path / "brain_nudge.log"
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "no-project"))
    return path


def _project_settings(tmp_path, monkeypatch, text: str) -> None:
    """Write config/orchestration.json (``text`` as is) under a tmp project dir and point the hook at it."""
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "config" / "orchestration.json").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))


STATUS = ("Bash", {"command": "python scripts/status_tool.py status"})


def _call_records(calls):
    """An assistant ``tool_use`` record and its ``tool_result`` record, per (id, (name, input))."""
    records = []
    for cid, (name, tool_input) in calls:
        records.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": cid, "name": name, "input": tool_input}]}})
        records.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": "ok"}]}})
    return records


def _transcript(tmp_path, records) -> Path:
    """Write the transcript: dicts are JSON-encoded, strings are written as raw lines. A brain chat's
    ``custom-title`` record comes first: the nudge is the brain's alone (#320), a titleless session gets none."""
    title = {"type": "custom-title", "customTitle": _load_settings()["brain_chat_title"]}
    lines = [json.dumps(title)] + [r if isinstance(r, str) else json.dumps(r) for r in records]
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _run(tmp_path, transcript, tool_name, tool_input, *, tool_use_id=None, agent_type=None,
         event="PreToolUse", stdin=None) -> subprocess.CompletedProcess:
    payload = {"hook_event_name": event, "tool_name": tool_name, "tool_input": tool_input,
               "transcript_path": str(transcript), "session_id": "sess-131"}
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    text = stdin if stdin is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK_SCRIPT)], input=text, capture_output=True,
                          text=True, timeout=20)


def _step(tmp_path, history, current, mode):
    """Run the hook on ``current`` after ``history``.

    ``id_in_transcript``: the current call's record is in the transcript and its id is in the
    payload. ``id_not_in_transcript``: the id is in the payload, the record is not written yet.
    ``no_id``: no id in the payload, the record is not written yet.
    """
    cid, (name, tool_input) = current
    calls = list(history) + ([current] if mode == "id_in_transcript" else [])
    transcript = _transcript(tmp_path, _call_records(calls))
    tool_use_id = None if mode == "no_id" else cid
    return _run(tmp_path, transcript, name, tool_input, tool_use_id=tool_use_id)


def _nudged(proc: subprocess.CompletedProcess) -> bool:
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() != ""


def _log_rows(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def _reads(*ids):
    return [(cid, READ) for cid in ids]


# --- the nudge on the third read -------------------------------------------------------------

@pytest.mark.parametrize("mode", ID_MODES)
def test_nudge_on_third_read_only(tmp_path, mode):
    r1, r2, r3, r4 = _reads("r1", "r2", "r3", "r4")
    assert not _nudged(_step(tmp_path, [], r1, mode))
    assert not _nudged(_step(tmp_path, [r1], r2, mode))
    assert _nudged(_step(tmp_path, [r1, r2], r3, mode))
    assert not _nudged(_step(tmp_path, [r1, r2, r3], r4, mode))


@pytest.mark.parametrize("mode", ID_MODES)
def test_nudge_json_shape(tmp_path, mode):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    proc = _step(tmp_path, [r1, r2], r3, mode)
    out = json.loads(proc.stdout)
    assert list(out) == ["hookSpecificOutput"]
    hso = out["hookSpecificOutput"]
    assert hso == {"hookEventName": "PreToolUse", "additionalContext": NUDGE_TEXT}
    assert "permissionDecision" not in hso
    assert "third read in a row" in hso["additionalContext"]


@pytest.mark.parametrize("mode", ID_MODES)
def test_nudge_is_logged_once_per_run(tmp_path, _hook_log, mode):
    r1, r2, r3, r4 = _reads("r1", "r2", "r3", "r4")
    _step(tmp_path, [], r1, mode)
    _step(tmp_path, [r1], r2, mode)
    assert _log_rows(_hook_log) == []
    _step(tmp_path, [r1, r2], r3, mode)
    _step(tmp_path, [r1, r2, r3], r4, mode)
    rows = _log_rows(_hook_log)
    assert len(rows) == 1
    assert rows[0]["action"] == "read-nudge"
    assert rows[0]["event"] == "PreToolUse"
    assert rows[0]["session"] == "sess-131"
    assert rows[0]["reads"] == 3


@pytest.mark.parametrize("mode", ID_MODES)
def test_grep_and_glob_count_as_reads(tmp_path, mode):
    r1, = _reads("r1")
    g2 = ("g2", GREP)
    g3 = ("g3", GLOB)
    assert _nudged(_step(tmp_path, [r1, g2], g3, mode))


# --- neutral calls: the outcome command neither counts nor resets ----------------------------

@pytest.mark.parametrize("mode", ID_MODES)
def test_outcome_between_reads_neither_counts_nor_resets(tmp_path, mode):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    out = ("o1", OUTCOME)
    assert _nudged(_step(tmp_path, [r1, out, r2], r3, mode))
    assert _nudged(_step(tmp_path, [r1, r2, out], ("r3", READ), mode))


# --- other calls reset the run ---------------------------------------------------------------

@pytest.mark.parametrize("mode", ID_MODES)
def test_edit_resets_the_run(tmp_path, mode):
    r1, r2, r3, r4, r5, r6 = _reads("r1", "r2", "r3", "r4", "r5", "r6")
    edit = ("e1", EDIT)
    # the fifth call: the first read after the edit, no nudge
    assert not _nudged(_step(tmp_path, [r1, r2, r3, edit], r4, mode))
    # three reads after the edit: the nudge is on the third of them, not the sixth overall
    assert _nudged(_step(tmp_path, [r1, r2, r3, edit, r4, r5], r6, mode))


@pytest.mark.parametrize("mode", ID_MODES)
def test_agent_call_resets_the_run(tmp_path, mode):
    r1, r2, r3, r4 = _reads("r1", "r2", "r3", "r4")
    agent = ("a1", AGENT)
    assert not _nudged(_step(tmp_path, [r1, r2, agent, r3], r4, mode))
    assert _nudged(_step(tmp_path, [r1, r2, agent, r3, r4], ("r5", READ), mode))


# --- skips: background sessions, other agents, non-reads --------------------------------------

def test_background_session_is_silent_and_unlogged(tmp_path, _hook_log):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    transcript = _transcript(tmp_path, [{"sessionKind": "bg"}]
                             + _call_records([r1, r2, r3]))
    proc = _run(tmp_path, transcript, "Read", READ[1], tool_use_id="r3")
    assert _nudged(proc) is False
    assert _log_rows(_hook_log) == []


def test_agent_orch_payload_is_silent_and_unlogged(tmp_path, _hook_log):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    transcript = _transcript(tmp_path, _call_records([r1, r2, r3]))
    proc = _run(tmp_path, transcript, "Read", READ[1], tool_use_id="r3", agent_type="orch")
    assert not _nudged(proc)
    assert _log_rows(_hook_log) == []


def test_non_read_tool_is_silent_and_unlogged(tmp_path, _hook_log):
    r1, r2 = _reads("r1", "r2")
    transcript = _transcript(tmp_path, _call_records([r1, r2]))
    for name, tool_input in (("Edit", EDIT[1]), ("Agent", AGENT[1]),
                             # tagged: the brain transcript's untagged nudge (#178) stays out of this test
                             ("Bash", {"command": "# purpose: ticket\ngit commit -m x"})):
        proc = _run(tmp_path, transcript, name, tool_input, tool_use_id="r3")
        assert proc.returncode == 0
        assert proc.stdout == ""
    assert _log_rows(_hook_log) == []


def test_only_pre_tool_use_is_handled(tmp_path, _hook_log):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    transcript = _transcript(tmp_path, _call_records([r1, r2, r3]))
    proc = _run(tmp_path, transcript, "Read", READ[1], tool_use_id="r3", event="PostToolUse")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert _log_rows(_hook_log) == []


def test_bad_stdin_exits_zero_and_silent(tmp_path):
    proc = _run(tmp_path, tmp_path / "missing.jsonl", "Read", READ[1], stdin="not json")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_missing_transcript_is_silent(tmp_path):
    proc = _run(tmp_path, tmp_path / "missing.jsonl", "Read", READ[1], tool_use_id="r1")
    assert proc.returncode == 0
    assert proc.stdout == ""


# --- neutral scripts: read from the project's config/orchestration.json ------------------------

@pytest.mark.parametrize("mode", ID_MODES)
def test_settings_neutral_script_neither_counts_nor_resets(tmp_path, monkeypatch, mode):
    _project_settings(tmp_path, monkeypatch,
                      json.dumps({"read_run_neutral_scripts": ["scripts/status_tool.py"]}))
    r1, r2, r3 = _reads("r1", "r2", "r3")
    assert _nudged(_step(tmp_path, [r1, ("s1", STATUS), r2], r3, mode))


@pytest.mark.parametrize("mode", ID_MODES)
def test_script_resets_the_run_without_settings(tmp_path, mode):
    r1, r2, r3 = _reads("r1", "r2", "r3")
    assert not _nudged(_step(tmp_path, [r1, ("s1", STATUS), r2], r3, mode))


@pytest.mark.parametrize("settings", [
    "{not json",
    json.dumps({}),
    json.dumps({"read_run_neutral_scripts": "scripts/status_tool.py"}),
    json.dumps({"read_run_neutral_scripts": [1, None]}),
    json.dumps(["scripts/status_tool.py"]),
])
def test_bad_settings_give_no_neutral_script(tmp_path, monkeypatch, settings):
    _project_settings(tmp_path, monkeypatch, settings)
    assert neutral_scripts_from_settings() == ()


def test_missing_settings_file_gives_no_neutral_script(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "no-project"))
    assert neutral_scripts_from_settings() == ()


def test_loader_keeps_only_non_empty_string_entries(tmp_path, monkeypatch):
    _project_settings(tmp_path, monkeypatch, json.dumps(
        {"read_run_neutral_scripts": ["scripts/status_tool.py", 3, ""]}))
    assert neutral_scripts_from_settings() == ("scripts/status_tool.py",)


def test_without_project_dir_the_repo_root_settings_are_read(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    settings = json.loads((PROJECT_ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    expected = tuple(s for s in settings.get("read_run_neutral_scripts", [])
                     if isinstance(s, str) and s)
    assert neutral_scripts_from_settings() == expected


# --- the transcript tail: the last 1 MB only ---------------------------------------------------

@pytest.mark.parametrize("mode", ID_MODES)
def test_reads_after_a_big_line_are_counted(tmp_path, mode):
    """A line of 1.5 MB before the reads: the first, partial, line of the tail is dropped and the
    three reads in the last megabyte still count."""
    big = json.dumps({"type": "attachment", "blob": "x" * 1_500_000})
    r1, r2, r3 = _reads("r1", "r2", "r3")
    history = [r1, r2]
    calls = history + ([r3] if mode == "id_in_transcript" else [])
    transcript = _transcript(tmp_path, [big] + _call_records(calls))
    tool_use_id = None if mode == "no_id" else "r3"
    assert _nudged(_run(tmp_path, transcript, "Read", READ[1], tool_use_id=tool_use_id))


@pytest.mark.parametrize("mode", ID_MODES)
def test_reads_before_the_last_megabyte_are_not_counted(tmp_path, mode):
    """Two reads, then a 1.5 MB line: the reads sit outside the tail, so this is the first read."""
    big = json.dumps({"type": "attachment", "blob": "x" * 1_500_000})
    r1, r2, r3 = _reads("r1", "r2", "r3")
    history = [r1, r2]
    calls = history + ([r3] if mode == "id_in_transcript" else [])
    transcript = _transcript(tmp_path, _call_records(calls) + [big])
    tool_use_id = None if mode == "no_id" else "r3"
    assert not _nudged(_run(tmp_path, transcript, "Read", READ[1], tool_use_id=tool_use_id))
