"""Issue #1080: the core names no Pocock skill the installed plugin lacks.

Three seams: the core files (every file of the manifest), the port's machine check with a fake
plugin folder (one skill missing, then all present), and the installed plugin on this machine.

Issue #30: the plugin's identity is its name, ``mattpocock-skills``; the marketplace after ``@`` is
the machine's choice. The machine check resolves that name from the user's settings and the caches
of the marketplaces that enable it, and the port writes the resolved name into the target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops import port
from tools.agentops.core_check import MANIFEST_PATH, read_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOVED_SKILL = "resolving-merge-conflicts"
SEVEN_REQUIRED = {
    "tdd",
    "diagnosing-bugs",
    "code-review",
    "writing-for-agents",
    "domain-modeling",
    "codebase-design",
    "research",
}
OFFICIAL = "mattpocock-skills@claude-plugins-official"
OWN = "mattpocock-skills@mattpocock"


class _Gh:
    returncode = 0
    stdout = "gh version 2.101.0"
    stderr = ""


def _gh_ok(args):
    return _Gh()


def _settings(tmp_path: Path, enabled: dict) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")
    return path


def _cache_root(tmp_path: Path, marketplaces: dict[str, set[str]]) -> Path:
    """The cache root: ``<root>/<marketplace>/mattpocock-skills/1.3.1/skills/engineering/<name>/SKILL.md``
    for each marketplace, the skills it ships."""
    root = tmp_path / "cache"
    for marketplace, names in marketplaces.items():
        for name in names:
            folder = root / marketplace / "mattpocock-skills" / "1.3.1" / "skills" / "engineering" / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return root


def _machine_plugin_folder() -> Path:
    """The installed plugin folder of the marketplace this machine enables; skips when none is ready."""
    try:
        return port.resolve_plugin(Path.home() / ".claude" / "settings.json")[1]  # read only
    except port.PortError as exc:
        pytest.skip(f"the mattpocock-skills plugin is not enabled and complete on this machine: {exc}")


def _quiet_port(monkeypatch) -> None:
    """The steps of ``run_port`` that need the home's git state, gh or the monitor's install folder."""
    monkeypatch.setattr(port, "check_home_clean", lambda root=None: None)
    monkeypatch.setattr(port, "check_target_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(port, "sync_labels", lambda repo, gh_runner=None, labels=None: ([], []))
    monkeypatch.setattr(port, "ensure_runtime_excludes", lambda target, git_runner=None, extra_rules=(): [])
    monkeypatch.setattr(port, "write_monitor_stubs", lambda target, repo, home_root=None: [])
    monkeypatch.setattr(port, "install_monitor", lambda *args, **kwargs: port.MonitorInstall(Path("unused")))
    # the port's commit (issue #39) needs a git repo at the target; these tests use a plain folder
    monkeypatch.setattr(port, "commit_written", lambda target, paths, message, owner_dirty=(): port.CommitResult())
    monkeypatch.setattr(port, "uncommitted_paths", lambda target: set())


def test_removed_skill_is_named_in_no_core_file():
    hits = []
    for entry in read_manifest(MANIFEST_PATH):
        if entry.startswith("tests/"):
            continue  # a test names the removed skill to test its removal
        path = REPO_ROOT / entry
        if path.is_file() and REMOVED_SKILL in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(entry)
    assert hits == []


def test_porting_names_exactly_the_seven_required_skills():
    assert port.required_porting_skills() == SEVEN_REQUIRED


# -- issue #1080: the named skills against the plugin folder ------------------------------------

def test_check_fails_naming_the_one_skill_and_the_marketplace_considered(tmp_path):
    named = port.named_pocock_skills()
    assert "research" in named
    cache = _cache_root(tmp_path, {"claude-plugins-official": named - {"research"}})
    with pytest.raises(port.PortError, match="research") as info:
        port.check_machine_ready(_settings(tmp_path, {OFFICIAL: True}), gh_runner=_gh_ok, plugin_cache=cache)
    assert "claude-plugins-official" in str(info.value)
    assert "mattpocock-skills" in str(info.value)


def test_check_passes_when_the_fake_plugin_ships_every_named_skill(tmp_path):
    cache = _cache_root(tmp_path, {"claude-plugins-official": set(port.named_pocock_skills())})
    port.check_machine_ready(_settings(tmp_path, {OFFICIAL: True}), gh_runner=_gh_ok, plugin_cache=cache)


def test_installed_plugin_ships_every_named_skill():
    folder = _machine_plugin_folder()
    assert port.missing_pocock_skills(port.named_pocock_skills(), port.installed_plugin_skills(folder)) == []


def test_port_text_states_every_label_of_core_labels():
    assert len(port.load_core_labels()) == 19
    porting = (REPO_ROOT / "docs" / "agents" / "PORTING.md").read_text(encoding="utf-8")
    assert "The 19 labels named in `core_labels.json`" in porting
    assert "sync_labels``: the 19 labels" in Path(port.__file__).read_text(encoding="utf-8")


# -- issue #30 row 1: the marketplace the machine enables, here the official one -----------------

def test_row1_check_passes_under_the_official_marketplace_and_returns_its_name(tmp_path):
    settings = _settings(tmp_path, {OFFICIAL: True})
    cache = _cache_root(tmp_path, {"claude-plugins-official": set(port.named_pocock_skills())})
    name = port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=cache)
    assert name == OFFICIAL


# -- issue #30 row 2: no marketplace enables the plugin ----------------------------------------

def test_row2_refuses_when_the_settings_file_is_missing(tmp_path):
    settings = tmp_path / "absent.json"
    with pytest.raises(port.PortError) as info:
        port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=tmp_path / "cache")
    assert "mattpocock-skills" in str(info.value)
    assert str(settings) in str(info.value)


@pytest.mark.parametrize("enabled", [{}, {OWN: False}, {OFFICIAL: False}, {"mattpocock-skills@other": False}])
def test_row2_refuses_when_no_marketplace_enables_the_plugin(tmp_path, enabled):
    settings = _settings(tmp_path, enabled)
    cache = _cache_root(tmp_path, {"claude-plugins-official": set(port.named_pocock_skills())})
    with pytest.raises(port.PortError) as info:
        port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=cache)
    assert "mattpocock-skills" in str(info.value)
    assert str(settings) in str(info.value)


# -- issue #30 row 3: two marketplaces enable it ------------------------------------------------

def test_row3_both_marketplaces_enabled_resolves_to_mattpocock(tmp_path):
    settings = _settings(tmp_path, {OFFICIAL: True, OWN: True})
    complete = set(port.named_pocock_skills())
    cache = _cache_root(tmp_path, {"claude-plugins-official": complete, "mattpocock": complete})
    assert port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=cache) == OWN


def test_without_mattpocock_the_first_complete_marketplace_in_settings_order_wins(tmp_path):
    settings = _settings(tmp_path, {"mattpocock-skills@other-market": True, OFFICIAL: True})
    complete = set(port.named_pocock_skills())
    cache = _cache_root(tmp_path, {"other-market": complete, "claude-plugins-official": complete})
    assert port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=cache) == \
        "mattpocock-skills@other-market"


def test_an_incomplete_marketplace_is_not_chosen(tmp_path):
    settings = _settings(tmp_path, {OWN: True, OFFICIAL: True})
    complete = set(port.named_pocock_skills())
    cache = _cache_root(tmp_path, {"mattpocock": complete - {"research"}, "claude-plugins-official": complete})
    assert port.check_machine_ready(settings, gh_runner=_gh_ok, plugin_cache=cache) == OFFICIAL


# -- issue #30 row 4: the port writes the resolved name into the target --------------------------

def test_row4_port_writes_the_resolved_name_into_a_new_target(tmp_path, monkeypatch):
    _quiet_port(monkeypatch)
    settings = _settings(tmp_path, {OFFICIAL: True})
    cache = _cache_root(tmp_path, {"claude-plugins-official": set(port.named_pocock_skills())})
    target = tmp_path / "target"
    target.mkdir()
    port.run_port(target, "owner/target", settings_path=settings, plugin_cache=cache,
                  gh_runner=_gh_ok, home_root=REPO_ROOT)
    data = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == {OFFICIAL: True}


def test_row4_a_target_that_names_another_marketplace_keeps_its_entry(tmp_path, monkeypatch):
    _quiet_port(monkeypatch)
    settings = _settings(tmp_path, {OFFICIAL: True})
    cache = _cache_root(tmp_path, {"claude-plugins-official": set(port.named_pocock_skills())})
    target = tmp_path / "target"
    (target / ".claude").mkdir(parents=True)
    (target / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {OWN: True}}), encoding="utf-8")
    report = port.run_port(target, "owner/target", settings_path=settings, plugin_cache=cache,
                           gh_runner=_gh_ok, home_root=REPO_ROOT)
    data = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == {OWN: True}
    assert report.plugin_enabled is True
