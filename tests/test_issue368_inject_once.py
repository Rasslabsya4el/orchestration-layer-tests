"""#368 unit 3: of N concurrent runs of `tools/hooks/brain_state_inject.py` with one session id, exactly one injects.

Each run is a plain `python tools/hooks/brain_state_inject.py` child, the payload on stdin, its output on stdout,
the mode as its one argument for the rules mode (`rules`). Runs start together behind a barrier, in the hand-off
(entry) mode and in the rules mode, on the first tool call (PreToolUse, UserPromptSubmit) and on a SessionStart
(startup). Acceptance row 2 of #368: exactly 1 output with the text per mode.

Review follow-ups (#368): the same concurrency from a marker that holds an earlier miss (`injected` false, tries 1
and 3), and from an empty marker (a claim killed between create and write): older than 30 s it is stale and one call
injects; younger than 30 s it is a claim being written and nothing injects.

The git side is absent on purpose: the tmp project is no git repo, so the fetch fails and both modes read the working
copy (the same path the hook takes on a machine without a remote).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "brain_state_inject.py"

BRAIN_TITLE = "Brain: orch layer"
SESSION = "sess-368"
CALLS = 5
BRAIN_LINE = "Updated (brain) 24.10, #368 inject once test entry"
RULES_TEXT = "# Brain rules\n\nrule one, #368 inject once test\n"
HAND_OFF_HEAD = "This chat is the brain; its hand-off entry from docs/STATE.md follows"
RULES_HEAD = "This chat is the brain; its rule file docs/agents/brain.md follows, read from"
EVENTS = [
    ("PreToolUse", None),
    ("UserPromptSubmit", None),
    ("SessionStart", "startup"),
]
EVENT_IDS = ["pretooluse", "userpromptsubmit", "sessionstart-startup"]
MODES = ["", "rules"]
MODE_IDS = ["hand-off", "rules"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "orchestration.json").write_text(
        json.dumps({"brain_chat_title": BRAIN_TITLE}), encoding="utf-8")
    (project / "docs" / "agents").mkdir(parents=True)
    (project / "docs" / "agents" / "brain.md").write_bytes(RULES_TEXT.encode("utf-8"))
    (project / "docs" / "STATE.md").write_text(f"# STATE\n\n{BRAIN_LINE}\n", encoding="utf-8")
    return project


def _payload(project: Path, event: str, source: str | None) -> dict:
    payload = {
        "hook_event_name": event,
        "session_id": SESSION,
        "session_title": BRAIN_TITLE,
        "cwd": str(project),
        "transcript_path": str(project / "no-transcript-yet.jsonl"),
    }
    if source is not None:
        payload["source"] = source
    return payload


def _env(tmp_path: Path, project: Path) -> dict:
    base = tmp_path / "state"
    return {**os.environ,
            "CLAUDE_PROJECT_DIR": str(project),
            "AGENTOPS_HOOK_LOG": str(base / "log.jsonl"),
            "AGENTOPS_SESSION_PAYLOAD_LOG": str(base / "payloads.jsonl"),
            "AGENTOPS_BRAIN_INJECT_DIR": str(base / "brain_inject")}


def _marker(tmp_path: Path, mode: str) -> Path:
    name = f"{SESSION}.{mode}.json" if mode else f"{SESSION}.json"
    return tmp_path / "state" / "brain_inject" / name


def _run_once(payload: dict, env: dict, args: list[str], barrier: threading.Barrier) -> str:
    barrier.wait(timeout=30)
    result = subprocess.run([sys.executable, str(HOOK), *args], input=json.dumps(payload).encode("utf-8"),
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("utf-8")


def _runs(tmp_path: Path, event: str, source: str | None, mode: str, calls: int = CALLS,
          seed: str | None = None, age: float = 0.0) -> list[str]:
    """`calls` concurrent runs; a `seed` is the marker's content written first (`age` seconds old when set)."""
    project = _project(tmp_path)
    env = _env(tmp_path, project)
    payload = _payload(project, event, source)
    args = [mode] if mode else []
    if seed is not None:
        marker = _marker(tmp_path, mode)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(seed, encoding="utf-8")
        if age:
            past = time.time() - age
            os.utime(marker, (past, past))
    barrier = threading.Barrier(calls)
    with ThreadPoolExecutor(max_workers=calls) as pool:
        return list(pool.map(lambda _: _run_once(payload, env, args, barrier), range(calls)))


def _assert_one_injection(outs: list[str], event: str, mode: str, tmp_path: Path) -> None:
    injected = [json.loads(out) for out in outs if out.strip()]
    assert len(injected) == 1, f"{len(injected)} of {len(outs)} runs injected: {outs}"
    hook_output = injected[0]["hookSpecificOutput"]
    assert hook_output["hookEventName"] == event
    context = hook_output["additionalContext"]
    if mode:
        assert context.startswith(RULES_HEAD)
        assert RULES_TEXT in context
    else:
        assert context.startswith(HAND_OFF_HEAD)
        assert BRAIN_LINE in context
    marker = json.loads(_marker(tmp_path, mode).read_text(encoding="utf-8"))
    assert marker["injected"] is True


@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
@pytest.mark.parametrize("event, source", EVENTS, ids=EVENT_IDS)
def test_five_concurrent_runs_inject_once(tmp_path: Path, event: str, source: str | None, mode: str):
    _assert_one_injection(_runs(tmp_path, event, source, mode), event, mode, tmp_path)


@pytest.mark.parametrize("seeded_tries", [1, 3])
@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
@pytest.mark.parametrize("event, source", EVENTS, ids=EVENT_IDS)
def test_five_concurrent_retries_of_an_earlier_miss_inject_once(
        tmp_path: Path, event: str, source: str | None, mode: str, seeded_tries: int):
    seed = json.dumps({"injected": False, "tries": seeded_tries})
    _assert_one_injection(_runs(tmp_path, event, source, mode, seed=seed), event, mode, tmp_path)


@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_empty_marker_older_than_thirty_seconds_is_stale_and_injects(tmp_path: Path, mode: str):
    outs = _runs(tmp_path, "PreToolUse", None, mode, calls=1, seed="", age=60)
    _assert_one_injection(outs, "PreToolUse", mode, tmp_path)


@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_five_concurrent_runs_on_a_stale_empty_marker_inject_once(tmp_path: Path, mode: str):
    outs = _runs(tmp_path, "PreToolUse", None, mode, seed="", age=60)
    _assert_one_injection(outs, "PreToolUse", mode, tmp_path)


@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_fresh_empty_marker_is_a_claim_being_written_and_injects_nothing(tmp_path: Path, mode: str):
    outs = _runs(tmp_path, "PreToolUse", None, mode, calls=1, seed="")
    assert [out for out in outs if out.strip()] == []
    assert _marker(tmp_path, mode).read_text(encoding="utf-8") == ""


# -- a startup or clear over a marker that already says injected (the old code re-injected on every start) ------

INJECTED = json.dumps({"injected": True, "tries": 0})


@pytest.mark.parametrize("source", ["clear", "startup"])
@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_a_start_over_an_old_injection_injects_as_before(tmp_path: Path, source: str, mode: str):
    outs = _runs(tmp_path, "SessionStart", source, mode, calls=1, seed=INJECTED, age=60)
    _assert_one_injection(outs, "SessionStart", mode, tmp_path)


@pytest.mark.parametrize("source", ["clear", "startup"])
@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_five_concurrent_starts_over_an_old_injection_inject_once(tmp_path: Path, source: str, mode: str):
    outs = _runs(tmp_path, "SessionStart", source, mode, seed=INJECTED, age=60)
    _assert_one_injection(outs, "SessionStart", mode, tmp_path)


@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
def test_a_start_within_the_sibling_window_of_an_injection_is_skipped(tmp_path: Path, mode: str):
    """The accepted trade-off (#368 review): a start younger than SIBLING_SECONDS over an injection is taken for a
    parallel call of that start, so it injects nothing."""
    outs = _runs(tmp_path, "SessionStart", "clear", mode, calls=1, seed=INJECTED, age=0)
    assert [out for out in outs if out.strip()] == []
