"""#368 unit D: the text lister. Every text the brain hooks can emit to the brain must not tell how a reply opens.

Seams (the real functions, driven by inputs that reach each branch; no hook text is copied here):
- ``tools/hooks/brain_reset_nudge.py``: ``handle_user_prompt_submit`` (the size nudge in its line, wait and ceiling
  branches, the first-prompt brain title with and without the model line), ``handle_post_tool_use`` (the wayfinder close).
- ``tools/hooks/brain_read_nudge.py``: ``run`` (the untagged nudge, the only text the read hook gives now; the
  third-read text is gone, #368 unit B).
- ``tools/hooks/brain_state_inject.py``: ``run`` in hand-off mode and in rules mode (the hook-authored wrapper text
  only). The injected files (``docs/STATE.md`` entry, ``docs/agents/brain.md``) are file content, not hook text:
  they are replaced by a placeholder, so only the wrapper the hook writes around them is collected. ``git`` is stubbed
  (``_git``) so each fetch and origin outcome is reached without a repo.

Second seam, without driving: every string literal of the three hook modules (``ast``, docstrings excluded, f-string
literal parts joined) goes through the same ``forbidden``, so a new literal in any branch is caught even when no
branch above drives it.

The reply check (``brain_reset_nudge._reply_check_text``) is EXEMPT: it is switched off by #347
(``REPLY_CHECK_ENABLED``), and its text is the reply rule of ``docs/agents/brain.md`` read from a file. See ``EXEMPT``.

Acceptance (#368 row 1): ``collect`` lists the branches, ``violations`` is empty over them and over the literals, and a
planted "Start every reply with X" added to any of the three hook modules makes the same check fail.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_settings import SETTINGS_PATH, load as load_settings  # noqa: E402
from tools.hooks import brain_read_nudge as read_hook  # noqa: E402
from tools.hooks import brain_reset_nudge as hook  # noqa: E402
from tools.hooks import brain_state_inject as inject_hook  # noqa: E402

BRAIN_TITLE = hook.BRAIN_CHAT_TITLE
LINE = 150_000
CEILING = 200_000
SESSION = "s368"
PLANT = "Start every reply with X."

#: phrases that tell how a reply opens or that a reply is a hand-off (#368 row 1), matched case-insensitively.
#: "every reply" also matches "start every reply", "begin every reply" and "open every reply".
FORBIDDEN = (
    "opens with",
    "every reply",
    "the reply is the hand-off",
)

#: every branch ``collect`` must reach. The count and the names are both asserted, so a dropped branch fails.
BRANCHES = (
    "reset_nudge:line",                       # over the line, no live work
    "reset_nudge:wait-line-ceiling",          # over the line, under the ceiling, a subagent live
    "reset_nudge:ceiling",                    # over the ceiling, no live work
    "reset_nudge:wait-ceiling-subagent",      # over the ceiling, a subagent live
    "reset_nudge:wait-ceiling-shell-monitor", # over the ceiling, a background shell and a monitor live
    "reset_nudge:first-prompt",               # the brain's first prompt, title only
    "reset_nudge:first-prompt-model",         # the brain's first prompt, model differs from the core row
    "reset_nudge:wayfinder-close",            # a wayfinder ticket closed by a Bash call
    "read_nudge:untagged",                    # an untagged shell call of the brain
    "state_inject:hand-off-entry",            # SessionStart, hand-off mode, fetch failed, the brain entry found
    "state_inject:hand-off-no-entry",         # SessionStart, hand-off mode, fetch failed, no brain entry
    "state_inject:hand-off-origin-missing",   # SessionStart, hand-off mode, fetch ok, origin/<branch> has no STATE.md
    "state_inject:rules",                     # SessionStart, rules mode, working copy, the file fits under the cap
    "state_inject:rules-origin",              # SessionStart, rules mode, read from origin/<branch>
    "state_inject:rules-over-cap",            # SessionStart, rules mode, the file is over the hook cap
    "state_inject:rules-unreadable",          # SessionStart, rules mode, the file cannot be read
)

#: the three hook modules whose literals ``literal_violations`` scans
HOOK_MODULES = ("brain_reset_nudge.py", "brain_read_nudge.py", "brain_state_inject.py")

#: texts a hook can emit that ``collect`` does not check, each with the reason. Never silently skipped.
EXEMPT = {
    "reset_nudge:reply-check": (
        "switched off by #347 (REPLY_CHECK_ENABLED is False): handle_user_prompt_submit never calls "
        "_reply_check_text, so it emits nothing. When on, its text is the block of docs/agents/brain.md between the "
        "reply-check markers: a file, and a reply rule by design, so it legitimately talks about replies. "
        "test_reply_check_exemption_still_holds fails when the switch is turned on, to make the cover decision again."
    ),
}

#: literals of the hook modules (``module:line``) that trip ``forbidden`` and are legitimate, each with the reason.
#: Empty today: no literal of the three modules holds a reply-opening instruction.
LITERAL_EXEMPT: dict[str, str] = {}


def forbidden(text: str) -> list[str]:
    """The phrases of ``FORBIDDEN`` that ``text`` holds, case-insensitive, in ``FORBIDDEN``'s order; ``[]`` when none."""
    low = text.lower()
    return [phrase for phrase in FORBIDDEN if phrase in low]


def violations(texts: dict[str, str]) -> dict[str, list[str]]:
    """The texts of ``texts`` that hold a forbidden phrase, name -> matched phrases; ``{}`` when all hold none."""
    found: dict[str, list[str]] = {}
    for name, text in texts.items():
        hits = forbidden(text)
        if hits:
            found[name] = hits
    return found


def literal_strings(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` of every str constant of ``source`` outside a docstring, and of every f-string joined from its
    literal parts (a placeholder reads as ``{}``, so a phrase is not split by one)."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    joined_parts: set[int] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
            found.append((node.lineno, text))
            joined_parts.update(id(part) for part in node.values)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and id(node) not in joined_parts):
            found.append((node.lineno, node.value))
    return sorted(found)


def literal_violations(source: str, module: str = "source") -> dict[str, list[str]]:
    """``module:line`` -> matched phrases for every literal of ``source`` that ``forbidden`` flags, minus LITERAL_EXEMPT."""
    found: dict[str, list[str]] = {}
    for line, text in literal_strings(source):
        key = f"{module}:{line}"
        hits = forbidden(text)
        if hits and key not in LITERAL_EXEMPT:
            found[key] = hits
    return found


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every hook write goes under tmp_path; the settings file pins the brain title and the two context sizes."""
    settings = load_settings(SETTINGS_PATH)
    settings.update(brain_chat_title=BRAIN_TITLE, brain_chats={}, context_line=LINE, context_ceiling=CEILING)
    path = tmp_path / "settings" / "orchestration.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(path))
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / "var" / "brain_nudge.log"))
    monkeypatch.setenv("AGENTOPS_BRAIN_INJECT_DIR", str(tmp_path / "var" / "brain_inject"))
    monkeypatch.setenv("AGENTOPS_SESSION_PAYLOAD_LOG", str(tmp_path / "var" / "payloads.jsonl"))
    monkeypatch.delenv("AGENTOPS_HOOK_GH", raising=False)
    monkeypatch.delenv("AGENTOPS_BRAIN_MD", raising=False)


# --- transcript and payload builders ---------------------------------------------------------------

def _transcript(tmp_path: Path, name: str, records: list[dict]) -> str:
    path = tmp_path / "transcripts" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _title() -> dict:
    return {"type": "custom-title", "customTitle": BRAIN_TITLE, "sessionId": SESSION}


def _owner() -> dict:
    return {"type": "user", "uuid": "owner-1", "message": {"role": "user", "content": "next"}}


def _usage(tokens: int) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                                             "usage": {"input_tokens": tokens}}}


def _model(name: str) -> dict:
    return {"type": "attachment", "attachment": {"type": "model", "identity": {"marketingName": name}}}


def _launch(text: str) -> dict:
    """A tool_result record of a background launch (the shape ``transcript.live_things`` counts)."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu", "content": text}]}}


SUBAGENT = _launch("Async agent launched successfully. agentId: a92546000dbe92ddd (internal ID x)")
SHELL = _launch("Command running in background with ID: blcg3vv5o. Output is being written to: out")
MONITOR = _launch("Monitor started (task b36wayjkg, expires in 30m unless the source ends first)")


def _prompt(transcript: str) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "transcript_path": transcript,
            "prompt": "next"}


def _context(out: dict | None, name: str) -> str:
    assert out is not None, f"{name}: the branch emitted nothing"
    return out["hookSpecificOutput"]["additionalContext"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path, name: str, state: str | None = None, brain: str | None = None) -> Path:
    """A project dir: the brain title in its settings, and the files the injection reads (a file is content, not hook
    text; the collector gives them placeholder text)."""
    project = tmp_path / "projects" / name
    _write(project / "config" / "orchestration.json",
           json.dumps({"brain_chat_title": BRAIN_TITLE, "brain_chats": {}}))
    if state is not None:
        _write(project / "docs" / "STATE.md", state)
    if brain is not None:
        _write(project / "docs" / "agents" / "brain.md", brain)
    return project


def _start(session: str) -> dict:
    return {"hook_event_name": "SessionStart", "source": "startup", "session_id": session,
            "session_title": BRAIN_TITLE}


def _git_stub(fetch_ok: bool, origin: bytes | None):
    """A stand-in for ``brain_state_inject._git``: ``fetch`` succeeds or not; ``symbolic-ref`` names no branch (so
    ``main``); ``show origin/<branch>:<path>`` gives ``origin`` (bytes) or fails when it is None."""
    def git(project, *args, timeout=0):
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 0 if fetch_ok else 1, b"", b"")
        if args[0] == "symbolic-ref":
            return subprocess.CompletedProcess(args, 1, b"", b"")
        if origin is None:
            return subprocess.CompletedProcess(args, 128, b"", b"")
        return subprocess.CompletedProcess(args, 0, origin, b"")
    return git


#: the placeholder for an injected file: the file's text is content, never hook text, so it is not what is checked
FILE_PLACEHOLDER = "(file content placeholder)"


# --- the collector -----------------------------------------------------------------------------------

def collect(tmp_path: Path, monkeypatch) -> dict[str, str]:
    """Every hook-authored text, one per branch in ``BRANCHES``, from the real functions. A branch that emits
    nothing fails here, by name."""
    texts: dict[str, str] = {}

    def size(name: str, tokens: int, live: list[dict], expect: str) -> None:
        path = _transcript(tmp_path, name, [_title(), _owner(), *live, _usage(tokens)])
        text = _context(hook.handle_user_prompt_submit(_prompt(path)), name)
        assert expect in text, f"{name} is not the branch it names: {text}"
        texts[name] = text

    size("reset_nudge:line", 160_000, [], "over the context line (150k)")
    size("reset_nudge:wait-line-ceiling", 160_000, [SUBAGENT], "live (1 subagent(s), 0 background shell(s)")
    size("reset_nudge:ceiling", 250_000, [], "over the context ceiling (200k)")
    size("reset_nudge:wait-ceiling-subagent", 250_000, [SUBAGENT], "live (1 subagent(s), 0 background shell(s)")
    size("reset_nudge:wait-ceiling-shell-monitor", 250_000, [SHELL, MONITOR],
         "live (0 subagent(s), 1 background shell(s), 1 monitor(s))")

    path = _transcript(tmp_path, "reset_nudge-first-prompt", [_title(), _owner()])
    text = _context(hook.handle_user_prompt_submit(_prompt(path)), "first-prompt")
    assert "you are the brain" in text, text
    texts["reset_nudge:first-prompt"] = text

    path = _transcript(tmp_path, "reset_nudge-first-prompt-model", [_title(), _model("Opus 5.5"), _owner()])
    text = _context(hook.handle_user_prompt_submit(_prompt(path)), "first-prompt-model")
    assert "is 'Opus 5.5'" in text, text
    texts["reset_nudge:first-prompt-model"] = text

    monkeypatch.setattr(hook, "_gh_labels", lambda n, timeout=5: ["wayfinder:map"])
    path = _transcript(tmp_path, "reset_nudge-wayfinder-close", [_title(), _owner()])
    closed = hook.handle_post_tool_use({
        "hook_event_name": "PostToolUse", "session_id": SESSION, "transcript_path": path,
        "tool_name": "Bash", "tool_input": {"command": "gh issue close 5"},
        "tool_response": {"stdout": "✓ Closed issue o/n#5", "stderr": ""},
    })
    text = _context(closed, "wayfinder-close")
    assert "A wayfinder ticket closed (#5)" in text, text
    texts["reset_nudge:wayfinder-close"] = text

    path = _transcript(tmp_path, "read_nudge-untagged", [_title(), _owner()])
    out = read_hook.run({"hook_event_name": "PreToolUse", "session_id": SESSION, "transcript_path": path,
                         "tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    text = _context(out, "untagged")
    assert "counted untagged" in text, text
    texts["read_nudge:untagged"] = text

    # hand-off mode: a failed fetch on a machine with no reachable origin (the first two branches)
    monkeypatch.setattr(inject_hook, "_git", _git_stub(fetch_ok=False, origin=None))

    project = _project(tmp_path, "hand-off-entry", state="# State\n\nUpdated (brain) placeholder entry\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-handoff-entry"), ""), "hand-off-entry")
    assert "This chat is the brain" in text and "git fetch failed" in text, text
    texts["state_inject:hand-off-entry"] = text

    project = _project(tmp_path, "hand-off-no-entry", state="# State\n\nno brain line\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-handoff-none"), ""), "hand-off-no-entry")
    assert "has no brain entry" in text, text
    texts["state_inject:hand-off-no-entry"] = text

    # hand-off mode: the fetch succeeds, but origin/main has no docs/STATE.md: the working copy is read instead
    monkeypatch.setattr(inject_hook, "_git", _git_stub(fetch_ok=True, origin=None))
    project = _project(tmp_path, "hand-off-origin-missing", state="# State\n\nUpdated (brain) placeholder entry\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-handoff-origin"), ""), "hand-off-origin-missing")
    assert "origin/main has no docs/STATE.md, read from the working copy" in text, text
    texts["state_inject:hand-off-origin-missing"] = text

    # rules mode: no origin file is asked for except through the stub; the working copy is the source
    monkeypatch.setattr(inject_hook, "_git", _git_stub(fetch_ok=False, origin=None))
    project = _project(tmp_path, "rules", brain=FILE_PLACEHOLDER + "\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-rules"), "rules"), "rules")
    assert "its rule file docs/agents/brain.md follows, read from the working copy" in text, text
    texts["state_inject:rules"] = text

    # rules mode: origin/main has the file, so the head names origin/main as its source
    monkeypatch.setattr(inject_hook, "_git", _git_stub(fetch_ok=True, origin=(FILE_PLACEHOLDER + "\n").encode()))
    project = _project(tmp_path, "rules-origin", brain=FILE_PLACEHOLDER + "\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-rules-origin"), "rules"), "rules-origin")
    assert "read from origin/main" in text, text
    texts["state_inject:rules-origin"] = text

    monkeypatch.setattr(inject_hook, "_git", _git_stub(fetch_ok=False, origin=None))
    project = _project(tmp_path, "rules-over-cap", brain="x" * (inject_hook.HOOK_CAP + 1))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-rules-cap"), "rules"), "rules-over-cap")
    assert "over the 10,000-character hook cap" in text, text
    texts["state_inject:rules-over-cap"] = text

    project = _project(tmp_path, "rules-unreadable")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    text = _context(inject_hook.run(_start("s368-rules-none"), "rules"), "rules-unreadable")
    assert "could not be read" in text, text
    texts["state_inject:rules-unreadable"] = text

    return texts


# --- the tests -----------------------------------------------------------------------------------------

def test_forbidden_matches_each_phrase_case_insensitively():
    assert forbidden("Start every reply with X.") == ["every reply"]
    assert forbidden("THE REPLY OPENS WITH the status") == ["opens with"]
    assert forbidden("the reply is the hand-off") == ["the reply is the hand-off"]
    assert forbidden("Answer the owner in full; start no long new work.") == []


def test_below_the_line_nothing_is_emitted(tmp_path):
    path = _transcript(tmp_path, "below-line", [_title(), _owner(), _usage(100_000)])
    assert hook.handle_user_prompt_submit(_prompt(path)) is None


def test_collector_reaches_every_branch(tmp_path, monkeypatch):
    texts = collect(tmp_path, monkeypatch)
    assert len(texts) >= len(BRANCHES)
    assert set(BRANCHES) <= set(texts)
    assert not set(EXEMPT) & set(texts)


def test_no_hook_text_tells_how_a_reply_opens(tmp_path, monkeypatch):
    texts = collect(tmp_path, monkeypatch)
    assert violations(texts) == {}


def test_no_hook_literal_tells_how_a_reply_opens():
    """Every string literal of the three hook modules, not only the driven ones (``ast``, docstrings excluded)."""
    hooks_dir = ROOT / "tools" / "hooks"
    found: dict[str, list[str]] = {}
    for name in HOOK_MODULES:
        source = (hooks_dir / name).read_text(encoding="utf-8")
        found.update(literal_violations(source, name))
    assert found == {}


def test_planted_literal_in_source_is_reported():
    """The same literal scan over a temporary module text: ``X = "Start every reply with X"`` is reported."""
    found = literal_violations('X = "Start every reply with X"\n', "planted")
    assert found == {"planted:1": ["every reply"]}


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_planted_literal_added_to_a_hook_module_is_reported(module):
    """A planted line appended to each real hook module's own source is reported by the same literal scan."""
    source = (ROOT / "tools" / "hooks" / module).read_text(encoding="utf-8")
    planted = source + '\nX = "Start every reply with X"\n'
    last = len(planted.splitlines())
    found = literal_violations(planted, module)
    assert found.get(f"{module}:{last}") == ["every reply"], found


def test_reply_check_exemption_still_holds():
    assert "reset_nudge:reply-check" in EXEMPT
    assert hook.REPLY_CHECK_ENABLED is False, (
        "the reply check is on (#347 switched it off): its text can emit now, so cover it in BRANCHES "
        "or re-decide its EXEMPT entry")


def _plant_nudge(monkeypatch) -> None:
    """Add the planted sentence to the size nudge's real text (any kind, the same function result)."""
    real = hook._nudge_text

    def planted(*args):
        kind, text = real(*args)
        return kind, f"{text} {PLANT}"

    monkeypatch.setattr(hook, "_nudge_text", planted)


#: per hook module: the planting, and the branch the planted sentence must be found in
PLANTS = {
    "reset_nudge": (lambda monkeypatch: _plant_nudge(monkeypatch), "reset_nudge:line"),
    "read_nudge": (lambda monkeypatch: monkeypatch.setattr(read_hook, "UNTAGGED_TEXT",
                                                           read_hook.UNTAGGED_TEXT + " " + PLANT),
                   "read_nudge:untagged"),
    "state_inject": (lambda monkeypatch: monkeypatch.setattr(inject_hook, "HEAD_TEXT",
                                                             inject_hook.HEAD_TEXT + " " + PLANT),
                     "state_inject:hand-off-entry"),
}


@pytest.mark.parametrize("module", sorted(PLANTS))
def test_planted_start_every_reply_fails_the_check(module, tmp_path, monkeypatch):
    plant, branch = PLANTS[module]
    plant(monkeypatch)
    found = violations(collect(tmp_path, monkeypatch))
    assert branch in found, f"a planted text in {module} was not found in {branch}"
    assert "every reply" in found[branch]
