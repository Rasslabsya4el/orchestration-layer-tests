"""Tests for tools/agentops/core_settings.py (issue #147, half b)."""

from __future__ import annotations

import json

import pytest

from tools.agentops import core_settings


def test_load_reads_the_real_settings_file():
    data = core_settings.load()
    for key in core_settings.REQUIRED_KEYS:
        assert key in data


def test_missing_file_raises_one_line_naming_the_file(tmp_path):
    missing = tmp_path / "no_such_orchestration.json"
    with pytest.raises(RuntimeError) as exc_info:
        core_settings.load(missing)
    message = str(exc_info.value)
    assert str(missing) in message
    assert "\n" not in message


def test_missing_key_raises_one_line_naming_the_key(tmp_path):
    settings_path = tmp_path / "orchestration.json"
    data = {k: "x" for k in core_settings.REQUIRED_KEYS if k != "bridge_lock"}
    settings_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        core_settings.load(settings_path)
    message = str(exc_info.value)
    assert "bridge_lock" in message
    assert str(settings_path) in message
    assert "\n" not in message


def test_invalid_json_raises_one_line(tmp_path):
    settings_path = tmp_path / "orchestration.json"
    settings_path.write_text("not json", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_info:
        core_settings.load(settings_path)
    assert "\n" not in str(exc_info.value)


def test_areas_defaults_to_empty_list_when_absent(tmp_path):
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert core_settings.load(p)["areas"] == []


def test_read_run_neutral_scripts_defaults_to_empty_list_when_absent(tmp_path):
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert core_settings.load(p)["read_run_neutral_scripts"] == []
