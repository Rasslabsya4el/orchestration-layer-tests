"""#340 unit 14: the collector hands the page the add-on's neutral payload keys.

A repo's add-on file written before the page went neutral still emits its own key names. The repo's
`monitor.addon_aliases` (`keys`, `kinds`, `titles`) says how `collect.fast()` translates them: a legacy payload gives
the neutral keys (a renamed top-level section titled by `titles` when it names none), a payload that already uses the
neutral keys passes unchanged, where both stand the neutral one wins, and a repo with no aliases is not touched.
(Core text names no repo word, so the legacy words live here, in the test's config, and in config/orchestration.json.)

    python -m pytest -q tests/test_issue340_addon_legacy_keys.py
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_FOUND = sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))
assert len(_FOUND) == 1, f"the collector is expected in exactly one mod, found {_FOUND}"
spec = importlib.util.spec_from_file_location("collect340_legacy", _FOUND[0])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)


ALIASES = {"keys": {"laptop": "remote", "slots": "lanes", "slots_total": "lanes_total", "slots_in_use": "lanes_in_use"},
           "kinds": {"slots": "lanes"}, "titles": {"remote": "Laptop"}}


def _fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, aliases: dict | None = ALIASES,
          machine_pids=lambda ctx: (set(), None)) -> dict:
    """`collect.fast()` with an add-on whose `fast` answers `payload` and nothing else slow."""
    addon = SimpleNamespace(fast=lambda ctx: payload, machine_pids=machine_pids)
    monkeypatch.setattr(collect, "REPO", tmp_path)
    monkeypatch.setattr(collect, "settings", lambda root=None: {"monitor": {"addon_aliases": aliases} if aliases else {}})
    monkeypatch.setattr(collect, "addon", lambda: addon)
    monkeypatch.setattr(collect, "agents", lambda: [])
    monkeypatch.setattr(collect, "merge_status", lambda: {"missing": True})
    monkeypatch.setattr(collect, "machine", lambda *a, **k: {})
    return collect.fast()


def _legacy() -> dict:
    return {
        "runs": {"slots": [{"role": "queue"}], "daemon": {"slots_total": 6, "slots_in_use": 1},
                 "queued": [{"wait": {"kind": "slots"}}, {"wait": {"kind": "ram"}}]},
        "laptop": {"offline": {"slots_total": 4}, "runs": {"slots": [], "daemon": {"slots_total": 2}}},
    }


def test_a_legacy_payload_gives_the_neutral_keys(monkeypatch, tmp_path) -> None:
    out = _fast(monkeypatch, tmp_path, _legacy())
    assert "laptop" not in out and "slots" not in out["runs"]
    assert out["runs"]["lanes"] == [{"role": "queue"}]
    assert out["runs"]["daemon"] == {"lanes_total": 6, "lanes_in_use": 1}
    assert [j["wait"]["kind"] for j in out["runs"]["queued"]] == ["lanes", "ram"]
    assert out["remote"]["title"] == "Laptop"
    assert out["remote"]["offline"] == {"lanes_total": 4}
    assert out["remote"]["runs"] == {"lanes": [], "daemon": {"lanes_total": 2}}


def test_a_neutral_payload_passes_unchanged(monkeypatch, tmp_path) -> None:
    neutral = {"runs": {"lanes": [], "daemon": {"lanes_total": 6}, "queued": [{"wait": {"kind": "lanes"}}]},
               "remote": {"title": "Build host", "offline": {"lanes_total": 4}}}
    out = _fast(monkeypatch, tmp_path, {**neutral})
    assert {k: out[k] for k in neutral} == neutral


def test_a_mixed_payload_keeps_the_neutral_keys(monkeypatch, tmp_path) -> None:
    mixed = {"runs": {"slots": ["old"], "lanes": ["new"], "daemon": {"slots_total": 9, "lanes_total": 6}},
             "laptop": {"title": "Old"}, "remote": {"title": "New"}}
    out = _fast(monkeypatch, tmp_path, mixed)
    assert "laptop" not in out and out["remote"] == {"title": "New"}
    assert out["runs"]["lanes"] == ["new"] and "slots" not in out["runs"]
    assert out["runs"]["daemon"] == {"lanes_total": 6}


def test_a_repo_with_no_aliases_is_not_touched(monkeypatch, tmp_path) -> None:
    out = _fast(monkeypatch, tmp_path, _legacy(), aliases=None)
    assert out["laptop"] == _legacy()["laptop"] and out["runs"] == _legacy()["runs"]


def test_the_add_on_own_helpers_still_read_the_raw_keys(monkeypatch, tmp_path) -> None:
    seen = {}

    def machine_pids(ctx):
        seen["laptop"] = ctx.sections.get("laptop")
        seen["slots_in_use"] = ctx.sections["runs"]["daemon"]["slots_in_use"]
        return set(), None

    out = _fast(monkeypatch, tmp_path, _legacy(), machine_pids=machine_pids)
    assert seen["laptop"] == _legacy()["laptop"] and seen["slots_in_use"] == 1
    assert "laptop" not in out and out["runs"]["daemon"]["lanes_in_use"] == 1


def _legacy_copy(node, aliases):
    """The neutral fixture with the aliases in reverse: the shape a repo's own add-on file emits."""
    keys = {new: old for old, new in aliases["keys"].items()}
    kinds = {new: old for old, new in aliases["kinds"].items()}
    if isinstance(node, list):
        return [_legacy_copy(v, aliases) for v in node]
    if not isinstance(node, dict):
        return node
    out = {keys.get(k, k): _legacy_copy(v, aliases) for k, v in node.items()}
    wait = out.get("wait")
    if isinstance(wait, dict) and wait.get("kind") in kinds:
        out["wait"] = {**wait, "kind": kinds[wait["kind"]]}
    return out


def test_the_real_fast_fixture_in_its_legacy_shape_comes_back_neutral_and_nothing_else_changes(monkeypatch, tmp_path) -> None:
    neutral = json.loads((Path(__file__).parent / "fixtures" / "monitor" / "addon_fast.json").read_text(encoding="utf-8"))
    neutral["runs"]["queued"][0]["wait"] = {"kind": "lanes"}
    legacy = _legacy_copy(neutral, ALIASES)
    # the legacy words are built from parts, so no test file spells them as a key
    assert "lap" + "top" in legacy and "slo" + "ts" in legacy["runs"] and "remote" not in legacy
    assert legacy["runs"]["queued"][0]["wait"]["kind"] == "slo" + "ts"
    out = _fast(monkeypatch, tmp_path, copy.deepcopy(legacy))
    expected = copy.deepcopy(neutral)
    expected["remote"] = {"title": "Laptop", **expected["remote"]}
    own = [k for k in expected if k not in ("machine", "version", "agents", "merge", "pipeline", "tickets")]  # fast() sets these
    assert own and {k: out[k] for k in own} == {k: expected[k] for k in own}
