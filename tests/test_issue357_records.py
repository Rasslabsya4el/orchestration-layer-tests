"""Issue #357: the records dir of the replay harness.

``arms.make_run_tree(..., records_dir=...)`` copies ``<records_dir>/<case>/<relative path>`` byte-for-byte into the run
tree after the arm's overlay, for both arms. ``Config.records_dir`` and the ``run --records-dir`` flag reach it, and
``launcher.CASE_REPOS`` names the cases d and e.

Builds a tiny role repo and case repo in tmp_path, the same shape as tests/test_issue349_arms.py. No network, no real
claude; ``run_one`` is stopped by a spy on ``launcher.make_run_tree`` right after the call.
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
from tools.replays.launcher import Config, main, run_one  # noqa: E402

STOP_NAME = "state_claim_check.py"

ROLE_HOOKS = {
    "UserPromptSubmit": [{"hooks": [{"type": "command",
                                     "command": 'python "$CLAUDE_PROJECT_DIR/tools/hooks/brain_reset_nudge.py"'}]}],
    "Stop": [{"hooks": [{"type": "command", "command": f'python "$CLAUDE_PROJECT_DIR/tools/hooks/{STOP_NAME}"'}]}],
}

#: stand-ins for the two hooks the arms edit: the reply check's docstring anchor and the state hook's fetch lines.
FAKE_NUDGE = (
    'def _reply_check_text(payload, transcript_path):\n'
    '    """Issue #65: the reply check."""\n'
    '    return "check"\n'
)
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


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=60)
    return done.stdout.strip()


def _put(root: Path, rel: str, data: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _files(tree: Path) -> list[str]:
    """Every file of a tree as a sorted posix relative path, ``.git`` left out."""
    return sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(tree).parts)


@pytest.fixture
def world(tmp_path, monkeypatch):
    role = tmp_path / "role"
    role.mkdir()
    _git(role, "init", "-q")
    _put(role, "docs/agents/brain.md", b"old brain\r\n")
    _put(role, ".claude/agents/brain.md", b"old agent\n")
    _put(role, ".claude/settings.json", json.dumps({"hooks": ROLE_HOOKS}, indent=2))
    _put(role, "tools/hooks/brain_reset_nudge.py", FAKE_NUDGE)
    _put(role, "tools/hooks/brain_state_inject.py", FAKE_STATE)
    _put(role, f"tools/hooks/{STOP_NAME}", "stop = 1\n")
    _put(role, "tools/agentops/core.py", "core = 'role'\n")
    _git(role, "add", "-A")
    _git(role, "commit", "-qm", "role")
    monkeypatch.setattr(arms, "OLD_ROLE_COMMIT", _git(role, "rev-parse", "HEAD"))

    case = tmp_path / "case"
    case.mkdir()
    _git(case, "init", "-q")
    _put(case, "CLAUDE.md", "case own part\n")
    _put(case, "docs/STATE.md", "state at moment\n")
    _put(case, ".claude/settings.json", json.dumps({"permissions": {"deny": []}}))
    _put(case, "tools/keep.py", "keep = 1\n")
    _git(case, "add", "-A")
    _git(case, "commit", "-qm", "moment")

    draft = tmp_path / "draft"
    _put(draft, "brain.md", b"draft brain\r\n")
    _put(draft, "brain-reference.md", b"draft reference\n")
    _put(draft, "agent.md", b"draft agent\n")

    base = tmp_path / "trees"
    base.mkdir()
    return {"role": role, "case": {"repo": str(case), "commit": _git(case, "rev-parse", "HEAD"), "key": "a"},
            "draft": draft, "base": base, "tmp": tmp_path}


@pytest.fixture
def build(world):
    def go(arm: str, n: int = 1, records_dir=None) -> Path:  # a sealed clone (#357): pytest removes tmp_path
        return arms.make_run_tree(world["case"], arm, n, world["base"], world["role"],
                                  world["draft"] if arm == "new" else None, records_dir=records_dir)

    return go


def test_records_of_the_case_are_copied_byte_for_byte_into_both_arms(world, build):
    rec = world["tmp"] / "records"
    _put(rec, "a/top.txt", b"top\r\n")
    _put(rec, "a/notes/deep/sample.bin", b"\x00\xff\r\nraw\x80")
    trees = {"old": build("old", 1, records_dir=rec), "new": build("new", 2, records_dir=rec)}
    for rel in ("top.txt", "notes/deep/sample.bin"):
        source = (rec / "a" / rel).read_bytes()
        assert (trees["old"] / rel).read_bytes() == source
        assert (trees["new"] / rel).read_bytes() == source


def test_records_are_copied_after_the_overlay_so_they_win_on_a_shared_path(world, build):
    rec = world["tmp"] / "records"
    _put(rec, "a/tools/hooks/brain_state_inject.py", b"record hook\n")
    for arm, n in (("old", 1), ("new", 2)):
        assert (build(arm, n, records_dir=rec) / "tools/hooks/brain_state_inject.py").read_bytes() == b"record hook\n"


def test_other_cases_folders_and_root_files_of_the_records_dir_are_not_copied(world, build):
    rec = world["tmp"] / "records"
    _put(rec, "a/only_a.txt", b"a")
    _put(rec, "b/only_b.txt", b"b")
    _put(rec, "b/sub/only_b2.txt", b"b2")
    _put(rec, "c/only_c.txt", b"c")
    _put(rec, "only_root.txt", b"root")
    tree = build("old", 1, records_dir=rec)
    assert (tree / "only_a.txt").read_bytes() == b"a"
    for name in ("only_b.txt", "sub", "b", "only_c.txt", "c", "only_root.txt"):
        assert not (tree / name).exists(), name


def test_missing_records_dir_or_case_folder_copies_nothing(world, build):
    rec = world["tmp"] / "records"  # does not exist yet
    bare = _files(build("old", 1))
    assert _files(build("old", 2, records_dir=rec)) == bare
    _put(rec, "b/only_b.txt", b"b")  # the records dir exists, the case's folder does not
    assert _files(build("new", 3, records_dir=rec)) == _files(build("new", 4))


@pytest.mark.parametrize("rel", ["../escape.txt", "a/../../escape.txt", "/escape.txt", "C:/escape.txt"])
def test_a_relative_path_that_leaves_the_tree_is_refused_before_anything_is_copied(world, build, monkeypatch, rel):
    rec = world["tmp"] / "records"
    _put(rec, "a/inside.txt", b"x")
    monkeypatch.setattr(arms, "_record_rels", lambda folder: ["inside.txt", rel])
    with pytest.raises(ValueError):
        build("old", 1, records_dir=rec)
    assert not (world["base"] / "escape.txt").exists()
    assert not (world["base"] / "a-old-1" / "inside.txt").exists()


def test_config_records_dir_reaches_make_run_tree_through_run_one(world, tmp_path, monkeypatch):
    seen: dict = {}

    class Stop(Exception):
        pass

    def spy(case, arm, n, base_dir, role_repo, draft_dir, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(launcher, "make_run_tree", spy)
    cfg = _cfg(tmp_path, records_dir=str(tmp_path / "records"))
    assert cfg.records_dir == tmp_path / "records"
    with pytest.raises(Stop):
        run_one("a", "old", 1, cfg)
    assert seen == {"records_dir": tmp_path / "records"}


def test_without_records_dir_make_run_tree_gets_no_records_argument(tmp_path, monkeypatch):
    seen: dict = {}

    class Stop(Exception):
        pass

    def spy(case, arm, n, base_dir, role_repo, draft_dir, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(launcher, "make_run_tree", spy)
    cfg = _cfg(tmp_path)
    assert cfg.records_dir is None
    with pytest.raises(Stop):
        run_one("a", "old", 1, cfg)
    assert seen == {}


def test_case_repos_names_cases_d_and_e_on_the_home_repo():
    assert launcher.CASE_REPOS["d"] == launcher.HOME_SLUG
    assert launcher.CASE_REPOS["e"] == launcher.HOME_SLUG
    assert launcher.CASE_REPOS["a"] == launcher.HOME_SLUG
    assert launcher.CASE_REPOS["b"] == "config:cabinet-dashboard"
    assert launcher.CASE_REPOS["c"] == launcher.HOME_SLUG


def test_run_takes_records_dir_and_hands_it_to_the_config(tmp_path, monkeypatch):
    seen: list[Config] = []

    def fake_run_all(cfg, **kwargs):
        seen.append(cfg)
        cfg.runs_dir.mkdir(parents=True, exist_ok=True)
        (cfg.runs_dir / "progress.json").write_text("{}", encoding="utf-8")
        return []

    monkeypatch.setattr(launcher, "run_all", fake_run_all)
    common = ["run", "--claude-bin", "fake.py", "--runs-dir", str(tmp_path / "runs"),
              "--trees-dir", str(tmp_path / "trees")]
    assert main([*common, "--records-dir", str(tmp_path / "records")]) == 0
    assert main(common) == 0
    assert seen[0].records_dir == tmp_path / "records"
    assert seen[1].records_dir is None


def _cfg(tmp_path: Path, **kwargs) -> Config:
    (tmp_path / "harness" / "config").mkdir(parents=True, exist_ok=True)  # the deny list reads it (#357 unit 5)
    (tmp_path / "harness" / "config" / "orchestration.json").write_text('{"targets": []}', encoding="utf-8")
    return Config(claude_bin="unused.py", role_repo=tmp_path / "role", draft_dir=tmp_path / "draft",
                  runs_dir=tmp_path / "runs", trees_dir=tmp_path / "trees", harness_root=tmp_path / "harness",
                  projects_dir=tmp_path / "projects", state_dir=tmp_path / "state", **kwargs)
