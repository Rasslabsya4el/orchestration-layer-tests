"""#316 unit 1: the brain state inject hook (`tools/hooks/brain_state_inject.py`).

Each test starts the hook the way Claude Code runs it: a plain `python` child, the SessionStart
payload on stdin, the output read from stdout. The git side is real: a bare `origin` and a clone
of it in tmp dirs, so `git fetch origin` and `git show origin/<branch>:docs/STATE.md` really run.

Rows of #316's acceptance (rows 1-4 here; row 5-6 are the wiring and the live clear, not this unit):
1. source `clear`, title "Brain: orch layer": the brain entry's full line, not the ORCH line.
2. `startup` and `compact` the same, 3 of 3.
3. a title that is not a brain title, and a `bg` session titled "Brain: orch layer": no output, 2 of 2.
4. fetch fails: the entry comes from the working copy and the text says so.
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


def _run(project: Path, payload: dict, log: Path, raw: bytes | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "AGENTOPS_HOOK_LOG": str(log)}
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, env=env, timeout=60)


def _context(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    out = json.loads(result.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return out["hookSpecificOutput"]["additionalContext"]


# -- row 1 and row 2: the brain's own entry, from clear / startup / compact --------------------------

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
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "SessionStart"
    assert rows[0]["action"] == "error"


def test_injection_is_logged_once_with_the_session_id(tmp_path: Path):
    """Row 6 reads `brain_nudge.log` for the injection line of the cleared session."""
    _, _, project = _make_repo(tmp_path, _state(BRAIN_LINE))
    log = tmp_path / "log.jsonl"
    _run(project, _payload(project), log)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["event"], r["action"], r["session"]) for r in rows] == [("SessionStart", "injected", "sess-316")]
