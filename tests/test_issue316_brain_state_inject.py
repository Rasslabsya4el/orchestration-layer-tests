"""#316 units 1 and 3: the brain state inject hook (`tools/hooks/brain_state_inject.py`).

Each test starts the hook the way Claude Code runs it: a plain `python` child, the hook payload on stdin,
the output read from stdout. The git side is real: a bare `origin` and a clone of it in tmp dirs, so
`git fetch origin` and `git show origin/<branch>:docs/STATE.md` really run.

Rows of #316's acceptance (rows 1-4 are unit 1; row 6 is the live clear, not a test):
1. source `clear`, title "Brain: orch layer": the brain entry's full line, not the ORCH line.
2. `startup` and `compact` the same, 3 of 3.
3. a title that is not a brain title, and a `bg` session titled "Brain: orch layer": no output, 2 of 2.
4. fetch fails: the entry comes from the working copy and the text says so.

Unit 3 (the fallback and the logs): the raw SessionStart payload is logged; every skip logs its reason;
PreToolUse takes the title from the transcript head when the payload has none; a brain session injects once;
a non-brain session stops trying after 5 events; `compact` re-injects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "brain_state_inject.py"

BRAIN_TITLE = "Brain: orch layer"
OLD_BRAIN_LINE = "Updated (brain) 22.10, an older brain entry that must lose to the newer one"
BRAIN_LINE = "Updated (brain) 23.10, #316 brain hand-off entry for the test"
ORCH_LINE = "Updated (ORCH) 23.10, an ORCH entry that must never be injected"
HEAD = "This chat is the brain; its hand-off entry from docs/STATE.md follows (docs/agents/brain.md, Reset)"
FETCH_NOTE = "git fetch failed, read from the working copy"
PAYLOAD_LOG_NAME = "session_start_payloads.jsonl"
INJECT_DIR_NAME = "brain_inject"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


def _state(*lines: str) -> str:
    return "# STATE\n\n" + "\n".join(lines) + "\n"


def _make_repo(tmp_path: Path, state_text: str, settings: dict | None = None) -> tuple[Path, Path, Path]:
    """A bare `origin` (its HEAD on main) with one commit holding docs/STATE.md and config/orchestration.json,
    a `work` clone that pushes new commits to it, and a `project` clone of it (the hook's cwd).
    Returns (origin, work, project)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    work = tmp_path / "work"
    _git(tmp_path, "init", str(work))
    (work / "docs").mkdir()
    (work / "docs" / "STATE.md").write_text(state_text, encoding="utf-8")
    (work / "config").mkdir()
    settings = settings if settings is not None else {"repo": "x/y", "brain_chat_title": BRAIN_TITLE}
    (work / "config" / "orchestration.json").write_text(json.dumps(settings), encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "state")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "origin", "main")
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    return origin, work, project


def _payload(project: Path, source: str = "clear", title: str | None = BRAIN_TITLE, **extra) -> dict:
    payload = {
        "hook_event_name": "SessionStart",
        "source": source,
        "session_id": "sess-316",
        "cwd": str(project),
        "transcript_path": str(project / "no-transcript-yet.jsonl"),
    }
    if title is not None:
        payload["session_title"] = title
    payload.update(extra)
    return payload


def _tool_payload(project: Path, transcript: Path, **extra) -> dict:
    """A PreToolUse payload with no `session_title`: the fallback has to read the title from the transcript."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "session_id": "sess-316",
        "cwd": str(project),
        "transcript_path": str(transcript),
    }
    payload.update(extra)
    return payload


def _run(project: Path, payload: dict, log: Path, raw: bytes | None = None) -> subprocess.CompletedProcess:
    base = log.parent
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "AGENTOPS_HOOK_LOG": str(log),
           "AGENTOPS_SESSION_PAYLOAD_LOG": str(base / PAYLOAD_LOG_NAME),
           "AGENTOPS_BRAIN_INJECT_DIR": str(base / INJECT_DIR_NAME)}
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, env=env, timeout=60)


def _context(result: subprocess.CompletedProcess, event: str = "SessionStart") -> str:
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    out = json.loads(result.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["hookEventName"] == event
    return out["hookSpecificOutput"]["additionalContext"]


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _skips(log: Path) -> list[str]:
    return [r["why"] for r in _rows(log) if r["action"] == "skip"]


def _transcript(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _marker(tmp_path: Path) -> Path:
    return tmp_path / INJECT_DIR_NAME / "sess-316.json"


def _marker_data(tmp_path: Path) -> dict:
    return json.loads(_marker(tmp_path).read_text(encoding="utf-8"))


# -- rows 1 and 2: the brain's own entry, from clear / startup / compact --------------------------------

@pytest.mark.parametrize("source", ["clear", "startup", "compact"])
def test_brain_chat_gets_its_own_entry_not_the_orch_line(tmp_path: Path, source: str):
    """Row 1 (source `clear`) and row 2 (`startup`, `compact`), 3 of 3: the text is the head line, a newline,
    then the whole first `Updated (brain)` line; the ORCH entry above it is not injected."""
    _, _, project = _make_repo(tmp_path, _state(ORCH_LINE, BRAIN_LINE))
    result = _run(project, _payload(project, source=source), tmp_path / "log.jsonl")
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"


def test_brain_chats_value_counts_as_a_brain_title(tmp_path: Path):
    """Every `brain_chats` value of config/orchestration.json is a brain title, not only `brain_chat_title`."""
    settings = {"repo": "x/y", "brain_chat_title": BRAIN_TITLE, "brain_chats": {"other": "Brain: other repo"}}
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE), settings)
    result = _run(project, _payload(project, title="Brain: other repo"), tmp_path / "log.jsonl")
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"


# -- row 3: no output for a non-brain chat or a background session, 2 of 2 -----------------------------

def test_other_title_gets_no_output(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, title="Some other chat"), log)
    assert result.returncode == 0
    assert result.stdout == b""


def test_background_session_titled_as_the_brain_gets_no_output(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, sessionKind="bg"), log)
    assert result.returncode == 0
    assert result.stdout == b""


# -- row 4: fetch fails -> the working copy, and the text says so -------------------------------------

def test_fetch_failure_reads_the_working_copy_and_says_so(tmp_path: Path):
    origin, _, project = _make_repo(tmp_path, _state(OLD_BRAIN_LINE))
    # an uncommitted edit in the working copy: the one the hook must read when the fetch fails
    (project / "docs" / "STATE.md").write_text(_state(ORCH_LINE, BRAIN_LINE), encoding="utf-8")
    _git(project, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project), log)
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}\n{FETCH_NOTE}"


# -- the fetch really runs: a newer origin entry wins over the stale clone ----------------------------

def test_fetch_success_reads_origin_not_the_stale_clone(tmp_path: Path):
    _, work, project = _make_repo(tmp_path, _state(OLD_BRAIN_LINE))
    # the clone is now behind: a newer entry lands on origin after the clone was made
    (work / "docs" / "STATE.md").write_text(_state(ORCH_LINE, BRAIN_LINE), encoding="utf-8")
    _git(work, "commit", "-am", "newer state")
    _git(work, "push", "origin", "main")
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project), log)
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"


# -- no brain entry, and the never-raises rule ---------------------------------------------------------

def test_no_brain_entry_says_so(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(ORCH_LINE))
    result = _run(project, _payload(project), tmp_path / "log.jsonl")
    assert _context(result) == "docs/STATE.md has no brain entry"


def test_bad_stdin_exits_zero_silently_and_logs_one_line(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, {}, log, raw=b"not json at all")
    assert result.returncode == 0
    assert result.stdout == b""
    rows = _rows(log)
    assert len(rows) == 1
    assert rows[0]["event"] == "SessionStart"
    assert rows[0]["action"] == "error"


def test_injection_is_logged_once_with_the_session_id(tmp_path: Path):
    """Row 6 reads `brain_nudge.log` for the injection line of the cleared session."""
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    _run(project, _payload(project), log)
    rows = _rows(log)
    assert [(r["event"], r["action"], r["session"]) for r in rows] == [("SessionStart", "injected", "sess-316")]


# -- unit 3 (a): the raw SessionStart payload is logged, with its keys ---------------------------------

def test_session_start_payload_is_logged_with_its_keys(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    payload = _payload(project, source="startup", extra_field="hello")
    _run(project, payload, tmp_path / "log.jsonl")
    rows = _rows(tmp_path / PAYLOAD_LOG_NAME)
    assert len(rows) == 1
    assert rows[0]["action"] == "payload"
    assert rows[0]["event"] == "SessionStart"
    assert rows[0]["session"] == "sess-316"
    assert rows[0]["keys"] == sorted(payload)
    assert rows[0]["raw"] == payload


def test_payload_raw_cuts_every_string_at_500_chars(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    _run(project, _payload(project, long_field="y" * 600), tmp_path / "log.jsonl")
    raw = _rows(tmp_path / PAYLOAD_LOG_NAME)[0]["raw"]
    assert raw["long_field"] == "y" * 500
    assert raw["session_title"] == BRAIN_TITLE


# -- unit 3 (a): one skip line per reason ----------------------------------------------------------------

def test_skip_event_is_logged(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, hook_event_name="Stop"), log)
    assert result.stdout == b""
    assert _skips(log) == ["event"]


def test_skip_source_is_logged(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, source="resume"), log)
    assert result.stdout == b""
    assert _skips(log) == ["source"]


def test_skip_bg_is_logged_and_not_payload_logged(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, sessionKind="bg"), log)
    assert result.stdout == b""
    assert _skips(log) == ["bg"]
    assert not (tmp_path / PAYLOAD_LOG_NAME).exists()


def test_skip_no_title_is_logged(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, title=None), log)
    assert result.stdout == b""
    assert _skips(log) == ["no-title"]


def test_skip_not_brain_title_is_logged(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project, title="Some other chat"), log)
    assert result.stdout == b""
    assert _skips(log) == ["not-brain-title"]


# -- unit 3 (b): the fallback on PreToolUse / UserPromptSubmit -----------------------------------------

def test_pretooluse_takes_the_title_from_the_transcript_head(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    transcript = _transcript(
        tmp_path,
        {"type": "user", "message": "first prompt"},
        {"type": "custom-title", "customTitle": BRAIN_TITLE, "sessionId": "sess-316"},
        {"type": "assistant", "message": "reply"},
    )
    log = tmp_path / "log.jsonl"
    result = _run(project, _tool_payload(project, transcript), log)
    assert _context(result, "PreToolUse") == f"{HEAD}\n{BRAIN_LINE}"
    assert [r["action"] for r in _rows(log)] == ["injected"]


def test_pretooluse_injects_once_per_session(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    transcript = _transcript(tmp_path, {"type": "custom-title", "customTitle": BRAIN_TITLE})
    log = tmp_path / "log.jsonl"
    first = _run(project, _tool_payload(project, transcript), log)
    second = _run(project, _tool_payload(project, transcript), log)
    assert _context(first, "PreToolUse") == f"{HEAD}\n{BRAIN_LINE}"
    assert second.returncode == 0
    assert second.stdout == b""
    assert [r["action"] for r in _rows(log)] == ["injected"]
    assert _marker_data(tmp_path) == {"injected": True, "tries": 1}


def test_marked_session_is_silent_on_tool_calls(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    _marker(tmp_path).parent.mkdir(parents=True)
    _marker(tmp_path).write_text(json.dumps({"injected": True, "tries": 0}), encoding="utf-8")
    transcript = _transcript(tmp_path, {"type": "custom-title", "customTitle": BRAIN_TITLE})
    log = tmp_path / "log.jsonl"
    result = _run(project, _tool_payload(project, transcript), log)
    assert result.stdout == b""
    assert _rows(log) == []


def test_bg_in_the_transcript_head_skips_the_fallback(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    transcript = _transcript(
        tmp_path,
        {"type": "custom-title", "customTitle": BRAIN_TITLE},
        {"type": "user", "sessionKind": "bg"},
    )
    log = tmp_path / "log.jsonl"
    result = _run(project, _tool_payload(project, transcript), log)
    assert result.stdout == b""
    assert _skips(log) == ["bg"]


def test_non_brain_session_stops_trying_after_five_events(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    transcript = _transcript(tmp_path, {"type": "custom-title", "customTitle": "Some other chat"})
    log = tmp_path / "log.jsonl"
    for _ in range(5):
        result = _run(project, _tool_payload(project, transcript), log)
        assert result.stdout == b""
    assert _marker_data(tmp_path) == {"injected": False, "tries": 5}
    assert _skips(log) == ["not-brain-title"] * 5

    sixth = _run(project, _tool_payload(project, transcript), log)
    assert sixth.returncode == 0
    assert sixth.stdout == b""
    assert _marker_data(tmp_path)["tries"] == 5
    assert _skips(log) == ["not-brain-title"] * 5


# -- unit 3 (a): compact always re-injects; SessionStart writes the marker ------------------------------

def test_compact_reinjects_after_the_marker_says_injected(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    _marker(tmp_path).parent.mkdir(parents=True)
    _marker(tmp_path).write_text(json.dumps({"injected": True, "tries": 0}), encoding="utf-8")
    result = _run(project, _payload(project, source="compact"), tmp_path / "log.jsonl")
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"
    assert _marker_data(tmp_path)["injected"] is True


def test_session_start_inject_writes_the_marker(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    result = _run(project, _payload(project, source="clear"), tmp_path / "log.jsonl")
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"
    assert _marker_data(tmp_path) == {"injected": True, "tries": 0}


def test_compact_without_session_title_reads_the_title_from_the_transcript(tmp_path: Path):
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    transcript = _transcript(tmp_path, {"type": "user"}, {"type": "custom-title", "customTitle": BRAIN_TITLE})
    payload = _payload(project, source="compact", title=None, transcript_path=str(transcript))
    _marker(tmp_path).parent.mkdir(parents=True)
    _marker(tmp_path).write_text(json.dumps({"injected": True, "tries": 1}), encoding="utf-8")
    result = _run(project, payload, tmp_path / "log.jsonl")
    assert _context(result) == f"{HEAD}\n{BRAIN_LINE}"
