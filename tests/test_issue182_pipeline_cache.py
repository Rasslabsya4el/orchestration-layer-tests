"""#182: the Progress pipeline reads its ticket facts from the folder the pane reads, never from the repo's test cache.

Before: the pipeline's ticket facts and label cache were fixed at import under the repo's `mods/pob-slots/bin/.cache`. The
`serve` collector's fast pull read the repo file, which only the tickets pull writes and the tests overwrite with the
`t1` fixture, while the pane's real facts sat in the install folder. Now both paths resolve at use time, beside
`collector-fast.json` (`fast_path().parent`): the install folder when the collector writes there, else CACHE_DIR.

No PoB, no network, no process started: `gh` is a counting fake, the claude / opencode / machine reads are stubs, and
the install root and the repo cache are tmp dirs. The "real" folder of the bug (the import-time repo cache) is only
snapshotted, never created by this test.

    python -m pytest -q -p no:faulthandler tests/test_issue182_pipeline_cache.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

# the repo's own cache folder as the module fixed it at import: the folder the bug wrote into
REAL_REPO_CACHE = Path(collect.__file__).resolve().parent / ".cache"

# the test facts the repo folder holds (the golden fixture's `t1` ticket, in the facts' shape)
T1_FACTS = {"1": {"title": "t1", "open": True, "labels": ["ready-for-agent"], "blockers": []}}
# the pane's real facts, in the install folder
REAL_FACTS = {
    "7": {"title": "Real ticket seven", "open": True, "labels": ["ready-for-agent"], "blockers": []},
    "9": {"title": "Real ticket nine", "open": True, "labels": ["ready-for-agent"], "blockers": []},
}
GRAPHQL_PAGE = json.dumps({"data": {"repository": {"issues": {
    "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}})


def _plugin_id() -> str:
    from tools.agentops import install_folder

    return install_folder.plugin_id(ROOT)


def _write_facts(folder: Path, facts: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pipeline-tickets.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")


def _snapshot(folder: Path):
    """{relative path: (mtime_ns, bytes)} of the folder's files, or None when the folder is absent."""
    if not folder.is_dir():
        return None
    return {str(p.relative_to(folder)): (p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def serve_beside_pane(tmp_path, monkeypatch):
    """The `serve` collector's cache (CACHE_DIR at its default, here a tmp folder holding the `t1` facts) and the
    installed pane's folder (AGENTOPS_MODS_DIR, a tmp root with `<plugin id>/`, holding the real facts)."""
    repo_cache = tmp_path / "repo" / ".cache"
    _write_facts(repo_cache, T1_FACTS)
    install_root = tmp_path / "mods"
    install_cache = install_root / _plugin_id() / "bin" / ".cache"
    _write_facts(install_cache, REAL_FACTS)
    monkeypatch.delenv("MONITOR_CACHE_DIR", raising=False)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(install_root))
    monkeypatch.setattr(collect, "_DEFAULT_CACHE_DIR", repo_cache)
    monkeypatch.setattr(collect, "CACHE_DIR", repo_cache)
    return SimpleNamespace(repo_cache=repo_cache, install_cache=install_cache)


def test_serve_fast_pull_shows_the_pane_facts_not_the_repo_test_cache(serve_beside_pane, monkeypatch):
    assert collect.fast_path() == serve_beside_pane.install_cache / "collector-fast.json"
    # the fast pull's other reads are stubbed: no gh, no claude, no opencode, no machine process
    monkeypatch.setattr(collect, "agents", lambda: [])
    monkeypatch.setattr(collect, "merge_status", lambda: None)
    monkeypatch.setattr(collect, "machine", lambda *a, **k: {})
    monkeypatch.setattr(collect, "addon", lambda: None)
    monkeypatch.setattr(collect, "_orch_version", lambda: None)
    monkeypatch.setattr(collect, "_outcome_units", lambda: {})
    monkeypatch.setattr(collect, "_pipeline_row_times", lambda *a, **k: None)  # the label-time calls: gh, not asked

    out = collect.fast()

    assert "pipeline_error" not in out, out.get("pipeline_error")
    rows = {row["number"]: row["title"] for row in out["pipeline"]}
    assert rows == {7: "Real ticket seven", 9: "Real ticket nine"}
    assert "t1" not in {row["title"] for row in out["pipeline"]}


class _TicketsGh:
    """`gh` for one tickets pull with no issues: the open read answers an empty page, the closed list is empty, and
    every other command fails at once (the merge line reads as absent)."""

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 0, GRAPHQL_PAGE, "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return subprocess.CompletedProcess(cmd, 1, "", "no gh in this test")


def test_tickets_pull_leaves_the_real_repo_cache_and_the_install_folder_alone(tmp_path, monkeypatch):
    fake_install = tmp_path / "mods" / _plugin_id() / "bin" / ".cache"
    _write_facts(fake_install, REAL_FACTS)
    (fake_install / "collector-fast.json").write_text('{"at_ms": 1}', encoding="utf-8")
    for path in fake_install.iterdir():
        os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    before_install = _snapshot(fake_install)
    before_real = _snapshot(REAL_REPO_CACHE)

    test_cache = tmp_path / "test-cache"
    monkeypatch.delenv("MONITOR_CACHE_DIR", raising=False)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(tmp_path / "mods"))
    monkeypatch.setattr(collect, "CACHE_DIR", test_cache)
    monkeypatch.setattr(collect.subprocess, "run", _TicketsGh())

    collect.tickets()

    assert (test_cache / "pipeline-tickets.json").is_file(), "the tickets pull writes its facts into CACHE_DIR"
    assert _snapshot(fake_install) == before_install, "the install folder changed under a test"
    assert _snapshot(REAL_REPO_CACHE) == before_real, "the repo's real cache folder changed under a test"


def test_every_collector_cache_path_follows_a_moved_cache_dir(tmp_path, monkeypatch):
    """The two other import-time paths of the bug's pattern (opencode launch cache, orch cache) follow CACHE_DIR."""
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path)
    assert collect._oc_launch_cache() == tmp_path / "opencode_launch.json"
    assert collect._orch_cache() == tmp_path / "orch.json"
