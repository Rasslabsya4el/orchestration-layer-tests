"""Tests for the `mirror_repos` setting (issue #425 unit 2): the list of repos the dispatcher mirrors."""

from __future__ import annotations

import json

import pytest

from tools.agentops import core_settings


def _settings_file(tmp_path, **extra):
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    data.update(extra)
    p = tmp_path / "orchestration.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_mirror_repos_defaults_to_empty_list_when_absent(tmp_path):
    assert core_settings.mirror_repos(_settings_file(tmp_path)) == []


def test_mirror_repos_returns_the_listed_repos(tmp_path):
    p = _settings_file(tmp_path, mirror_repos=["some-owner/some-repo", "owner.name/repo_2"])
    assert core_settings.mirror_repos(p) == ["some-owner/some-repo", "owner.name/repo_2"]


@pytest.mark.parametrize(
    "bad",
    ["some-owner/some-repo", ["noslash"], ["a/b/c"], ["/name"], ["owner/"], [5], [""], [None]],
)
def test_mirror_repos_bad_value_raises_value_error_naming_the_setting(tmp_path, bad):
    p = _settings_file(tmp_path, mirror_repos=bad)
    with pytest.raises(ValueError) as exc_info:
        core_settings.mirror_repos(p)
    assert "mirror_repos" in str(exc_info.value)


def test_real_settings_file_lists_orch_it_as_the_mirrored_repo():
    settings = core_settings.load(core_settings.SETTINGS_PATH)
    owner = settings["home"].split("/")[0]
    assert settings["mirror_repos"] == [owner + "/orch-it"]
    assert core_settings.mirror_repos(core_settings.SETTINGS_PATH) == [owner + "/orch-it"]


def test_glossary_has_the_mirror_entry():
    text = (core_settings.ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    assert "**Mirror**:" in text
