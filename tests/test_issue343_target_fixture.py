"""#343 unit 6 (acceptance row 8): a target repo takes the change through ``core update`` with no edit of its own. The
routing of prequeue's full suite and of red-on-base comes from the settings dict alone: a target fixture of another
name and path (a folder named like a target, not this repo) gives the same place as this repo's settings, and no
repo name, path or test name is written in the two modules."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import core_settings, prequeue, red_on_base
from tools.agentops.prequeue import Seams as PrequeueSeams
from tools.agentops.red_on_base import Seams as RedOnBaseSeams

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
MODULES = (Path(prequeue.__file__), Path(red_on_base.__file__))
def _home_words() -> tuple:
    """The names of this repo and of its targets, read from the settings: none is written here."""
    cfg = core_settings.load()
    names = [cfg.get("repo", "")] + [t.get("repo", "") for t in cfg.get("targets", [])]
    return tuple(sorted({part for name in names for part in name.split("/") if part}))


HOME_WORDS = _home_words()


def _settings(**over) -> dict:
    return {**core_settings.DEFAULTS, **over}


def _target_root(tmp_path: Path) -> Path:
    root = tmp_path / "Some-Target-Shop"
    root.mkdir()
    return root


def test_prequeue_routes_a_target_fixture_by_its_settings_only(tmp_path):
    target = _settings(github_suite=True, repo="someone/some-target", test_copy_remote="https://github.com/someone/copy.git")
    where, reason, plan = prequeue.full_suite_place(target, _target_root(tmp_path),
                                                    PrequeueSeams(clock=lambda: NOW))
    assert (where, reason) == ("github", "")
    assert plan.repo_name == "Some-Target-Shop"


def test_red_on_base_routes_a_target_fixture_by_its_settings_only(tmp_path):
    target = _settings(github_suite=True, repo="someone/some-target", test_copy_remote="https://github.com/someone/copy.git")
    where, reason, plan = red_on_base.base_place(target, _target_root(tmp_path), RedOnBaseSeams(clock=lambda: NOW))
    assert (where, reason) == ("github", "")
    assert plan.repo_name == "Some-Target-Shop"


def test_a_target_with_github_suite_off_runs_on_the_pc_with_the_same_reason_as_home(tmp_path):
    target = _settings(github_suite=False, repo="someone/some-target")
    home = _settings(github_suite=False, repo="o/r")
    root = _target_root(tmp_path)
    assert prequeue.full_suite_place(target, root, PrequeueSeams(clock=lambda: NOW))[:2] == ("pc", "github_suite off")
    assert prequeue.full_suite_place(home, root, PrequeueSeams(clock=lambda: NOW))[:2] == ("pc", "github_suite off")
    assert red_on_base.base_place(target, root, RedOnBaseSeams(clock=lambda: NOW))[:2] == ("pc", "github_suite off")
    assert red_on_base.base_place(home, root, RedOnBaseSeams(clock=lambda: NOW))[:2] == ("pc", "github_suite off")


def test_the_target_and_home_settings_give_the_same_routing(tmp_path):
    root = _target_root(tmp_path)
    for flags in ({"github_suite": True, "test_copy_remote": "https://github.com/o/t.git"},
                  {"github_suite": False}):
        home = prequeue.full_suite_place(_settings(repo="o/r", **flags), root, PrequeueSeams(clock=lambda: NOW))
        other = prequeue.full_suite_place(_settings(repo="x/y", **flags), root, PrequeueSeams(clock=lambda: NOW))
        assert home[:2] == other[:2]


def test_no_repo_name_is_written_in_the_routing_modules():
    for module in MODULES:
        text = module.read_text(encoding="utf-8")
        for word in HOME_WORDS:
            assert word not in text, f"{word} in {module.name}"
