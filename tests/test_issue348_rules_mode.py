"""#348 unit 2: the `rules` mode of the brain state inject hook (`tools/hooks/brain_state_inject.py rules`).

Each test starts the hook the way Claude Code runs it: a plain `python` child with the mode as its one
argument, the hook payload on stdin, the output read from stdout. The git side is real: a bare `origin` and a
clone of it in tmp dirs.

Acceptance rows covered (the issue's numbering):
5. `startup` / `clear` / `compact`, brain title, `origin/main` holds a 9,359-char docs/agents/brain.md: one
   string = head line + "\\n" + the file, <= 10,000, head names `origin/main`, no `git fetch` by this mode.
6. a non-brain title, a `bg` payload, the autopilot payload: no output, the hand-off mode's skip reasons.
7. title absent at SessionStart, then a UserPromptSubmit whose transcript head has the brain title: injected
   once on that event; a second PreToolUse injects nothing; the two modes' markers are separate.
8. a 10,500-char brain.md: head line + the read-it-now line, <= 10,000, one `rules-over-cap` row.
10. (wiring) is pinned in tests/test_issue316_wiring.py.
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
SESSION = "sess-348"
BRAIN_MD_PATH = "docs/agents/brain.md"
HEAD_ORIGIN = "This chat is the brain; its rule file docs/agents/brain.md follows, read from origin/main"
HEAD_WORKING = "This chat is the brain; its rule file docs/agents/brain.md follows, read from the working copy"
CAP = 10_000
HAND_OFF_HEAD = ("This chat is the brain; its hand-off entry from docs/STATE.md follows "
                 "(docs/agents/brain-reference.md, The start of a chat)")
BRAIN_LINE = "Updated (brain) 24.10, #348 rules mode test entry"


def _brain_md(length: int, fill: str = "r") -> str:
    """A brain.md fixture of exactly `length` characters (ASCII, so characters = bytes)."""
    head = "# Brain rules\n"
    body = (fill * (length - len(head) - 1)) + "\n"
    text = head + body
    assert len(text) == length
    return text


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


def _make_repo(tmp_path: Path, brain_md: str) -> tuple[Path, Path]:
    """A bare `origin` (HEAD on main) holding docs/agents/brain.md, docs/STATE.md and config/orchestration.json,
    a `work` clone that pushes to it, and a `project` clone of it (the hook's cwd). Returns (work, project)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    work = tmp_path / "work"
    _git(tmp_path, "init", str(work))
    (work / "docs" / "agents").mkdir(parents=True)
    (work / "docs" / "agents" / "brain.md").write_bytes(brain_md.encode("utf-8"))
    (work / "docs" / "STATE.md").write_text(f"# STATE\n\n{BRAIN_LINE}\n", encoding="utf-8")
    (work / "config").mkdir()
    (work / "config" / "orchestration.json").write_text(
        json.dumps({"repo": "x/y", "brain_chat_title": BRAIN_TITLE}), encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "rules")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "origin", "main")
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    return work, project


def _payload(project: Path, source: str = "clear", title: str | None = BRAIN_TITLE, **extra) -> dict:
    payload = {
        "hook_event_name": "SessionStart",
        "source": source,
        "session_id": SESSION,
        "cwd": str(project),
        "transcript_path": str(project / "no-transcript-yet.jsonl"),
    }
    if title is not None:
        payload["session_title"] = title
    payload.update(extra)
    return payload


def _event_payload(project: Path, event: str, transcript: Path, **extra) -> dict:
    """A UserPromptSubmit / PreToolUse payload with no `session_title`: the title comes from the transcript."""
    payload = {"hook_event_name": event, "session_id": SESSION, "cwd": str(project),
               "transcript_path": str(transcript)}
    payload.update(extra)
    return payload


def _run(project: Path, payload: dict, log: Path, *args: str) -> subprocess.CompletedProcess:
    base = log.parent
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "AGENTOPS_HOOK_LOG": str(log),
           "AGENTOPS_SESSION_PAYLOAD_LOG": str(base / "session_start_payloads.jsonl"),
           "AGENTOPS_BRAIN_INJECT_DIR": str(base / "brain_inject")}
    return subprocess.run([sys.executable, str(HOOK), *args], input=json.dumps(payload).encode("utf-8"),
                          capture_output=True, env=env, timeout=60)


def _rules(project: Path, payload: dict, log: Path) -> subprocess.CompletedProcess:
    return _run(project, payload, log, "rules")


def _context(result: subprocess.CompletedProcess, event: str = "SessionStart") -> str:
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    out = json.loads(result.stdout.decode("utf-8"))
    assert out["hookSpecificOutput"]["hookEventName"] == event
    return out["hookSpecificOutput"]["additionalContext"]


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _transcript(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _markers(tmp_path: Path) -> list[str]:
    folder = tmp_path / "brain_inject"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# -- row 5: the whole file, read from origin/main, no fetch ------------------------------------------------

@pytest.mark.parametrize("source", ["startup", "clear", "compact"])
def test_rules_mode_injects_head_line_and_the_whole_file(tmp_path: Path, source: str):
    text = _brain_md(9359)
    _, project = _make_repo(tmp_path, text)
    result = _rules(project, _payload(project, source=source), tmp_path / "log.jsonl")
    context = _context(result)
    assert context == f"{HEAD_ORIGIN}\n{text}"
    assert len(context) <= CAP


def test_rules_mode_runs_no_git_fetch(tmp_path: Path):
    """A newer brain.md on origin that the clone has not fetched is NOT seen: the mode reads the clone's
    `origin/main` as it stands."""
    old, new = _brain_md(9359, "o"), _brain_md(9000, "n")
    work, project = _make_repo(tmp_path, old)
    (work / "docs" / "agents" / "brain.md").write_bytes(new.encode("utf-8"))
    _git(work, "commit", "-am", "newer rules")
    _git(work, "push", "origin", "main")
    context = _context(_rules(project, _payload(project), tmp_path / "log.jsonl"))
    assert context == f"{HEAD_ORIGIN}\n{old}"


def test_rules_mode_reads_the_working_copy_when_origin_has_no_file(tmp_path: Path):
    text = _brain_md(9359)
    _, project = _make_repo(tmp_path, text)
    _git(project, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    _git(project, "update-ref", "-d", "refs/remotes/origin/main")
    (project / "docs" / "agents" / "brain.md").write_bytes(b"# local rules\n")
    context = _context(_rules(project, _payload(project), tmp_path / "log.jsonl"))
    assert context == f"{HEAD_WORKING}\n# local rules\n"


def test_rules_mode_logs_an_injected_row_marked_with_its_mode(tmp_path: Path):
    _, project = _make_repo(tmp_path, _brain_md(9359))
    log = tmp_path / "log.jsonl"
    _rules(project, _payload(project), log)
    rows = _rows(log)
    assert [(r["action"], r["session"], r["mode"]) for r in rows] == [("injected", SESSION, "rules")]


def test_hand_off_mode_is_unchanged_and_rows_carry_no_mode(tmp_path: Path):
    _, project = _make_repo(tmp_path, _brain_md(9359))
    log = tmp_path / "log.jsonl"
    result = _run(project, _payload(project), log)
    assert _context(result) == f"{HAND_OFF_HEAD}\n{BRAIN_LINE}"
    assert all("mode" not in r for r in _rows(log))


# -- row 6: the same skips as the hand-off mode ---------------------------------------------------------------

def _autopilot_payload(project: Path, tmp_path: Path) -> dict:
    transcript = _transcript(tmp_path, {"type": "agent-setting", "agentSetting": "brain"}, {"type": "user"})
    return _payload(project, title=None, transcript_path=str(transcript))


@pytest.mark.parametrize("case, why", [
    ("other-title", "not-brain-title"),
    ("bg", "bg"),
    ("autopilot", "no-title"),
])
def test_rules_mode_skips_like_the_hand_off_mode(tmp_path: Path, case: str, why: str):
    _, project = _make_repo(tmp_path, _brain_md(9359))
    payloads = {
        "other-title": _payload(project, title="Some other chat"),
        "bg": _payload(project, sessionKind="bg"),
        "autopilot": _autopilot_payload(project, tmp_path),
    }
    payload = payloads[case]
    rules_log, hand_log = tmp_path / "rules" / "log.jsonl", tmp_path / "hand" / "log.jsonl"
    rules_result = _rules(project, payload, rules_log)
    hand_result = _run(project, payload, hand_log)
    assert rules_result.returncode == 0 and rules_result.stdout == b""
    assert hand_result.stdout == b""
    rules_skips = [r["why"] for r in _rows(rules_log) if r["action"] == "skip"]
    hand_skips = [r["why"] for r in _rows(hand_log) if r["action"] == "skip"]
    assert rules_skips == hand_skips == [why]
    assert all(r["mode"] == "rules" for r in _rows(rules_log))


# -- row 7: the fallback, once, with markers apart ----------------------------------------------------------------

def test_rules_mode_fallback_injects_once_and_markers_are_separate(tmp_path: Path):
    text = _brain_md(9359)
    _, project = _make_repo(tmp_path, text)
    log = tmp_path / "log.jsonl"
    # SessionStart: the title is not there yet
    start = _rules(project, _payload(project, title=None), log)
    assert start.stdout == b""
    assert _markers(tmp_path) == []
    # UserPromptSubmit: the transcript head carries the brain title
    transcript = _transcript(tmp_path, {"type": "user"}, {"type": "custom-title", "customTitle": BRAIN_TITLE})
    first = _rules(project, _event_payload(project, "UserPromptSubmit", transcript), log)
    assert _context(first, "UserPromptSubmit") == f"{HEAD_ORIGIN}\n{text}"
    # a second event of the same session: nothing
    second = _rules(project, _event_payload(project, "PreToolUse", transcript, tool_name="Bash"), log)
    assert second.returncode == 0 and second.stdout == b""
    injected = [r for r in _rows(log) if r["action"] == "injected"]
    assert [(r["event"], r["mode"]) for r in injected] == [("UserPromptSubmit", "rules")]
    # the rules mode left the hand-off mode's marker alone
    assert len(_markers(tmp_path)) == 1
    assert f"{SESSION}.json" not in _markers(tmp_path)
    # the hand-off mode still injects for the same session
    hand = _run(project, _event_payload(project, "PreToolUse", transcript, tool_name="Bash"), log)
    assert _context(hand, "PreToolUse") == f"{HAND_OFF_HEAD}\n{BRAIN_LINE}"
    assert f"{SESSION}.json" in _markers(tmp_path) and len(_markers(tmp_path)) == 2


def test_hand_off_success_does_not_stop_the_rules_mode(tmp_path: Path):
    text = _brain_md(9359)
    _, project = _make_repo(tmp_path, text)
    log = tmp_path / "log.jsonl"
    transcript = _transcript(tmp_path, {"type": "custom-title", "customTitle": BRAIN_TITLE})
    hand = _run(project, _event_payload(project, "UserPromptSubmit", transcript), log)
    assert _context(hand, "UserPromptSubmit") == f"{HAND_OFF_HEAD}\n{BRAIN_LINE}"
    rules = _rules(project, _event_payload(project, "UserPromptSubmit", transcript), log)
    assert _context(rules, "UserPromptSubmit") == f"{HEAD_ORIGIN}\n{text}"


def test_rules_mode_compact_reinjects_after_the_marker_says_injected(tmp_path: Path):
    text = _brain_md(9359)
    _, project = _make_repo(tmp_path, text)
    log = tmp_path / "log.jsonl"
    assert _context(_rules(project, _payload(project, source="clear"), log)) == f"{HEAD_ORIGIN}\n{text}"
    again = _rules(project, _payload(project, source="compact"), log)
    assert _context(again) == f"{HEAD_ORIGIN}\n{text}"


def test_rules_mode_non_brain_session_stops_trying_after_five_events(tmp_path: Path):
    _, project = _make_repo(tmp_path, _brain_md(9359))
    log = tmp_path / "log.jsonl"
    transcript = _transcript(tmp_path, {"type": "custom-title", "customTitle": "Some other chat"})
    for _ in range(6):
        assert _rules(project, _event_payload(project, "PreToolUse", transcript), log).stdout == b""
    assert [r["why"] for r in _rows(log) if r["action"] == "skip"] == ["not-brain-title"] * 5


# -- row 8: the cap ---------------------------------------------------------------------------------------------

def test_over_cap_file_is_replaced_by_the_read_it_now_line(tmp_path: Path):
    _, project = _make_repo(tmp_path, _brain_md(10_500))
    log = tmp_path / "log.jsonl"
    context = _context(_rules(project, _payload(project), log))
    assert context == (f"{HEAD_ORIGIN}\ndocs/agents/brain.md is 10500 characters, over the 10,000-character "
                       "hook cap: read it whole now")
    assert len(context) <= CAP
    over = [r for r in _rows(log) if r["action"] == "rules-over-cap"]
    assert len(over) == 1
    assert over[0]["chars"] == 10_500 and over[0]["mode"] == "rules"


def test_file_that_fits_with_the_head_line_exactly_is_sent_whole(tmp_path: Path):
    """The cap counts the head line and its newline: the largest file that fits is CAP - len(head) - 1."""
    size = CAP - len(HEAD_ORIGIN) - 1
    text = _brain_md(size)
    _, project = _make_repo(tmp_path, text)
    log = tmp_path / "log.jsonl"
    context = _context(_rules(project, _payload(project), log))
    assert context == f"{HEAD_ORIGIN}\n{text}" and len(context) == CAP
    assert [r for r in _rows(log) if r["action"] == "rules-over-cap"] == []


def test_one_character_more_is_over_the_cap(tmp_path: Path):
    size = CAP - len(HEAD_ORIGIN)
    _, project = _make_repo(tmp_path, _brain_md(size))
    log = tmp_path / "log.jsonl"
    context = _context(_rules(project, _payload(project), log))
    assert "read it whole now" in context and len(context) <= CAP
    assert len([r for r in _rows(log) if r["action"] == "rules-over-cap"]) == 1


# -- row 9: the repo's own file under the cap (the cap test of the issue's rule 6) ------------------------------

def test_repos_own_brain_md_fits_the_cap_with_the_head_line():
    text = (ROOT / BRAIN_MD_PATH).read_bytes().decode("utf-8", errors="replace")
    assert len(HEAD_ORIGIN) + 1 + len(text) <= CAP, (
        f"{BRAIN_MD_PATH} is {len(text)} characters; head line + file must stay within {CAP}")
