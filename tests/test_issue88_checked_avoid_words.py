"""#88: the avoid-word scan runs only on the words a target lists in its own ``checked_avoid_words`` (in
``config/core_leak_patterns.json``) and only those its GLOSSARY.md ``_Avoid_`` list also holds.

Seams: ``target_check.check_targets`` (the hits for a tree), ``merge.run_merge`` (the refusal), and
``core_check.read_patterns`` / ``run_check`` (the settings file with the list key still loads). Fake targets are
the #73 helpers; no real target is read."""
from __future__ import annotations

import json

from tools.agentops.core_check import read_patterns, run_check
from tools.agentops.merge import run_merge
from tools.agentops.target_check import check_targets

from test_issue73_target_checks import (  # noqa: F401 (merge_env is a fixture)
    REPO, SUITE, WIDGET, _branch, _git, _target, _tree, _world, merge_env)


def _hits(merged, target, home):
    return [h.line() for h in check_targets(merged, [{"repo": REPO, "folder": str(target)}], home)]


def test_1_a_checked_word_that_the_glossary_avoids_is_a_hit(tmp_path):
    merged = _tree(tmp_path / "merged", "# core\nthe widget here\n")
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["widget"]}, avoid="widget")
    assert _hits(merged, target, tmp_path) == [f"{REPO} rejects core text: docs/agents/core.md:2 widget"]


def test_1_merge_path_refuses_it_and_main_does_not_move(tmp_path, merge_env):
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["widget"]}, avoid="widget")
    bare, checkout, lock = _world(tmp_path, target)
    ref = _branch(tmp_path, bare, "# core\nthe widget\n")
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert not result.ok and not result.pushed
    assert result.message == f"refused: {REPO} rejects core text: docs/agents/core.md:2 widget"
    assert _git(bare, "rev-parse", "main") == main_before


def test_2_a_glossary_avoid_word_not_in_the_checked_list_is_no_hit(tmp_path):
    merged = _tree(tmp_path / "merged", "the widget\n")
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["gizmo"]}, avoid="widget")
    assert _hits(merged, target, tmp_path) == []


def test_3_a_checked_word_not_in_the_glossary_avoid_list_is_no_hit(tmp_path):
    merged = _tree(tmp_path / "merged", "the widget\n")
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["widget"]}, avoid="gizmo")
    assert _hits(merged, target, tmp_path) == []


def test_4_no_checked_key_scans_no_avoid_word_whatever_the_glossary_holds(tmp_path):
    merged = _tree(tmp_path / "merged", "the widget gizmo hand-off\n")
    with_patterns = _target(tmp_path / "t", patterns={"L1": "zzqq-never"}, avoid="widget, gizmo, hand-off")
    without_file = _target(tmp_path / "bare", avoid="widget, gizmo, hand-off")
    assert _hits(merged, with_patterns, tmp_path) == []
    assert _hits(merged, without_file, tmp_path) == []


def test_5_the_settings_file_with_the_list_key_loads_and_l1_to_l3_behave_as_before(tmp_path):
    settings = {**WIDGET, "checked_avoid_words": ["widget", "gizmo"]}
    settings_path = tmp_path / "core_leak_patterns.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    assert set(read_patterns(settings_path)) == {"L1", "L2", "L3"}
    assert read_patterns(settings_path)["L1"].search("Widget") is not None

    merged = _tree(tmp_path / "merged", "# core\nuse the Widget here\n")
    rows, ok = run_check(merged, merged / "tools" / "agentops" / "core_manifest.txt", settings_path)
    assert ok is False and rows[0].counts.get("L1") == 1

    target = _target(tmp_path / "t", patterns=settings, avoid="gizmo")
    assert _hits(merged, target, tmp_path) == [f"{REPO} rejects core text: docs/agents/core.md:2 Widget"]
