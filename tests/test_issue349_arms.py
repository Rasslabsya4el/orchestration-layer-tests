"""Issue #349, unit 4: the two arms' run trees (`tools/replays/arms.py`).

Unit tests build a role repo (two commits), a case repo and a draft dir in tmp_path. One integration test
builds the 6 real trees (cases a, b, c; arms old, new) and skips when a repo path or commit is absent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools.replays import arms  # noqa: E402
from tools.replays.launcher import resolve_repo  # noqa: E402

STOP_NAME = "state_claim_check.py"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60)
    return done.stdout.strip()


def _write(root: Path, rel: str, data: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _settings(hooks: dict, indent: int = 2, **other) -> str:
    return json.dumps({"permissions": {"deny": []}, **other, "hooks": hooks}, indent=indent)


def _cmd(script: str) -> dict:
    return {"type": "command", "command": f'python "$CLAUDE_PROJECT_DIR/tools/hooks/{script}"'}


ROLE_HOOKS = {
    "UserPromptSubmit": [{"hooks": [_cmd("brain_reset_nudge.py")]}],
    "Stop": [{"hooks": [_cmd(STOP_NAME)]}],
}

#: stands in for the real hook: the `_reply_check_text` function whose first statement the new arm edits.
FAKE_NUDGE = (
    'def _reply_check_text(payload, transcript_path):\n'
    '    """Issue #65: the reply check for every owner prompt of a brain chat (title in ``_brain_titles``)."""\n'
    '    return "check"\n'
)


#: stands in for the state hook's `_inject`: the fetch of origin and the note its failure adds.
FAKE_STATE = (
    'def _inject(payload, event, project):\n'
    '    fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)\n'
    '    fetched = fetch is not None and fetch.returncode == 0\n'
    '    if fetched:\n'
    '        text = _origin_text(project, "main")\n'
    '    else:\n'
    '        notes.append(FETCH_FAILED_NOTE)\n'
    '        text = _working_copy_text(project)\n'
)


@pytest.fixture
def world(tmp_path):
    role = tmp_path / "role"
    role.mkdir()
    _git(role, "init", "-q")
    _write(role, "docs/agents/brain.md", b"old brain\r\nline two\r\n")
    _write(role, ".claude/agents/brain.md", b"old agent\n")
    _write(role, ".claude/settings.json", _settings(ROLE_HOOKS))
    _write(role, "tools/hooks/brain_reset_nudge.py", FAKE_NUDGE)
    _write(role, "tools/hooks/brain_state_inject.py", FAKE_STATE)
    _write(role, f"tools/hooks/{STOP_NAME}", "stop = 1\n")
    _write(role, "tools/hooks/only_old.py", "x = 1\n")
    _write(role, "tools/agentops/core.py", b"core = 'role'\r\n")
    _git(role, "add", "-A")
    _git(role, "commit", "-qm", "role v1")
    v1 = _git(role, "rev-parse", "HEAD")
    _write(role, "docs/agents/brain.md", b"NEWER brain\n")
    _write(role, "tools/hooks/later.py", "y = 2\n")
    (role / "tools/hooks/only_old.py").unlink()
    _git(role, "add", "-A")
    _git(role, "commit", "-qm", "role v2")

    case = tmp_path / "case"
    case.mkdir()
    _git(case, "init", "-q")
    _write(case, "CLAUDE.md", "case own part\n")
    _write(case, "docs/STATE.md", "state at moment\n")
    _write(case, ".claude/settings.json", _settings({"SessionStart": []}, enabledPlugins={"p": True}))
    _write(case, "tools/hooks/stale.py", "stale = 1\n")
    _write(case, "tools/hooks/only_old.py", "different\n")
    _write(case, "tools/keep.py", "keep = 1\n")
    _write(case, "tools/agentops/core.py", "core = 'moment'\n")
    _write(case, "tools/agentops/moment_only.py", "gone = 1\n")
    _write(case, "config/orchestration.json", '{"own": 1}\n')
    _git(case, "add", "-A")
    _git(case, "commit", "-qm", "moment")
    moment = _git(case, "rev-parse", "HEAD")

    draft = tmp_path / "draft"
    draft.mkdir()
    _write(draft, "brain.md", b"draft brain\r\n")
    _write(draft, "brain-reference.md", b"draft reference\n")
    _write(draft, "agent.md", b"draft agent\n")

    base = tmp_path / "trees"
    base.mkdir()
    return {"role": role, "v1": v1, "case": {"repo": str(case), "commit": moment, "key": "a"},
            "draft": draft, "base": base, "tmp": tmp_path}


@pytest.fixture
def build(world, monkeypatch):
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", world["v1"])

    def go(arm: str, n: int = 1) -> Path:  # a sealed clone (#357): nothing to detach from the case repo, pytest removes tmp_path
        return arms.make_run_tree(world["case"], arm, n, world["base"], world["role"],
                                  world["draft"] if arm == "new" else None)

    return go


def test_tree_is_a_detached_standalone_clone_named_key_arm_n(world, build):
    tree = build("old", 2)
    assert tree == world["base"] / "a-old-2"
    assert (tree / "CLAUDE.md").read_text() == "case own part\n"
    assert (tree / "docs/STATE.md").read_text() == "state at moment\n"
    assert _git(tree, "rev-parse", "HEAD") == world["case"]["commit"]
    detached = subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"], capture_output=True, timeout=60)
    assert detached.returncode != 0
    assert (tree / ".git").is_dir()  # a standalone repository, not a worktree's `.git` pointer file
    assert not (Path(world["case"]["repo"]) / ".git/worktrees").exists()


def test_old_arm_holds_the_role_commit_files_byte_exact(world, build):
    tree = build("old")
    assert (tree / "docs/agents/brain.md").read_bytes() == b"old brain\r\nline two\r\n"
    assert (tree / ".claude/agents/brain.md").read_bytes() == b"old agent\n"
    assert not (tree / "docs/agents/brain-reference.md").exists()


def test_old_arm_keeps_the_stop_entry_and_the_trees_other_settings_keys(world, build):
    settings = json.loads((build("old") / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"] == ROLE_HOOKS
    assert settings["enabledPlugins"] == {"p": True}
    assert settings["permissions"] == {"deny": []}


def test_hooks_dir_is_the_role_commits_files_only_in_tools_hooks(world, build):
    for arm in ("old", "new"):
        tree = build(arm, 1 if arm == "old" else 2)
        names = sorted(p.name for p in (tree / "tools/hooks").iterdir())
        assert names == sorted(["brain_reset_nudge.py", "brain_state_inject.py", STOP_NAME, "only_old.py"])
        assert (tree / "tools/hooks/only_old.py").read_text() == "x = 1\n"  # overwritten, byte-exact
        assert (tree / "tools/keep.py").read_text() == "keep = 1\n"  # outside tools/hooks untouched


def test_agentops_package_is_the_role_commits_and_config_stays_the_moments(world, build):
    for arm in ("old", "new"):
        tree = build(arm, 1 if arm == "old" else 2)
        assert [p.name for p in (tree / "tools/agentops").iterdir()] == ["core.py"]
        assert (tree / "tools/agentops/core.py").read_bytes() == b"core = 'role'\r\n"
        assert (tree / "config/orchestration.json").read_text() == '{"own": 1}\n'
        assert (tree / "CLAUDE.md").read_text() == "case own part\n"


def test_old_arm_hook_source_is_unedited(world, build):
    assert (build("old") / "tools/hooks/brain_reset_nudge.py").read_text() == FAKE_NUDGE


def test_new_arm_holds_the_drafts_three_files_byte_exact(world, build):
    tree = build("new")
    assert (tree / "docs/agents/brain.md").read_bytes() == b"draft brain\r\n"
    assert (tree / "docs/agents/brain-reference.md").read_bytes() == b"draft reference\n"
    assert (tree / ".claude/agents/brain.md").read_bytes() == b"draft agent\n"


def test_new_arm_has_no_stop_entry_and_keeps_every_other_key_and_hook(world, build):
    raw = (build("new") / ".claude/settings.json").read_text(encoding="utf-8")
    assert "state_claim_check" not in raw
    settings = json.loads(raw)
    assert "Stop" not in settings["hooks"]
    assert settings["hooks"]["UserPromptSubmit"] == ROLE_HOOKS["UserPromptSubmit"]
    assert settings["enabledPlugins"] == {"p": True}
    assert settings["permissions"] == {"deny": []}


def test_new_arm_edit_is_one_line_that_turns_the_reply_check_off(world, build):
    edited = (build("new") / "tools/hooks/brain_reset_nudge.py").read_text(encoding="utf-8")
    lines = edited.splitlines()
    assert len(lines) == len(FAKE_NUDGE.splitlines()) + 1
    assert [ln for ln in lines if ln not in FAKE_NUDGE.splitlines()] == [arms.REPLY_CHECK_OFF_LINE]
    namespace: dict = {}
    exec(compile(edited, "edited", "exec"), namespace)
    assert namespace["_reply_check_text"]({}, "t") is None
    # the log line the check writes is behind the new return, so it is never written
    assert edited.index(arms.REPLY_CHECK_OFF_LINE) < edited.index('return "check"')


def test_new_arm_edit_fails_loudly_when_the_hook_has_no_anchor(world, build, monkeypatch):
    _write(world["role"], "tools/hooks/brain_reset_nudge.py", "def other(): pass\n")
    _git(world["role"], "add", "-A")
    _git(world["role"], "commit", "-qm", "no anchor")
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", _git(world["role"], "rev-parse", "HEAD"))
    with pytest.raises(RuntimeError, match="reply-check"):
        arms.make_run_tree(world["case"], "new", 1, world["base"], world["role"], world["draft"])


def test_bad_arm_and_missing_draft_raise(world, build):
    with pytest.raises(ValueError):
        arms.make_run_tree(world["case"], "x", 1, world["base"], world["role"], None)
    with pytest.raises(ValueError):
        arms.make_run_tree(world["case"], "new", 1, world["base"], world["role"], None)


@pytest.mark.parametrize("arm", ["old", "new"])
def test_state_hook_never_fetches_or_reads_origin_in_both_arms_identically(world, build, arm):
    edited = (build(arm) / "tools/hooks/brain_state_inject.py").read_text(encoding="utf-8")
    assert '"fetch", "origin"' not in edited and "notes.append(FETCH_FAILED_NOTE)" not in edited
    assert "fetch = None" in edited and "pass" in edited
    lines = edited.splitlines()
    assert len(lines) == len(FAKE_STATE.splitlines())  # two lines replaced, none added or dropped


def test_state_hook_edit_fails_loudly_when_the_hook_has_no_fetch_line(world, build, monkeypatch):
    _write(world["role"], "tools/hooks/brain_state_inject.py", "def other(): pass\n")
    _git(world["role"], "add", "-A")
    _git(world["role"], "commit", "-qm", "no fetch")
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", _git(world["role"], "rev-parse", "HEAD"))
    with pytest.raises(RuntimeError, match="state hook"):
        arms.make_run_tree(world["case"], "old", 1, world["base"], world["role"], None)


def test_settings_keep_the_original_indent(world):
    case = Path(world["case"]["repo"])
    _write(case, ".claude/settings.json", _settings({"SessionStart": []}, indent=4, enabledPlugins={"p": True}))
    _git(case, "add", "-A")
    _git(case, "commit", "-qm", "indent 4")
    moved = {**world["case"], "commit": _git(case, "rev-parse", "HEAD")}
    arms_old = arms.OLD_ROLE_COMMIT  # restored below
    arms.OLD_ROLE_COMMIT = world["v1"]
    try:
        tree = arms.make_run_tree(moved, "old", 7, world["base"], world["role"], None)
    finally:
        arms.OLD_ROLE_COMMIT = arms_old
    lines = (tree / ".claude/settings.json").read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith('    "') and not lines[1].startswith("     ")


# -- integration: the 6 real trees -------------------------------------------------------------

MAIN = Path("C:/Coding/orchestration-layer")
DRAFT = MAIN / "docs/research/2026-10-23-brain-role-draft"


def _case_b_folder() -> Path | None:
    """Case b's folder from config/orchestration.json, read when a test runs; None when the config has no such target
    (the public test copy drops it), so the test skips instead of failing collection."""
    try:
        return Path(resolve_repo("config:cabinet-dashboard")[0])
    except ValueError:
        return None


def _has(repo: Path, rev: str) -> bool:
    if not repo.exists():
        return False
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", rev + "^{commit}"],
                          capture_output=True, timeout=60).returncode == 0


def _brain_line(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("Updated (brain)"))


def _run_hook(tree: Path, tmp: Path, script: str, event: str, session: str, **extra) -> subprocess.CompletedProcess:
    """One run of ``tools/hooks/<script>`` of the tree on a first brain prompt: cwd and CLAUDE_PROJECT_DIR = the
    tree, every log, marker and settings path pointed at tmp so the tree stays as built."""
    config = json.loads((tree / "config/orchestration.json").read_text(encoding="utf-8"))
    transcript = tmp / f"{tree.name}-{session}.jsonl"
    rows = [{"type": "user", "message": {"content": "hi"}},
            {"type": "custom-title", "customTitle": config["brain_chat_title"], "sessionId": "s1"}]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tree),
           "AGENTOPS_HOOK_SETTINGS": str(tree / "config/orchestration.json"),
           "AGENTOPS_HOOK_LOG": str(tmp / f"{tree.name}-{session}.log"),
           "AGENTOPS_SESSION_PAYLOAD_LOG": str(tmp / f"{tree.name}-{session}.payloads"),
           "AGENTOPS_BRAIN_INJECT_DIR": str(tmp / f"{tree.name}-{session}-inject")}
    payload = {"hook_event_name": event, "session_id": session, "transcript_path": str(transcript),
               "cwd": str(tree), "tool_name": "Bash", "tool_input": {"command": "echo hi"},
               "tool_response": {"stdout": "hi", "stderr": ""}, "source": "startup", "prompt": "hello",
               "stop_hook_active": False, **extra}
    return subprocess.run([sys.executable, str(tree / "tools/hooks" / script)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env, cwd=str(tree))


def _context(done: subprocess.CompletedProcess) -> str:
    assert done.returncode == 0, done.stderr[-1500:]
    out = [ln for ln in done.stdout.splitlines() if ln.strip()]
    return json.loads(out[0])["hookSpecificOutput"]["additionalContext"] if out else ""


def _missing_hook_scripts(tree: Path) -> list[str]:
    hooks = json.loads((tree / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"]
    commands = [h["command"] for entries in hooks.values() for e in entries for h in e.get("hooks", [])]
    return sorted({rel for c in commands for rel in re.findall(r'\$CLAUDE_PROJECT_DIR/([^"\s]+)', c)
                   if not (tree / rel).exists()})


def _every_hook_exits_zero(tree: Path, tmp: Path) -> None:
    hooks = json.loads((tree / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"]
    for event, entries in hooks.items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                for rel in re.findall(r'\$CLAUDE_PROJECT_DIR/([^"\s]+)', hook["command"]):
                    done = _run_hook(tree, tmp, Path(rel).name, event, f"all-{event}")
                    assert done.returncode == 0, f"{tree.name}: {event} {rel} exit {done.returncode}: {done.stderr[-300:]}"


def test_six_real_trees(tmp_path):
    case_b = _case_b_folder()
    if case_b is None:
        pytest.skip("case b's config target is absent here")
    if not (_has(MAIN, "ffce853") and _has(MAIN, "435c744") and _has(MAIN, "48ac6b9")
            and _has(case_b, "ae57ac1") and DRAFT.exists()):
        pytest.skip("a real repo path or commit of issue 349 is absent")
    real_cases = {
        "a": {"repo": str(MAIN), "commit": "435c744", "key": "a"},
        "b": {"repo": str(case_b), "commit": "ae57ac1", "key": "b"},
        "c": {"repo": str(MAIN), "commit": "48ac6b9", "key": "c"},
    }
    expect_old = subprocess.run(["git", "-C", str(MAIN), "show", "ffce853:docs/agents/brain.md"],
                                capture_output=True, check=True, timeout=60).stdout
    expect_new = (DRAFT / "brain.md").read_bytes()
    old_md = expect_old.decode("utf-8")
    block = old_md.split("<!-- reply-check:start -->")[1].split("<!-- reply-check:end -->")[0].strip()
    for key, case in real_cases.items():
        contexts: dict[str, str] = {}
        for arm in ("old", "new"):
            tree = arms.make_run_tree(case, arm, 1, tmp_path, MAIN, DRAFT if arm == "new" else None)
            assert tree == tmp_path / f"{key}-{arm}-1"
            assert (tree / "docs/agents/brain.md").read_bytes() == (expect_old if arm == "old" else expect_new)
            settings = (tree / ".claude/settings.json").read_text(encoding="utf-8")
            assert ("state_claim_check" in settings) == (arm == "old")
            assert _missing_hook_scripts(tree) == [], tree.name
            _every_hook_exits_zero(tree, tmp_path)

            # the injected hand-off is the moment's own docs/STATE.md, never today's origin
            own = _brain_line((tree / "docs/STATE.md").read_text(encoding="utf-8"))
            handoff = _context(_run_hook(tree, tmp_path, "brain_state_inject.py", "UserPromptSubmit", "hand"))
            assert own in handoff, f"{tree.name}: the moment's own brain line is not injected"
            today = subprocess.run(["git", "-C", str(tree), "show", "origin/main:docs/STATE.md"],
                                   capture_output=True, text=True, timeout=60)
            assert today.returncode != 0, f"{tree.name}: the sealed tree can read origin/main (#357)"

            # the first-prompt injection of brain_reset_nudge: present in both arms, the reply check in old only
            ctx = _context(_run_hook(tree, tmp_path, "brain_reset_nudge.py", "UserPromptSubmit", "first"))
            assert "you are the brain" in ctx, f"{tree.name}: no first-prompt injection"
            assert (block in ctx) == (arm == "old"), f"{tree.name}: reply-check block presence wrong"
            log_path = tmp_path / f"{tree.name}-first.log"
            log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            assert ('"reply-check"' in log) == (arm == "old"), f"{tree.name}: reply-check log line wrong"
            contexts[arm] = ctx
        assert contexts["new"] == contexts["old"].replace("\n\n" + block, ""), key  # nothing else differs
