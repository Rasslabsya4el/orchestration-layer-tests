"""Issue #323: `tickets_repo` and `notes_repo` settings (default: the repo itself / the tickets repo)."""

from __future__ import annotations

import json

from tools.agentops import core_settings


def _write(tmp_path, **extra):
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["repo"] = "owner/code"
    data.update(extra)
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_without_the_setting_tickets_and_notes_are_the_repo_itself(tmp_path):
    path = _write(tmp_path)
    assert core_settings.tickets_repo(path) == "owner/code"
    assert core_settings.notes_repo(path) == "owner/code"


def test_tickets_repo_set_notes_follow_it(tmp_path):
    path = _write(tmp_path, tickets_repo="owner/work")
    assert core_settings.tickets_repo(path) == "owner/work"
    assert core_settings.notes_repo(path) == "owner/work"
    assert core_settings.load(path)["repo"] == "owner/code"


def test_notes_repo_set_on_its_own(tmp_path):
    path = _write(tmp_path, tickets_repo="owner/work", notes_repo="owner/notes")
    assert core_settings.notes_repo(path) == "owner/notes"
