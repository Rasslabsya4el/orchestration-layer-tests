"""Test support (#96): the one-area rule off for a test file that imports `_no_area_rule`.

A core test file that drives the dispatcher does `from tests._area_rule import _no_area_rule  # noqa: F401`:
importing an autouse fixture into the module namespace activates it for every test of that file.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_area_rule(monkeypatch):
    """#96: the one-area rule (`areas` of the target's config/orchestration.json) is off in every test of the
    importing file; a test of the rule patches `area_labels` or `_load_settings` itself, so the target's config
    never decides."""

    import tools.agentops.dispatch as dispatch

    real = dispatch._load_settings
    monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "areas": []})
