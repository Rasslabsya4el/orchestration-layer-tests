"""#190: a target's leak list holds real names only, so a plain English word of the core text is not a hit.

Seams: ``target_check.check_targets`` (the hits for a tree) and ``cli.main(["merge", ...])`` with ``--detach`` (the queue
refusal, as in test_issue110). The fake target is a folder in tmp_path whose ``config/core_leak_patterns.json`` holds
names only: the home holds no word list of its own. Neutral fixture names only."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.agentops import cli, detach, merge as merge_module
from tools.agentops.target_check import check_targets
from tools.agentops.util import no_window_flags

REPO = "acme/widgets"
NAMES = {"L1": "acme-widgets|\\bacmeco\\b", "L2": "zzqq-never", "L3": "zzqq-never-2"}
PLAIN = "// the build number stands beside the footer time\n// item, skill and tree are plain words here\n"
NAMED = "// the Acme-Widgets build number stands beside the footer time\n"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _tree(root: Path, core_text: str = "# core\nplain text\n") -> Path:
    """A merged core tree: one manifest file, `docs/agents/core.md`."""
    (root / "tools" / "agentops").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "agentops" / "core_manifest.txt").write_text("docs/agents/core.md\n", encoding="utf-8")
    (root / "docs" / "agents" / "core.md").write_text(core_text, encoding="utf-8")
    return root


def _target(folder: Path, *, patterns: dict) -> Path:
    (folder / "config").mkdir(parents=True, exist_ok=True)
    (folder / "config" / "core_leak_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    return folder


def _world(tmp_path: Path, names: dict):
    """A bare origin whose main is a tiny home: one target whose leak list is `names`, two manifest core docs."""
    target = _target(tmp_path / "t", patterns=names)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "tools" / "agentops").mkdir(parents=True)
    (seed / "docs" / "agents").mkdir(parents=True)
    (seed / "config").mkdir()
    (seed / "tools" / "agentops" / "core_manifest.txt").write_text(
        "docs/agents/f1.md\ndocs/agents/f2.md\n", encoding="utf-8")
    for name in ("f1", "f2"):
        (seed / "docs" / "agents" / f"{name}.md").write_text("# core\nplain\n", encoding="utf-8")
    settings = {"update_rule": "home", "targets": [{"repo": REPO, "folder": str(target)}]}
    (seed / "config" / "orchestration.json").write_text(json.dumps(settings), encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    return bare, checkout


def _branch(tmp_path: Path, bare: Path, name: str, doc: str, text: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / "docs" / "agents" / f"{doc}.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", f"edit {name}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Recorders: detached launches, and the merge log / results the refusal must leave untouched."""
    launched = []
    results = tmp_path / "results"
    monkeypatch.setattr(detach, "launch", lambda argv, kind, branch: launched.append(argv) or (results / "r.json", 1))
    log = tmp_path / "merge.log"
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(log))
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", log)
    return launched, results, log


def _no_trace(results: Path, log: Path):
    assert not results.exists() or list(results.iterdir()) == []
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_plain_english_words_are_no_hit_when_the_list_holds_names_only(tmp_path):
    merged = _tree(tmp_path / "merged", "# core\n" + PLAIN)
    target = _target(tmp_path / "t", patterns=NAMES)
    assert check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path) == []


def test_the_name_in_the_text_is_one_hit_that_names_file_line_and_word(tmp_path):
    merged = _tree(tmp_path / "merged", "# core\n" + NAMED)
    target = _target(tmp_path / "t", patterns=NAMES)
    hits = check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path)
    assert [h.line() for h in hits] == [f"{REPO} rejects core text: docs/agents/core.md:2 Acme-Widgets"]


def test_the_queue_refuses_a_branch_that_adds_the_name(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout = _world(tmp_path, NAMES)
    ref = _branch(tmp_path, bare, "name", "f2", "# core\n" + NAMED)
    rc = cli.main(["merge", ref, "--detach", "--issue", "190", "--cwd", str(checkout)])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out == [f"refused at queue: {REPO} rejects core text: docs/agents/f2.md:2 Acme-Widgets (branch {ref})"]
    assert launched == []
    _no_trace(results, log)


def test_a_plain_word_in_the_list_would_hit_so_the_list_holds_names_only(tmp_path):
    merged = _tree(tmp_path / "merged", "# core\nthe filler build number\n")
    target = _target(tmp_path / "t", patterns={**NAMES, "L2": "filler"})
    hits = check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path)
    assert [h.word for h in hits] == ["filler"]
