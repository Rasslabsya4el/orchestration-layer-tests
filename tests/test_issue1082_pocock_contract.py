"""Issue #1082 (unit 1 of 2): the core's contract with the Pocock plugin and the check of it.

Fake plugin folders under tmp_path (an older one, a newer one), a fake repo with its own manifest,
contract and files, the manifest grep on main, the installed 1.3.1 plugin when it is present, and the
``core pocock-check`` verb's exit codes. Nothing here starts the heavy tool.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.agentops import pocock_contract as pc
from tools.agentops.core_check import DEFAULT_ROOT, main
from tools.agentops.port import PortError, resolve_plugin


def _machine_plugin_folder():
    """The installed plugin folder of the marketplace this machine enables (#30), or None when none is ready."""
    try:
        return resolve_plugin(Path.home() / ".claude" / "settings.json")[1]  # read only
    except PortError:
        return None

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "tools" / "agentops" / "pocock_contract.json"
SETUP_DIR = "skills/engineering/setup-matt-pocock-skills"
TRACKER_TEMPLATE = f"{SETUP_DIR}/issue-tracker-github.md"
TRIAGE_TEMPLATE = f"{SETUP_DIR}/triage-labels.md"
DOMAIN_TEMPLATE = f"{SETUP_DIR}/domain.md"
TRACKER_FILE = "docs/agents/issue-tracker.md"
PORTING_SEVEN = {
    "tdd", "diagnosing-bugs", "code-review", "writing-for-agents",
    "domain-modeling", "codebase-design", "research",
}
PREFIXED_RE = re.compile(r"mattpocock-skills:([a-z][a-z-]*[a-z])")


def _plugin(cache: Path, version: str, skills, templates: dict[str, str] | None = None) -> Path:
    """A plugin version folder: skills in their group (as the real plugin), templates at their paths."""
    root = cache / version
    for name in skills:
        folder = root / "skills" / "engineering" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    for rel, text in (templates or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _repo(tmp_path: Path, files: dict[str, str], skills, templates: dict[str, str] | None = None,
          version: str = "1.3.1") -> Path:
    """A fake repo: its manifest lists `files`, its contract lists `skills` and `templates`."""
    root = tmp_path / "repo"
    (root / "tools" / "agentops").mkdir(parents=True)
    (root / "tools" / "agentops" / "core_manifest.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    contract = {"verified_plugin_version": version, "skills": sorted(skills), "templates": templates or {}}
    (root / "tools" / "agentops" / "pocock_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    return root


def _contract_of(repo: Path) -> Path:
    return repo / "tools" / "agentops" / "pocock_contract.json"


# -- the fake older plugin: a listed skill is missing -------------------------------------------

def test_fake_older_plugin_missing_a_listed_skill_fails_naming_it(tmp_path):
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd", "diagnosing-bugs"])
    repo = _repo(tmp_path, {"docs/agents/brain.md": "use `tdd`, then mattpocock-skills:research\n"},
                 ["tdd", "diagnosing-bugs", "research"])
    findings = pc.check_contract(plugin, _contract_of(repo), repo)
    assert findings == ["missing in plugin: research"]


def test_fake_plugin_shipping_every_skill_and_template_is_clean(tmp_path):
    tracker = "# Issue tracker: GitHub\n\n## Conventions\n\nbody\n"
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd", "research"],
                     {TRACKER_TEMPLATE: "# Issue tracker: GitHub\n\n## Conventions\n"})
    repo = _repo(tmp_path, {"docs/agents/brain.md": "use `tdd` and mattpocock-skills:research\n",
                            TRACKER_FILE: tracker},
                 ["tdd", "research"], {TRACKER_FILE: TRACKER_TEMPLATE})
    assert pc.check_contract(plugin, _contract_of(repo), repo) == []


# -- the fake newer plugin: one skill removed, one renamed -------------------------------------

def test_newer_fake_plugin_reports_both_the_removed_and_the_renamed_skill(tmp_path):
    cache = tmp_path / "cache"
    _plugin(cache, "1.3.1", ["tdd", "research", "diagnosing-bugs"])
    _plugin(cache, "1.4.0", ["tdd", "diagnose-bugs"])  # research removed, diagnosing-bugs renamed
    repo = _repo(tmp_path, {"docs/agents/brain.md": "use `tdd`\n"},
                 ["tdd", "research", "diagnosing-bugs"])
    newest = pc.newest_plugin_root(cache)
    assert newest == cache / "1.4.0"
    findings = pc.check_contract(newest, _contract_of(repo), repo)
    assert findings == ["missing in plugin: diagnosing-bugs", "missing in plugin: research"]


def test_newest_version_is_compared_as_numbers(tmp_path):
    cache = tmp_path / "cache"
    _plugin(cache, "1.9.0", ["tdd"])
    _plugin(cache, "1.10.0", ["tdd"])
    assert pc.newest_plugin_root(cache) == cache / "1.10.0"
    assert pc.newest_plugin_root(tmp_path / "nothing-here") is None


def test_deprecated_group_does_not_count_as_shipped(tmp_path):
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd"])
    old = plugin / "skills" / "deprecated" / "resolving-merge-conflicts"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert pc.plugin_skill_names(plugin) == {"tdd"}


# -- names found in manifest files must be in the contract ------------------------------------

def test_prefixed_name_absent_from_contract_is_named_with_its_file(tmp_path):
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd", "writing-for-agents"])
    repo = _repo(tmp_path, {"docs/agents/brain.md": "call mattpocock-skills:writing-for-agents first\n"},
                 ["tdd"])
    findings = pc.check_contract(plugin, _contract_of(repo), repo)
    assert findings == ["not in contract: writing-for-agents (docs/agents/brain.md)"]


def test_backticked_bare_plugin_name_absent_from_contract_is_named(tmp_path):
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd", "triage"])
    repo = _repo(tmp_path, {"docs/agents/brain.md": "run the `triage` skill, not `main`\n"}, ["tdd"])
    findings = pc.check_contract(plugin, _contract_of(repo), repo)
    assert findings == ["not in contract: triage (docs/agents/brain.md)"]


# -- the three target-owned templates: heading differences are reported ------------------------

def test_template_headings_extra_in_file_and_missing_from_file_are_both_reported(tmp_path):
    plugin = _plugin(tmp_path / "cache", "1.3.1", ["tdd"],
                     {TRACKER_TEMPLATE: "# Issue tracker: GitHub\n\n## Conventions\n\n## Wayfinding operations\n"})
    repo = _repo(tmp_path,
                 {TRACKER_FILE: "# Issue tracker: GitHub\n\n## Conventions\n\n## How a ticket is built\n"},
                 ["tdd"], {TRACKER_FILE: TRACKER_TEMPLATE})
    findings = pc.check_contract(plugin, _contract_of(repo), repo)
    assert findings == [
        f"extra heading in {TRACKER_FILE}: ## How a ticket is built",
        f"missing heading in {TRACKER_FILE}: ## Wayfinding operations",
    ]


# -- on main: the manifest grep, and the installed plugin -------------------------------------

def _names_in_manifest_files(repo_root: Path) -> dict[str, set[str]]:
    """Independent of pocock_contract's scan: the prefixed names and the backticked bare names
    that are shipped by the installed plugin or listed in the contract (the vocabulary)."""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    vocabulary = set(contract["skills"])
    folder = _machine_plugin_folder()
    if folder is not None and folder.is_dir():
        vocabulary |= pc.plugin_skill_names(pc.newest_plugin_root(folder))
    found: dict[str, set[str]] = {}
    for entry in pc.manifest_entries(repo_root):
        if entry.startswith("tests/"):
            continue  # a test names skills as data, not as a claim about the core
        path = repo_root / entry
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in PREFIXED_RE.findall(text):
            found.setdefault(name, set()).add(entry)
        for name in re.findall(r"`([a-z][a-z-]*[a-z])`", text):
            if name in vocabulary:
                found.setdefault(name, set()).add(entry)
    return found


@pytest.mark.live
def test_every_name_the_manifest_files_name_is_in_the_contract():
    """Checks every skill name the manifest files name on this checkout is in the pocock contract (`core pocock-check` covers it for the owner)."""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    found = _names_in_manifest_files(REPO_ROOT)
    absent = {name: sorted(files) for name, files in found.items() if name not in contract["skills"]}
    assert absent == {}
    assert PORTING_SEVEN <= set(contract["skills"])


def test_contract_templates_name_the_three_target_owned_files():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["templates"] == {
        "docs/agents/issue-tracker.md": TRACKER_TEMPLATE,
        "docs/agents/triage-labels.md": TRIAGE_TEMPLATE,
        "docs/agents/domain.md": DOMAIN_TEMPLATE,
    }
    assert set(contract["templates"]) == {"docs/agents/issue-tracker.md", "docs/agents/triage-labels.md",
                                          "docs/agents/domain.md"}


@pytest.mark.live
def test_the_real_contract_names_only_skills_the_installed_plugin_ships():
    """Checks the real contract names only skills the mattpocock-skills plugin installed on this machine ships (`core pocock-check` covers it for the owner)."""
    folder = _machine_plugin_folder()
    if folder is None or not folder.is_dir():
        pytest.skip("the mattpocock-skills plugin is not enabled and complete on this machine")
    findings = pc.check_contract(pc.newest_plugin_root(folder), CONTRACT_PATH, REPO_ROOT)
    assert [f for f in findings if f.startswith("missing in plugin:")] == []


# -- the CLI verb: `python -m tools.agentops core pocock-check` ---------------------------------

def test_cli_exits_0_when_the_fake_plugin_is_clean(tmp_path, capsys):
    cache = tmp_path / "cache"
    _plugin(cache, "1.3.1", ["tdd"])
    repo = _repo(tmp_path, {"docs/agents/brain.md": "use `tdd`\n"}, ["tdd"])
    code = main(["core", "pocock-check", "--plugin-cache", str(cache), "--root", str(repo)])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_cli_exits_1_naming_the_missing_skill(tmp_path, capsys):
    cache = tmp_path / "cache"
    _plugin(cache, "1.3.1", ["tdd"])
    repo = _repo(tmp_path, {"docs/agents/brain.md": "use `tdd`\n"}, ["tdd", "research"])
    code = main(["core", "pocock-check", "--plugin-cache", str(cache), "--root", str(repo)])
    assert code == 1
    assert "missing in plugin: research" in capsys.readouterr().out


def test_cli_exits_1_when_no_plugin_version_is_installed(tmp_path, capsys):
    repo = _repo(tmp_path, {"docs/agents/brain.md": "plain text\n"}, ["tdd"])
    code = main(["core", "pocock-check", "--plugin-cache", str(tmp_path / "empty"), "--root", str(repo)])
    assert code == 1
    assert "no plugin version" in capsys.readouterr().out


def test_cli_default_root_is_this_repo():
    assert DEFAULT_ROOT == REPO_ROOT
