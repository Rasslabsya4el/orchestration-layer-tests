"""#229: the research bridge, started from a linked git worktree, reads `opencode.json` of the main
working tree of the same repo when the worktree has none; it never copies the file into the worktree and
never prints its contents. An explicit `config_path` wins; absent in both places the error names both paths.

Seams: `ocserver.read_opencode_config` (with `ocserver.ROOT` patched to the worktree) and
`ocserver.bridge_unavailable_line`. Real temp git repos with a real `git worktree add`; no network, no opencode.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.agentops import ocserver
from tools.agentops.ocserver import BridgeConfigMissing, bridge_unavailable_line
from tools.agentops.util import no_window_flags

GOOD_PROVIDER = {
    "codex-sale-oai": {
        "options": {"baseURL": "https://gpt.example.com/v1"},
        "models": {"gpt-6-luna": {}},
    }
}


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        creationflags=no_window_flags(),
    ).stdout


def _write_config(path, provider):
    path.write_text(json.dumps({"provider": provider}), encoding="utf-8")


@pytest.fixture
def repo_with_worktree(tmp_path):
    """A main working tree (no opencode.json) with one linked worktree (no opencode.json)."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
         "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "--detach", str(wt))
    return main, wt


def test_row1_worktree_reads_main_tree_config_and_leaves_no_copy(repo_with_worktree, monkeypatch):
    main, wt = repo_with_worktree
    _write_config(main / "opencode.json", GOOD_PROVIDER)
    monkeypatch.setattr(ocserver, "ROOT", wt)

    data = ocserver.read_opencode_config()

    assert data["provider"] == GOOD_PROVIDER
    assert "gpt-6-luna" in data["provider"]["codex-sale-oai"]["models"]
    # row 3: the worktree holds no copy of the file and is still clean
    assert not (wt / "opencode.json").exists()
    assert _git(wt, "status", "--porcelain") == ""


def test_row2_absent_everywhere_error_names_both_paths(repo_with_worktree, monkeypatch):
    main, wt = repo_with_worktree
    monkeypatch.setattr(ocserver, "ROOT", wt)

    with pytest.raises(BridgeConfigMissing) as info:
        ocserver.read_opencode_config()

    message = str(info.value)
    assert str(wt / "opencode.json") in message
    assert str(main.resolve() / "opencode.json") in message
    line = bridge_unavailable_line(info.value)
    assert line.startswith("research bridge cannot start: ")
    assert str(wt / "opencode.json") in line
    assert str(main.resolve() / "opencode.json") in line


def test_explicit_config_path_wins_over_the_fallback(repo_with_worktree, monkeypatch, tmp_path):
    main, wt = repo_with_worktree
    _write_config(main / "opencode.json", {"main-provider": {"models": {}}})
    explicit = tmp_path / "explicit.json"
    _write_config(explicit, {"explicit-provider": {"models": {}}})
    monkeypatch.setattr(ocserver, "ROOT", wt)

    data = ocserver.read_opencode_config(explicit)

    assert list(data["provider"]) == ["explicit-provider"]


def test_explicit_config_path_missing_does_not_fall_back(repo_with_worktree, monkeypatch, tmp_path):
    main, wt = repo_with_worktree
    _write_config(main / "opencode.json", GOOD_PROVIDER)
    missing = tmp_path / "nowhere.json"
    monkeypatch.setattr(ocserver, "ROOT", wt)

    with pytest.raises(BridgeConfigMissing) as info:
        ocserver.read_opencode_config(missing)

    assert str(missing) in str(info.value)
    assert str(main.resolve()) not in str(info.value)


def test_stub_in_main_tree_counts_as_missing(repo_with_worktree, monkeypatch):
    main, wt = repo_with_worktree
    stub = {"port-stub": {"options": {"baseURL": "https://gpt.example.invalid/v1"}, "models": {}}}
    _write_config(main / "opencode.json", stub)
    monkeypatch.setattr(ocserver, "ROOT", wt)

    with pytest.raises(BridgeConfigMissing) as info:
        ocserver.read_opencode_config()

    assert "stub" in str(info.value)
    assert str(main.resolve() / "opencode.json") in str(info.value)


def test_non_worktree_repo_without_file_names_one_path(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.setattr(ocserver, "ROOT", root)

    with pytest.raises(BridgeConfigMissing) as info:
        ocserver.read_opencode_config()

    assert str(info.value).count("opencode.json") == 1
    assert str(root / "opencode.json") in str(info.value)
