"""Issue #363, unit 2: the `empty`, `architect` and `full` arms of the replay harness (`tools/replays/arms.py`).

A small fake case repo (a CLAUDE.md with an `@` import, the imported block, an agent file, a skill, a settings file with
hooks, a plain docs file) is built in tmp. The trees are built by `make_run_tree`; the launch line by `build_command`;
the deny rules by the launcher's own `_disable_memory`, which every arm shares.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools.replays import arms, launcher  # noqa: E402

STOP_NAME = "state_claim_check.py"
ARCHITECT_TEXT = b"# architect rules\r\none rule\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60)
    return done.stdout.strip()


def _write(root: Path, rel: str, data: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _cmd(script: str) -> dict:
    return {"type": "command", "command": f'python "$CLAUDE_PROJECT_DIR/tools/hooks/{script}"'}


ROLE_HOOKS = {"UserPromptSubmit": [{"hooks": [_cmd("brain_reset_nudge.py")]}],
              "Stop": [{"hooks": [_cmd(STOP_NAME)]}]}
FAKE_NUDGE = ('def _reply_check_text(payload, transcript_path):\n'
              '    """the reply check."""\n'
              '    return "check"\n')
FAKE_STATE = ('def _inject(payload, event, project):\n'
              '    fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)\n'
              '    notes.append(FETCH_FAILED_NOTE)\n')


@pytest.fixture
def world(tmp_path, monkeypatch):
    role = tmp_path / "role"
    role.mkdir()
    _git(role, "init", "-q")
    _write(role, "docs/agents/brain.md", b"old brain\n")
    _write(role, ".claude/agents/brain.md", b"old agent\n")
    _write(role, ".claude/settings.json", json.dumps({"hooks": ROLE_HOOKS}, indent=2))
    _write(role, "tools/hooks/brain_reset_nudge.py", FAKE_NUDGE)
    _write(role, "tools/hooks/brain_state_inject.py", FAKE_STATE)
    _write(role, f"tools/hooks/{STOP_NAME}", "stop = 1\n")
    _write(role, "tools/agentops/core.py", "core = 'role'\n")
    _git(role, "add", "-A")
    _git(role, "commit", "-qm", "role")
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", _git(role, "rev-parse", "HEAD"))

    case = tmp_path / "case"
    case.mkdir()
    _git(case, "init", "-q")
    _write(case, "CLAUDE.md", "case own part\n\n## Core\n\n@docs/agents/core-block.md\n")
    _write(case, "docs/agents/core-block.md", "the block, imports @nested.md\n")
    _write(case, "docs/agents/nested.md", "nested import\n")
    _write(case, "docs/agents/brain.md", "moment brain\n")
    _write(case, "docs/STATE.md", "state at moment\n")
    _write(case, ".claude/agents/brain.md", "moment agent\n")
    _write(case, ".claude/skills/x/SKILL.md", "a skill\n")
    _write(case, ".claude/settings.json",
           json.dumps({"permissions": {"deny": []}, "enabledPlugins": {"p": True},
                       "hooks": {"SessionStart": [{"hooks": [_cmd("brain_state_inject.py")]}]}}, indent=2))
    _git(case, "add", "-A")
    _git(case, "commit", "-qm", "moment")

    draft = tmp_path / "draft"
    for name in ("brain.md", "brain-reference.md", "agent.md"):
        _write(draft, name, f"draft {name}\r\n")
    rules = tmp_path / "architect-rules.md"
    rules.write_bytes(ARCHITECT_TEXT)

    harness = tmp_path / "harness"
    _write(harness, "config/orchestration.json", json.dumps({"targets": [{"folder": "C:/Coding/other", "repo": "o/o"}]}))
    return {"case_dict": {"key": "a", "repo": str(case), "commit": _git(case, "rev-parse", "HEAD")},
            "role": role, "draft": draft, "rules": rules, "base": tmp_path / "trees", "harness": harness,
            "tmp": tmp_path}


def _tree(world, arm: str) -> Path:
    return arms.make_run_tree(world["case_dict"], arm, 1, world["base"], world["role"], world["draft"],
                              architect_rules=world["rules"])


def _argv(world, arm: str) -> list[str]:
    cfg = launcher.Config(claude_bin="claude", role_repo=world["role"], draft_dir=world["draft"],
                          runs_dir=world["tmp"] / "runs", harness_root=world["harness"])
    return launcher.build_command(cfg, "hello owner", arm)


def _hooks(tree: Path):
    return json.loads((tree / ".claude/settings.json").read_text(encoding="utf-8")).get("hooks")


@pytest.mark.parametrize("arm", ["empty", "architect"])
def test_empty_and_architect_strip_the_role_and_keep_the_rest(world, arm):
    tree = _tree(world, arm)
    assert not (tree / ".claude/agents").exists()
    assert not (tree / ".claude/skills").exists()
    assert not (tree / "docs/agents/core-block.md").exists()
    assert not (tree / "docs/agents/nested.md").exists()  # an import of an import
    assert not _hooks(tree)
    settings = json.loads((tree / ".claude/settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"] == {"p": True}  # the file kept, only the hooks key emptied
    assert (tree / "docs/STATE.md").read_text(encoding="utf-8") == "state at moment\n"
    assert (tree / "docs/agents/brain.md").read_text(encoding="utf-8") == "moment brain\n"  # no overlay of the role's brain.md
    assert not (tree / "tools/hooks").exists() or not (tree / "tools/hooks/brain_reset_nudge.py").exists()


def test_empty_has_no_claude_md(world):
    assert not (_tree(world, "empty") / "CLAUDE.md").exists()


def test_architect_claude_md_is_the_given_file_byte_for_byte(world):
    assert (_tree(world, "architect") / "CLAUDE.md").read_bytes() == ARCHITECT_TEXT


def test_architect_rules_default_is_the_research_file_of_the_harness_checkout():
    assert arms.ARCHITECT_RULES_PATH == PROJECT_ROOT / "docs/research/2026-10-24-brain-replays-4/architect-rules.md"


def test_architect_without_its_file_raises_before_a_tree_is_made(world):
    with pytest.raises(FileNotFoundError):
        arms.make_run_tree(world["case_dict"], "architect", 1, world["base"], world["role"], None,
                           architect_rules=world["tmp"] / "missing.md")
    assert not world["base"].exists()


@pytest.mark.parametrize("arm", ["full", "new"])
def test_full_is_the_draft_arm(world, arm):
    tree = _tree(world, arm)
    assert (tree / "docs/agents/brain.md").read_bytes() == b"draft brain.md\r\n"
    assert (tree / ".claude/agents/brain.md").read_bytes() == b"draft agent.md\r\n"
    assert (tree / "CLAUDE.md").read_text(encoding="utf-8").startswith("case own part")
    assert STOP_NAME not in json.dumps(_hooks(tree))
    assert "return None  # replay #349" in (tree / "tools/hooks/brain_reset_nudge.py").read_text(encoding="utf-8")


def test_unknown_arm_raises(world):
    with pytest.raises(ValueError):
        _tree(world, "bogus")


@pytest.mark.parametrize("arm", ["old", "new", "full"])
def test_agent_arms_launch_with_agent_brain(world, arm):
    argv = _argv(world, arm)
    assert argv[argv.index("--agent") + 1] == "brain"


@pytest.mark.parametrize("arm", ["empty", "architect"])
def test_empty_and_architect_launch_without_agent(world, arm):
    argv = _argv(world, arm)
    assert "--agent" not in argv
    assert argv[argv.index("-p") + 1] == "hello owner"
    assert "--permission-mode" in argv


def test_launch_with_an_unknown_arm_raises(world):
    with pytest.raises(ValueError):
        _argv(world, "bogus")


@pytest.mark.parametrize("arm", ["empty", "architect", "full"])
def test_every_arm_gets_the_shared_deny_rules_and_memory_off(world, arm):
    tree = _tree(world, arm)
    launcher._disable_memory(tree, world["case_dict"]["repo"], world["harness"])
    local = json.loads((tree / ".claude/settings.local.json").read_text(encoding="utf-8"))
    assert local["autoMemoryEnabled"] is False
    deny = local["permissions"]["deny"]
    assert f"Read({world['case_dict']['repo'].replace(chr(92), '/')}/**)" in deny
    assert "Read(C:/Coding/other/**)" in deny


@pytest.mark.parametrize("arm", ["empty", "architect"])
def test_the_tree_is_still_sealed(world, arm):
    tree = _tree(world, arm)
    assert _git(tree, "remote") == ""
    assert _git(tree, "for-each-ref") == ""
