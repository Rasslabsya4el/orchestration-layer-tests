"""#76 unit 1 (lock before reads): a push of many merges runs many core updates of one target at once. The target
lock is taken before the home reads, so an update that waited finds the target at the home's head and reads nothing
more of the home: the `gh api` calls of the whole burst are one update's, not one per attempt. Real git on a tmp
target, the home a fake `gh` runner that counts its calls by kind: no network."""

from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path

from tests.test_issue76_concurrent_core_update import (
    MANIFEST, NEW_HOME_FILES, NEW_VERSION, ORCH_JSON, SHA8, _entry, _fake_gh, _git, _lock, _target,
)
from tools.agentops.core_targets import run_core_targets

ATTEMPTS = 14


def _counting_gh(inner):
    counts: Counter = Counter()
    guard = threading.Lock()

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            kind = "head_sha"
        elif "/git/trees/" in path:
            kind = "tree"
        else:
            rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
            kind = "manifest" if rel == MANIFEST else f"file:{rel}"
        with guard:
            counts[kind] += 1
        return inner(args)

    return run, counts


def test_fourteen_attempts_on_one_target_read_each_home_file_once(tmp_path: Path) -> None:
    root = _target(tmp_path)
    gh, counts = _counting_gh(_fake_gh(NEW_HOME_FILES, head_delay=0.05))
    lines: list[list[str]] = []

    def call() -> None:
        lines.append(run_core_targets(_entry(root), home_root=tmp_path, gh_runner=gh))

    threads = [threading.Thread(target=call) for _ in range(ATTEMPTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    flat = [line for result in lines for line in result]
    assert len(flat) == ATTEMPTS
    assert not any("refused" in line or "failed" in line for line in flat)
    assert sum(line.startswith(f"owner/target: taken {SHA8}") for line in flat) == ATTEMPTS
    assert sum(line.endswith(" (by a concurrent update)") for line in flat) == ATTEMPTS - 1

    assert _git(root, "log", "--oneline").stdout.count("Core update") == 1
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not _lock(root).exists()

    files = {kind: n for kind, n in counts.items() if kind.startswith("file:")}
    assert set(files) == {"file:docs/agents/one.md", "file:docs/agents/three.md"}  # #148: two.md is the target's already
    assert counts["tree"] == 1  # one tree read for all 14 attempts
    assert all(n == 1 for n in files.values()), files  # each manifest entry served once in total, not 14 times
    assert counts["manifest"] <= 2  # one update reads it twice (the manifest, and as a manifest entry); 14 would be 28
    assert 1 <= counts["head_sha"] <= ATTEMPTS


def test_current_target_with_an_uncommitted_settings_edit_is_still_up_to_date(tmp_path: Path) -> None:
    """The dirty-path refusal belongs to an update that has something to write: a target already at the home's
    head whose own `.claude/settings.json` carries an uncommitted edit ends taken, nothing written."""
    settings = ".claude/settings.json"
    root = _target(tmp_path)
    (root / ".claude").mkdir()
    (root / settings).write_text("{}\n", encoding="utf-8")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-q", "-m", "target settings").returncode == 0
    gh = _fake_gh(NEW_HOME_FILES)
    assert run_core_targets(_entry(root), home_root=tmp_path, gh_runner=gh) == [f"owner/target: taken {SHA8}"]
    commits = _git(root, "rev-list", "--count", "HEAD").stdout.strip()
    (root / settings).write_text('{"mine": true}\n', encoding="utf-8")

    lines = run_core_targets(_entry(root), home_root=tmp_path, gh_runner=gh)

    assert lines == [f"owner/target: taken {SHA8}"]
    assert (root / settings).read_text(encoding="utf-8") == '{"mine": true}\n'
    assert _git(root, "rev-list", "--count", "HEAD").stdout.strip() == commits
