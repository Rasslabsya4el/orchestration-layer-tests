"""Issue #40, unit 1 (data paths): the ``suite_free_paths`` setting decides which commits need no suite.

Seams: ``merge.commit_needs_suite`` (rows 2 and 5), the settings loader ``core_settings.load`` that
reads the key (row 5), and ``merge._docs_only_since`` on a real tmp git repo (row 6).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.agentops import core_settings, merge

DEFAULTS = ["docs/**/*.md", "config/*.json", "GLOSSARY.md", "CODING_STANDARDS.md", "docs/STATE.md"]


def _settings_file(tmp_path, monkeypatch, **extra):
    """A settings file with every required key, plus ``extra``; the loader reads it through SETTINGS_PATH."""
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    data.update(extra)
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


def test_commit_needs_suite_settings_file_with_code_needs_suite(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch)
    assert merge.commit_needs_suite(["config/orchestration.json", "tools/agentops/x.py"]) is True


def test_commit_needs_suite_settings_file_alone_is_suite_free(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch)
    assert merge.commit_needs_suite(["config/orchestration.json"]) is False


def test_key_absent_gives_the_five_core_defaults(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch)
    assert core_settings.load()["suite_free_paths"] == DEFAULTS
    assert merge.commit_needs_suite(["docs/a/b.md", "config/x.json", "GLOSSARY.md",
                                     "CODING_STANDARDS.md", "docs/STATE.md"]) is False
    assert merge.commit_needs_suite(["README.md"]) is True
    assert merge.commit_needs_suite(["config/sub/x.json"]) is True  # config/*.json is one level only
    assert merge.commit_needs_suite(["docs/x.txt"]) is True


def test_extra_glob_is_a_data_path_and_defaults_stay(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, suite_free_paths=["data/*.csv"])
    assert core_settings.load()["suite_free_paths"] == [*DEFAULTS, "data/*.csv"]
    assert merge.commit_needs_suite(["data/x.csv"]) is False
    assert merge.commit_needs_suite(["config/orchestration.json"]) is False
    assert merge.commit_needs_suite(["tools/agentops/x.py"]) is True


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, rel, text, msg):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("data_path", ["config/orchestration.json", "docs/agents/brain.md"])
def test_docs_only_since_data_commit_reuses_green_suite(tmp_path, monkeypatch, data_path):
    _settings_file(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    base = _commit(repo, "tools/agentops/x.py", "x = 1\n", "code")
    moved = _commit(repo, data_path, "{}\n", "data")
    assert merge._docs_only_since(repo, base, moved) is True


def test_docs_only_since_code_commit_still_needs_the_suite(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    base = _commit(repo, "tools/agentops/x.py", "x = 1\n", "code")
    moved = _commit(repo, "tools/agentops/x.py", "x = 2\n", "code again")
    assert merge._docs_only_since(repo, base, moved) is False
