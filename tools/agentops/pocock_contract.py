"""The core's contract with the mattpocock-skills plugin (issue #1082).

The core takes two things from the plugin: the skills its files name (``mattpocock-skills:<name>`` or a
backticked bare ``<name>``), and the setup templates the three target-owned tracker files follow. The
contract is one JSON file, ``tools/agentops/pocock_contract.json``: the skills named, the templates
followed by which repo file, and the plugin version last verified against.

    python -m tools.agentops core pocock-check [--plugin-cache DIR] [--root PATH]

The check reads the newest version folder under the plugin cache and prints one finding per line:

- ``missing in plugin: <name>``: a skill the contract names that the plugin does not ship (the
  ``deprecated`` group does not count as shipped);
- ``not in contract: <name> (<file>)``: a skill name a manifest file names that the contract lacks;
- ``extra heading in <file>: <heading>`` / ``missing heading in <file>: <heading>``: the headings of a
  repo file that differ from its plugin template.

Exit 0 when there are no findings, 1 otherwise. Stdlib only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve()
CONTRACT_PATH = _HERE.with_suffix(".json")
CONTRACT_REL = "tools/agentops/pocock_contract.json"
MANIFEST_REL = "tools/agentops/core_manifest.txt"
DEPRECATED_GROUP = "deprecated"

#: a skill the core names with its plugin prefix (issue #1080).
_PREFIXED_RE = re.compile(r"mattpocock-skills:([a-z][a-z-]*[a-z])")
#: a skill the core names bare, in backticks; only a name the vocabulary knows counts.
_BACKTICK_RE = re.compile(r"`([a-z][a-z-]*[a-z])`")


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in path.name.split("."))


def newest_plugin_root(cache: Path) -> Path | None:
    """The newest version folder under the plugin cache (compared as numbers), or None if there is none."""
    if not cache.is_dir():
        return None
    versions = [p for p in cache.iterdir() if p.is_dir()]
    return max(versions, key=_version_key) if versions else None


def plugin_skill_names(plugin_root: Path) -> set[str]:
    """The skill names a plugin version ships: every folder with a SKILL.md under its ``skills/``, except
    the ``deprecated`` group."""
    skills_dir = plugin_root / "skills"
    names: set[str] = set()
    if not skills_dir.is_dir():
        return names
    for skill_file in skills_dir.rglob("SKILL.md"):
        if DEPRECATED_GROUP in skill_file.relative_to(skills_dir).parts:
            continue
        names.add(skill_file.parent.name)
    return names


def manifest_entries(repo_root: Path) -> list[str]:
    """The repo-relative paths of the core manifest of ``repo_root`` (none if it has no manifest)."""
    from .core_check import read_manifest

    path = repo_root / MANIFEST_REL
    return read_manifest(path) if path.is_file() else []


def manifest_skill_names(repo_root: Path, vocabulary: set[str]) -> dict[str, list[str]]:
    """Each skill name the manifest files of ``repo_root`` name -> the sorted files naming it. A prefixed
    name always counts; a bare backticked name counts only when the vocabulary holds it."""
    found: dict[str, set[str]] = {}
    for entry in manifest_entries(repo_root):
        path = repo_root / entry
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        names = set(_PREFIXED_RE.findall(text))
        names |= {name for name in _BACKTICK_RE.findall(text) if name in vocabulary}
        for name in names:
            found.setdefault(name, set()).add(entry)
    return {name: sorted(files) for name, files in found.items()}


def template_headings(text: str) -> list[str]:
    """The distinct lines that start with ``#``, in file order, as written."""
    headings: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.rstrip()
            if heading not in headings:
                headings.append(heading)
    return headings


def check_contract(plugin_root: Path, contract_path: Path, repo_root: Path) -> list[str]:
    """The findings of the contract against one plugin version folder; an empty list is clean."""
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    named = set(contract["skills"])
    shipped = plugin_skill_names(plugin_root)

    findings = [f"missing in plugin: {name}" for name in sorted(named - shipped)]

    vocabulary = shipped | named
    for name, files in sorted(manifest_skill_names(repo_root, vocabulary).items()):
        if name in named:
            continue
        findings.extend(f"not in contract: {name} ({rel})" for rel in files)

    for rel, template_rel in contract.get("templates", {}).items():
        template_path = plugin_root / template_rel
        repo_path = repo_root / rel
        if not template_path.is_file():
            findings.append(f"missing template in plugin: {template_rel}")
            continue
        if not repo_path.is_file():
            findings.append(f"missing file: {rel}")
            continue
        plugin_headings = template_headings(template_path.read_text(encoding="utf-8", errors="replace"))
        repo_headings = template_headings(repo_path.read_text(encoding="utf-8", errors="replace"))
        findings.extend(f"extra heading in {rel}: {h}" for h in repo_headings if h not in plugin_headings)
        findings.extend(f"missing heading in {rel}: {h}" for h in plugin_headings if h not in repo_headings)
    return findings


def run_pocock_check_command(args, settings_path=None) -> int:
    """`core pocock-check`: print the findings of the newest plugin version; 0 clean, 1 findings."""
    from .core_check import DEFAULT_ROOT
    from .port import PortError, resolve_plugin

    root = args.root if args.root is not None else DEFAULT_ROOT
    if args.plugin_cache is not None:
        cache = args.plugin_cache
    elif settings_path is None:
        print("no plugin cache: pass --plugin-cache (the owner's settings file is not known here)")
        return 1
    else:
        try:
            cache = resolve_plugin(settings_path)[1]
        except PortError as exc:
            print(str(exc))
            return 1
    plugin_root = newest_plugin_root(cache)
    if plugin_root is None:
        print(f"no plugin version under {cache}")
        return 1
    findings = check_contract(plugin_root, root / CONTRACT_REL, root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0
