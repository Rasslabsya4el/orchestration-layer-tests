"""The port command (issue #152, ADR 0109): copy the orchestration core into a target repo.

    python -m tools.agentops port --target <path> --repo <owner/name> [--brain-title "<title>"]
        [--update-rule word|auto] [--replace]

Run at the home, in this order; a failed step prints one plain line and exits 1. Nothing is
written before steps 1-4 (``check_*``) all pass:

1. ``check_home_clean``: the home's own ``core check`` is clean.
2. ``check_machine_ready``: the mattpocock-skills plugin is enabled and ships every skill the core
   names (#1080), ``gh`` is 2.101+.
3. ``check_target_ready``: the target is a git repo whose GitHub remote is ``--repo``.
4. ``find_clashes``: a manifest file already at the target with different content stops the
   port, unless ``--replace``. ``TARGET_OWNED_ENTRIES`` (the three files Pocock's own setup
   writes) never clash, ``--replace`` included: they belong to the target (issue #224).
5. ``copy_manifest``: every manifest file, byte for byte, except a ``TARGET_OWNED_ENTRIES`` file
   already present, which is kept exactly as it is.
6. ``write_own_files`` / ``stub_cited_paths``: the target's own identity file, ``CLAUDE.md``,
   ``.claude/settings.json``, the stub ``opencode.json`` (#59, only where absent) and a stub for every path a core file cites that is not itself a
   core file. Then ``install_monitor`` (#45): the target's monitor mod is installed into the machine folder named
   by its plugin id and that folder is added to the owner's ``env.CLAUDE_CODE_PLUGIN_DIRS``.
7. ``sync_labels``: the 19 labels, through ``gh``, created only where missing.
8. one report line.

Stdlib only, like every module the core ships with; the port never imports from ``src/``.
``--target`` may never be the home itself (the port refuses on a path match).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .closing_keywords import ClosingKeywordError, require_clean_built
from .core_check import DEFAULT_ROOT as HOME_ROOT
from .core_check import MANIFEST_PATH, MONITOR_LITERAL, read_manifest, run_check
from .gh_call import run_gh
from .leak_starter import PATTERNS_ENTRY, write_starter
from .util import no_window_flags
from .worktree import SHARED

#: the plugin's identity: its name, without the marketplace after ``@`` (the machine's choice, #30).
PLUGIN_ID = "mattpocock-skills"
MIN_GH_VERSION = (2, 101)

LABELS_PATH = HOME_ROOT / "config" / "core_labels.json"
CLAUDE_MD_TEMPLATE = HOME_ROOT / "config" / "core_templates" / "CLAUDE.md.stub"
#: the target's CODING_STANDARDS.md (issue #32): part 1 is the core file's rule lines, rendered in at port time.
#: The one spelling of its name: `core update` (#44) imports it from here too.
CODING_STANDARDS_ENTRY = "CODING_STANDARDS.md"
CODING_STANDARDS_TEMPLATE =HOME_ROOT / "config" / "core_templates" / "CODING_STANDARDS.md.stub"
CORE_STANDARDS_FILE = HOME_ROOT / "docs" / "agents" / "core-standards.md"
CORE_RULES_PLACEHOLDER = "{core_rules}"
SETTINGS_HOOKS_TEMPLATE = HOME_ROOT / "config" / "core_templates" / "settings.hooks.json"
#: the bridge's settings file (#59): `ocserver.read_opencode_config` reads it at the repo root. The port writes a STUB
#: (a fake baseURL, an env-reference key) only where the target holds none; `core update` never touches it.
OPENCODE_ENTRY = "opencode.json"
OPENCODE_TEMPLATE = HOME_ROOT / "config" / "core_templates" / "opencode.json.stub"

#: The monitor mod's folder in a target (#797): the generic files come with the manifest; these two files of the
#: pane's folder are the target's own (written once from a template, never in the manifest, so `core update` never
#: rewrites them): its add-on (none) and its constants. The payload types (`types/index.d.ts`) are generic and in the manifest.
MONITOR_DIR = "mods/" + MONITOR_LITERAL
MONITOR_STUBS = (
    ("hooks/addon.tsx", "monitor_addon.tsx.stub"),
    ("hooks/project.ts", "monitor_project.ts.stub"),
)

#: the default location of the user's own Claude Code settings (never this repo's).
DEFAULT_USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

#: cited paths never stubbed: they are written by ``write_own_files`` itself, or are not repo
#: paths at all (a bare command name, a placeholder with ``<...>``).
_CITED_PATH_RE = re.compile(r"`([A-Za-z0-9_][\w./-]*)`")

#: a stub only ever holds markdown, so only a ``.md`` file or a directory (stubbed as an empty
#: ``.gitkeep``) is a candidate; a ``.json``/``.txt``/``.py`` citation names a real manifest file
#: or a run-time file, never something the port itself should stub (issue #209).
_CITED_FILE_EXTENSION = ".md"

#: a bare cited name with no folder (e.g. ``GLOSSARY.md``) is only a target's own file when it is
#: one of the target's known root docs; anything else bare (e.g. ``MEMORY.md``, the index of
#: Claude Code's own auto-memory folder, not a repo path at all) is not a repo path (issue #209).
_ROOT_DOC_NAMES = {"GLOSSARY.md"}

#: Pocock's own setup writes these three from the target's own answers (issue #224,
#: docs/research/2026-09-28-port-vs-pocock-setup.md); once either the port or Pocock's setup has
#: seeded one at a target, it is the target's own file. The port seeds a copy only when the file is
#: missing, never reports it as a clash, never overwrites it with ``--replace``, and ``core update``
#: never rewrites or removes it.
TARGET_OWNED_ENTRIES = frozenset({
    "docs/agents/issue-tracker.md",
    "docs/agents/triage-labels.md",
    "docs/agents/domain.md",
})

#: the core-shipped modules a ported target's `local_modules` starts with (the autopilot is core since #314, the
#: skill counter since #75): their files are manifest paths by design, so `core update` never counts them as a
#: target's own (#53).
CORE_LOCAL_MODULES = (".autopilot", ".provisional", ".mods", ".skill_counter")


#: the runtime folders the core writes in a target (issue #289): never tracked, so a target's
#: `git status` never shows them once these are in `.git/info/exclude`.
RUNTIME_EXCLUDES = ("/artifacts/runs/", "/var/agentops/", "/var/locks/")


class PortError(RuntimeError):
    """One step of the port failed; the message is the one plain line to print."""


@dataclass
class PortReport:
    copied: list[str] = field(default_factory=list)
    #: ``TARGET_OWNED_ENTRIES`` files already present at the target, left byte for byte (issue #224).
    kept: list[str] = field(default_factory=list)
    own_files_written: list[str] = field(default_factory=list)
    stubs_written: list[str] = field(default_factory=list)
    labels_created: list[str] = field(default_factory=list)
    labels_existing: list[str] = field(default_factory=list)
    #: rules newly written into the target's `.git/info/exclude` (issue #289); a rule already
    #: covered (already in the file, or already ignored by the target's own `.gitignore`) is not
    #: counted here.
    runtime_excludes_written: list[str] = field(default_factory=list)
    #: the port's commit of the paths it wrote (issue #39): the full sha, "" when nothing was committed.
    commit_sha: str = ""
    committed: list[str] = field(default_factory=list)
    #: written paths the target's gitignore covers: left out of the commit and reported, not a failure.
    ignored: list[str] = field(default_factory=list)
    #: written paths the owner had already edited (uncommitted) before the port wrote: never staged, listed.
    left_uncommitted: list[str] = field(default_factory=list)
    #: the resolved full name the machine enables (``mattpocock-skills@<marketplace>``, #30).
    plugin_name: str = ""
    #: the target's own `enabledPlugins` value for `plugin_name` after the port ran, or ``None``
    #: when the target's settings hold no such key at all (should not happen: the port always
    #: writes or finds one).
    plugin_enabled: bool | None = None
    #: the machine folder the monitor mod was installed into (#45), and whether this run added it to the owner's plugin list.
    monitor_folder: str = ""
    monitor_registered: bool = False
    next_step: str = ""

    def render(self) -> str:
        plugin_note = ""
        if self.plugin_enabled is False:
            plugin_note = f"the target keeps {self.plugin_name!r} disabled (`false`) in its own settings. "
        kept_note = ""
        if self.kept:
            kept_note = f"kept {len(self.kept)} target-owned file(s) as they were: {', '.join(self.kept)}. "
        commit_note = "no commit (nothing new to commit); "
        if self.commit_sha:
            commit_note = (f"committed {len(self.committed)} file(s) as {self.commit_sha[:8]} "
                           "on the target's current branch; ")
        ignored_note = ""
        if self.ignored:
            ignored_note = f"skipped {len(self.ignored)} gitignored path(s): {', '.join(self.ignored)}; "
        if self.left_uncommitted:
            ignored_note += f"left uncommitted, your own edits: {', '.join(self.left_uncommitted)}; "
        monitor_note = ""
        if self.monitor_folder:
            monitor_note = (f"monitor installed at {self.monitor_folder}"
                            f"{' and added to the plugin list (restart Claude Code)' if self.monitor_registered else ''}; ")
        bridge_note = ""
        if OPENCODE_ENTRY in self.own_files_written:
            bridge_note = (f"wrote the stub {OPENCODE_ENTRY} (the bridge's settings): the owner fills its baseURL and "
                           "sets CODEX_LB_API_KEY; ")
        return (
            f"copied {len(self.copied)} file(s); "
            f"{kept_note}{bridge_note}"
            f"wrote {len(self.own_files_written)} own file(s), {len(self.stubs_written)} stub(s); "
            f"labels: {len(self.labels_created)} created, {len(self.labels_existing)} already there; "
            f"{len(self.runtime_excludes_written)} runtime exclude rule(s) written. "
            f"{monitor_note}{commit_note}{ignored_note}"
            f"{plugin_note}"
            f"Next: {self.next_step}"
        )


# -- step 1: the home is clean -----------------------------------------------------------------

def check_home_clean(root: Path = HOME_ROOT) -> None:
    _rows, ok = run_check(root=root)
    if not ok:
        raise PortError("the home's own core check leaks: run `python -m tools.agentops core check` first")


# -- step 2: the machine is ready ----------------------------------------------------------------

def _parse_gh_version(text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\.(\d+)(?:\.\d+)?", text)
    if not match:
        raise PortError(f"could not read gh's version from: {text.strip()!r}")
    return int(match.group(1)), int(match.group(2))


def _split_plugin(key: str) -> tuple[str, str]:
    """``name@marketplace`` -> (name, marketplace); a key with no ``@`` has an empty marketplace."""
    name, _, marketplace = key.partition("@")
    return name, marketplace


def _plugin_entries(settings: dict) -> list[tuple[str, bool]]:
    """The marketplaces that list the plugin in ``enabledPlugins``, in the settings' order, each with
    its value (``True`` when enabled)."""
    enabled = settings.get("enabledPlugins")
    if isinstance(enabled, dict):
        pairs = [(key, bool(value)) for key, value in enabled.items()]
    elif isinstance(enabled, list):
        pairs = [(key, True) for key in enabled if isinstance(key, str)]
    else:
        pairs = []
    entries = []
    for key, on in pairs:
        name, marketplace = _split_plugin(key)
        if name == PLUGIN_ID and marketplace:
            entries.append((marketplace, on))
    return entries


#: the plugins' install caches: `<root>/<marketplace>/mattpocock-skills/<version>/skills/<group>/<name>/SKILL.md`;
#: the newest version folder of a marketplace's plugin is read.
PLUGIN_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache"
#: a Pocock skill the core names as `mattpocock-skills:<name>` (#1080).
_POCOCK_NAME_RE = re.compile(r"mattpocock-skills:([a-z][a-z-]*[a-z])")
#: the port's own list of the plugin's skills the core requires (`docs/agents/PORTING.md`, step 2).
_PORTING_SKILLS_RE = re.compile(r"call\s+\w+\s+of\s+the plugin's skills by name\s*\(([^)]*)\)")
_BACKTICK_NAME_RE = re.compile(r"`([a-z][a-z-]*)`")


def required_porting_skills(root: Path = HOME_ROOT) -> set[str]:
    """The plugin skills `docs/agents/PORTING.md` step 2 names as required, read from that sentence."""
    text = (root / "docs" / "agents" / "PORTING.md").read_text(encoding="utf-8")
    match = _PORTING_SKILLS_RE.search(text)
    if not match:
        raise PortError("docs/agents/PORTING.md no longer names the plugin's skills the core requires")
    return set(_BACKTICK_NAME_RE.findall(match.group(1)))


def named_pocock_skills(root: Path = HOME_ROOT) -> set[str]:
    """Every Pocock skill the core names: `mattpocock-skills:<name>` in any manifest file, plus the
    list `docs/agents/PORTING.md` requires."""
    names: set[str] = set()
    for entry in read_manifest(MANIFEST_PATH):
        path = root / entry
        if path.is_file():
            names |= set(_POCOCK_NAME_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    return names | required_porting_skills(root)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in path.name.split("."))


def installed_plugin_skills(cache: Path) -> set[str]:
    """The skill names the installed plugin ships: every folder with a SKILL.md under the newest
    version folder's `skills/` (the plugin groups them, e.g. `skills/engineering/tdd/SKILL.md`)."""
    if not cache.is_dir():
        return set()
    versions = [p for p in cache.iterdir() if p.is_dir()]
    if not versions:
        return set()
    skills_dir = max(versions, key=_version_key) / "skills"
    names: set[str] = set()
    for skill_file in skills_dir.rglob("SKILL.md"):
        if "deprecated" in skill_file.relative_to(skills_dir).parts:
            continue
        names.add(skill_file.parent.name)
    return names


def missing_pocock_skills(named: set[str], installed: set[str]) -> list[str]:
    return sorted(set(named) - set(installed))


def resolve_plugin(settings_path: Path,
                   plugin_cache: Path = PLUGIN_CACHE_ROOT) -> tuple[str, Path]:
    """The plugin's full name the machine enables, ``mattpocock-skills@<marketplace>``, and its installed folder.

    The marketplaces considered are the ones the user's settings enable (``true``). Of those, a marketplace is
    complete when its cache ``<plugin_cache>/<marketplace>/mattpocock-skills`` ships every skill the core names.
    ``mattpocock`` wins among the complete ones, else the first in the settings' order."""
    if not settings_path.is_file():
        raise PortError(
            f"no marketplace enables {PLUGIN_ID!r}: {settings_path} does not exist "
            f"(run `/plugin marketplace add mattpocock/skills` then enable it)"
        )
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PortError(f"invalid JSON in {settings_path}: {exc}") from exc
    entries = _plugin_entries(settings)
    enabled = [marketplace for marketplace, on in entries if on]
    if not enabled:
        listed = ", ".join(f"{m} ({'true' if on else 'false'})" for m, on in entries) or "none"
        raise PortError(
            f"no marketplace enables {PLUGIN_ID!r} in {settings_path} (marketplaces considered: {listed}); "
            f"enable it with `/plugin`"
        )
    named = named_pocock_skills()
    considered: list[str] = []
    complete: list[str] = []
    for marketplace in enabled:
        folder = plugin_cache / marketplace / PLUGIN_ID
        if not folder.is_dir():
            considered.append(f"{marketplace} (no cache at {folder})")
            continue
        missing = missing_pocock_skills(named, installed_plugin_skills(folder))
        if missing:
            considered.append(f"{marketplace} (missing {', '.join(missing)})")
        else:
            complete.append(marketplace)
    if not complete:
        raise PortError(
            f"{PLUGIN_ID!r} ships no skill the core names under any marketplace the machine enables in "
            f"{settings_path} (marketplaces considered: {'; '.join(considered)}): update the plugin; "
            f"a target whose plugin lacks them has rules it cannot follow"
        )
    marketplace = "mattpocock" if "mattpocock" in complete else complete[0]
    return f"{PLUGIN_ID}@{marketplace}", plugin_cache / marketplace / PLUGIN_ID


def check_machine_ready(settings_path: Path,
                         gh_runner=None, plugin_cache: Path = PLUGIN_CACHE_ROOT) -> str:
    """Refuses unless the machine is ready; returns the plugin's resolved full name (``resolve_plugin``)."""
    name, _folder = resolve_plugin(settings_path, plugin_cache)

    runner = gh_runner or run_gh
    result = runner(["--version"])
    if result.returncode != 0:
        raise PortError("gh --version failed: is gh installed?")
    version = _parse_gh_version(result.stdout)
    if version < MIN_GH_VERSION:
        raise PortError(
            f"gh {version[0]}.{version[1]} is older than {MIN_GH_VERSION[0]}.{MIN_GH_VERSION[1]} "
            f"(native blocking and sub-issues need 2.101+): upgrade gh"
        )
    return name


# -- step 3: the target is ready -----------------------------------------------------------------

def check_target_ready(target: Path, repo: str, git_runner=None, home_root: Path = HOME_ROOT) -> None:
    try:
        if target.resolve() == home_root.resolve():
            raise PortError("--target is the home: a port never targets the repo it runs in")
    except OSError:
        pass
    if not (target / ".git").exists():
        raise PortError(f"{target} is not a git repo (no .git)")

    runner = git_runner or (lambda args: subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=no_window_flags(),
    ))
    result = runner(["remote", "-v"])
    if result.returncode != 0:
        raise PortError(f"git remote -v failed in {target}: {(result.stderr or '').strip()[:200]}")
    if repo.lower() not in result.stdout.lower():
        raise PortError(f"{target}'s git remote does not name {repo!r}: {result.stdout.strip()[:200]}")


# -- step 3b: AGENTS.md with no CLAUDE.md ---------------------------------------------------------

def check_no_agents_md_only(target: Path) -> None:
    """The core is imported only through ``CLAUDE.md`` (``write_own_files``); a target that holds
    ``AGENTS.md`` and no ``CLAUDE.md`` gets no guess at which file to edit (issue #224, matching
    Pocock's own setup's either-or choice, ``SKILL.md:76-80``)."""

    if (target / "AGENTS.md").is_file() and not (target / "CLAUDE.md").is_file():
        raise PortError(
            f"{target} holds AGENTS.md but no CLAUDE.md: the core is imported through CLAUDE.md "
            f"only -- create CLAUDE.md first (the port never guesses between the two)"
        )


# -- step 4: no clash --------------------------------------------------------------------------

def find_clashes(target: Path, entries: list[str], root: Path = HOME_ROOT) -> list[str]:
    clashes = []
    for entry in entries:
        if entry in TARGET_OWNED_ENTRIES:
            continue
        dest = target / entry
        if not dest.is_file():
            continue
        src = root / entry
        if src.read_bytes() != dest.read_bytes():
            clashes.append(entry)
    return clashes


# -- step 5: copy -------------------------------------------------------------------------------

def copy_manifest(target: Path, entries: list[str], root: Path = HOME_ROOT) -> tuple[list[str], list[str]]:
    """Returns ``(copied, kept)``. A ``TARGET_OWNED_ENTRIES`` file already present at the target is
    never rewritten (kept byte for byte, whatever it holds); every other entry is copied
    unconditionally, as before."""

    copied = []
    kept = []
    for entry in entries:
        dest = target / entry
        if entry in TARGET_OWNED_ENTRIES and dest.is_file():
            kept.append(entry)
            continue
        src = root / entry
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        copied.append(entry)
    return copied, kept


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- runtime excludes (issue #289) ---------------------------------------------------------------

def ensure_runtime_excludes(target: Path, git_runner=None, extra_rules=()) -> list[str]:
    """Appends the `RUNTIME_EXCLUDES` rules (plus ``extra_rules``, #633: cited paths under a
    SHARED dir, which the port never stubs) missing from the target's `.git/info/exclude` (the
    common git dir, so every worktree of the target's checkout is covered); never touches the
    target's tracked `.gitignore`. Append only: existing lines are kept, a rule already present
    (exact line match) or already covered by the target's own ignore rules
    (`git check-ignore`) is skipped. Returns the rules newly written; an empty list means nothing
    was missing, and the file is not touched at all."""

    runner = git_runner or (lambda args: subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=no_window_flags(),
    ))
    result = runner(["rev-parse", "--git-common-dir"])
    if result.returncode != 0:
        raise PortError(
            f"git rev-parse --git-common-dir failed in {target}: {(result.stderr or '').strip()[:200]}"
        )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (target / common_dir).resolve()
    exclude_path = common_dir / "info" / "exclude"

    existing_text = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    existing_lines = set(existing_text.splitlines())

    to_write = []
    for rule in (*RUNTIME_EXCLUDES, *(r for r in extra_rules if r not in RUNTIME_EXCLUDES)):
        if rule in existing_lines:
            continue
        probe = rule.strip("/") + "/__runtime_excludes_probe__"
        check = runner(["check-ignore", "-q", "--no-index", probe])
        if check.returncode == 0:
            continue
        to_write.append(rule)

    if not to_write:
        return []

    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"
    exclude_path.write_text(existing_text + "\n".join(to_write) + "\n", encoding="utf-8")
    return to_write


# -- step 6: the target's own files --------------------------------------------------------------

def _repo_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _effective_brain_title(repo: str, brain_title: str | None) -> str:
    """The brain chat's own title, the same value ``build_orchestration_settings`` writes into
    ``config/orchestration.json``'s ``brain_chat_title``: ``brain_title`` when given, else
    ``Brain: <repo name>``."""
    return brain_title or f"Brain: {_repo_name(repo)}"


def _default_plugin_id(repo: str) -> str:
    """A target's default monitor plugin id (#11): its repo name, lowercased, each run of non-alphanumeric
    characters one "-", plus "-monitor": ``X/My Repo.v2`` -> ``my-repo-v2-monitor``."""
    slug = re.sub(r"[^a-z0-9]+", "-", _repo_name(repo).lower()).strip("-")
    return f"{slug}-monitor"


def _install_folder(target: Path | None, repo: str | None = None) -> Path:
    """The monitor's install folder (`install_root() / <plugin id>`). No target = this repo: its plugin id as
    `install_folder.plugin_id` names it (`monitor.plugin_id`, else the source folder's name). A target: its own
    `monitor.plugin_id` (`config/orchestration.json` there) when set, else a default derived from its own repo
    name (`_default_plugin_id`); the source folder's name is never a target's default. Reads the settings by
    path, no environment variable is touched."""
    from .install_folder import install_root, plugin_id, source_name
    if target is None:
        return install_root() / plugin_id(None, default=source_name())
    return install_root() / plugin_id(target, default=_default_plugin_id(repo or target.resolve().name))


def next_step_message(repo: str, brain_title: str | None) -> str:
    """Step 8's own report line (issue #221, row 3): what is left for the human, the title of the brain chat to
    open (`docs/agents/PORTING.md` "The first brain chat of a target"). The monitor needs no step of the owner's:
    the port installs it and registers it in the owner's plugin list itself (#45, `install_monitor`)."""
    title = _effective_brain_title(repo, brain_title)
    return (
        "open the target in Claude Code, then follow docs/agents/PORTING.md "
        '"The first brain chat of a target" and give it a first issue labelled '
        f'ready-for-agent: the brain chat to open is "{title}"'
    )


def _claude_projects_dir(target: Path) -> str:
    """Mirrors Claude Code's own mangling of an absolute path into a project directory name."""
    resolved = str(target.resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved)


def build_orchestration_settings(target: Path, repo: str, brain_title: str | None,
                                  update_rule: str, home_repo: str, core_version: str) -> dict:
    return {
        "repo": repo,
        "brain_chat_title": brain_title or f"Brain: {_repo_name(repo)}",
        "context_line": 150000,
        "claude_projects_dir": _claude_projects_dir(target),
        # required by tools/agentops/core_settings.py's REQUIRED_KEYS; a target that adds
        # local modules of its own fills these in for real.
        "bridge_dir": str(target / "var" / "agentops"),
        "bridge_lock": "var/locks/bridge.lock",
        # the autopilot is core since #314: a target's dispatcher loads it by default.
        "local_modules": list(CORE_LOCAL_MODULES),
        "home": home_repo,
        "core_version": core_version,
        "update_rule": update_rule,
        # an explicitly empty areas setting: the one-area rules stay off until the target names its areas.
        "areas": [],
        "code_issue_area": "",
        # #11: the monitor's plugin id, the one ``_install_folder`` and ``write_monitor_stubs`` derive for this
        # target, so the installed folder, PANE and this file agree without a hand edit (COMMAND is the core's `monitor`, #173).
        "monitor": {"plugin_id": _default_plugin_id(repo)},
    }


def _refuse_other_repo_claim(target: Path, repo: str, plugin_id: str) -> None:
    """#11: refuse, changing nothing, when the install folder of `plugin_id` belongs to another REPO (the command file
    is never a claim, #173). The check is `install_folder.refuse_other_repo_claim`, shared with `mods install`; this
    keeps its message."""
    from .install_folder import InstallClaimError, install_root, refuse_other_repo_claim
    here = target.resolve().as_posix()
    try:
        refuse_other_repo_claim(install_root() / plugin_id, here,
                                hint=" (set monitor.plugin_id of this target to another name)")
    except InstallClaimError as exc:
        raise PortError(str(exc)) from exc


@dataclass
class MonitorInstall:
    folder: Path
    copied: int = 0
    removed: int = 0
    #: the folder was added to the owner's `env.CLAUDE_CODE_PLUGIN_DIRS` by this run (False: it was listed already).
    registered: bool = False
    command_line: str | None = None


def _plan_monitor_settings(target: Path, repo: str, settings_path: Path) -> str:
    """#52: the owner's settings text with the target's monitor folder in `env.CLAUDE_CODE_PLUGIN_DIRS`, planned
    before the port's first write: a settings error (not an object, `env` not an object, the value not a string,
    a file that does not parse) raises PortError here and nothing is written."""
    from . import install_folder as inst
    folder = _install_folder(target, repo)
    try:
        return inst.plan_plugin_dir(folder, settings_path)
    except (ValueError, OSError) as exc:
        raise PortError(f"cannot add {folder} to env.CLAUDE_CODE_PLUGIN_DIRS of {settings_path}: {exc}") from exc


def install_monitor(target: Path, repo: str, settings_path: Path, planned_settings: str,
                    commands_dir: Path | None = None) -> MonitorInstall:
    """#45: install the target's monitor mod (its `mods/<monitor>` folder, with the files `write_monitor_stubs` wrote)
    into `_install_folder(target, repo)` under the target's plugin id, with the code `mods install` uses
    (`install_folder.copy_files`: the manifest `name` and the quoted source name in the hook files become the plugin
    id), write its slash-command file, and write `planned_settings` (`_plan_monitor_settings`: the owner's
    `settings_path` with the folder in `env.CLAUDE_CODE_PLUGIN_DIRS`, every other byte kept). A folder another REPO
    holds is refused, nothing written; the shared command file `monitor.md` is never a refusal (#173). Idempotent: a
    second run copies 0 files and writes 0 bytes of the settings."""
    from . import install_folder as inst
    folder = _install_folder(target, repo)
    commands = commands_dir or inst.commands_dir()
    _refuse_other_repo_claim(target, repo, folder.name)
    source = target / MONITOR_DIR
    if not (source / "hooks" / "project.ts").is_file():
        raise PortError(f"no monitor mod to install at {source} (hooks/project.ts is missing)")
    inst.sync_shared(MONITOR_LITERAL, target)
    _total, diff = inst.diff_files(source, folder, MONITOR_LITERAL, folder.name)
    copied, removed, _backup = inst.copy_files(source, folder, MONITOR_LITERAL, folder.name, diff)
    inst.journal_append(folder.name, "install")  # #210: one line per monitor install, written here, not by run_install
    command_line = inst.command_step(source, commands)
    try:
        registered = inst.write_settings(settings_path, planned_settings)
    except OSError as exc:
        raise PortError(f"cannot write {settings_path} (env.CLAUDE_CODE_PLUGIN_DIRS, adding {folder}): {exc}") from exc
    return MonitorInstall(folder, copied, removed, registered, command_line)


def fill_stub(text: str, values: dict[str, str]) -> str:
    """Fill a monitor stub's placeholders (``{mod_dir}``, ``{pane}``...) with the target's values (#172: the one filling
    ``write_monitor_stubs`` and ``project_exports.lines_to_append`` both use)."""
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def monitor_stub_values(target: Path, repo: str) -> dict[str, str]:
    """The target's value for each placeholder of the monitor stubs (#172: the one dict ``write_monitor_stubs`` and
    ``core update``'s append of a missing export both fill the stubs with). PANE is the target's plugin id (#11); the
    command is the core's one `monitor` (#173), not filled here."""
    folder = _install_folder(target, repo)
    return {
        "{mod_dir}": folder.as_posix(),
        "{repo_path}": target.resolve().as_posix(),
        "{repo_slug}": repo,
        "{title}": _repo_name(repo),
        "{pane}": folder.name,
    }


def write_monitor_stubs(target: Path, repo: str, home_root: Path = HOME_ROOT) -> list[str]:
    """The monitor's own files at the target (#797), each only when absent: no add-on, the target's constants
    (its path, its tracker, its title, the folder `mods install` fills). PANE is the target's plugin id (#11), so two
    targets never share a pane; the command is the core's one `monitor` (#173). A refusal (#11) runs before any file is written."""
    plugin_id = _install_folder(target, repo).name
    if not (target / MONITOR_DIR / "hooks" / "project.ts").exists():
        _refuse_other_repo_claim(target, repo, plugin_id)
    values = monitor_stub_values(target, repo)
    written = []
    for rel, template in MONITOR_STUBS:
        dest = target / MONITOR_DIR / rel
        if dest.exists():
            continue
        text = fill_stub((home_root / "config" / "core_templates" / template).read_text(encoding="utf-8"), values)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written.append(f"{MONITOR_DIR}/{rel}")
    return written


def _agent_skills_block_from_stub() -> str:
    """The stub's own ``## Agent skills`` block (issue #224 gap), for an existing target
    ``CLAUDE.md`` that carries no such heading yet."""
    stub = CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8")
    heading = "## Agent skills"
    start = stub.index(heading)
    rest = stub[start + len(heading):]
    m = re.search(r"\n## (?!#)", rest)
    end = start + len(heading) + (m.start() + 1 if m else len(rest))
    return stub[start:end].rstrip("\n") + "\n"


def render_coding_standards(template: str, core_standards: str) -> str:
    """The target's CODING_STANDARDS.md text (#32, #44): part 1 is the ``- `` rule lines of the core
    standards file, joined, in place of ``{core_rules}`` of the stub ``template``."""
    rules = "\n".join(line for line in core_standards.splitlines() if line.startswith("- "))
    return template.replace(CORE_RULES_PLACEHOLDER, rules)


def write_own_files(target: Path, repo: str, brain_title: str | None, update_rule: str,
                     home_repo: str, core_version: str, plugin_name: str) -> list[str]:
    written = []

    settings_path = target / "config" / "orchestration.json"
    if not settings_path.is_file():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        data = build_orchestration_settings(target, repo, brain_title, update_rule, home_repo, core_version)
        settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append("config/orchestration.json")

    claude_md = target / "CLAUDE.md"
    import_line = "@docs/agents/core-block.md"
    agent_skills_heading = "## Agent skills"
    if not claude_md.is_file():
        stub = CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8").format(repo_name=_repo_name(repo))
        claude_md.write_text(stub, encoding="utf-8")
        written.append("CLAUDE.md")
    else:
        text = claude_md.read_text(encoding="utf-8")
        original = text
        if agent_skills_heading not in text:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n{_agent_skills_block_from_stub()}\n"
        if import_line not in text:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\n## The orchestration core\n\n{import_line}\n"
        if text != original:
            claude_md.write_text(text, encoding="utf-8")
            written.append("CLAUDE.md")

    standards = target / CODING_STANDARDS_ENTRY
    if not standards.is_file():
        standards.write_text(render_coding_standards(CODING_STANDARDS_TEMPLATE.read_text(encoding="utf-8"),
                                                     CORE_STANDARDS_FILE.read_text(encoding="utf-8")),
                             encoding="utf-8")
        written.append(CODING_STANDARDS_ENTRY)

    if write_starter(target, repo) is not None:  # #93: the target's leak words, L3 only, never a file already there
        written.append(PATTERNS_ENTRY)

    bridge = target / OPENCODE_ENTRY
    if not bridge.exists():
        bridge.write_bytes(OPENCODE_TEMPLATE.read_bytes())
        written.append(OPENCODE_ENTRY)

    settings_json = target / ".claude" / "settings.json"
    hooks_template = _with_plugin_name(json.loads(SETTINGS_HOOKS_TEMPLATE.read_text(encoding="utf-8")), plugin_name)
    if not settings_json.is_file():
        settings_json.parent.mkdir(parents=True, exist_ok=True)
        settings_json.write_text(json.dumps(hooks_template, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        written.append(".claude/settings.json")
    else:
        existing = json.loads(settings_json.read_text(encoding="utf-8"))
        hooks_changed = _merge_hooks(existing, hooks_template)
        plugins_changed = _merge_enabled_plugins(existing, hooks_template)
        deny_changed = _merge_permissions_deny(existing, hooks_template)
        if hooks_changed or plugins_changed or deny_changed:
            settings_json.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
            written.append(".claude/settings.json")

    return written


def _merge_hooks(existing: dict, template: dict) -> bool:
    """Add the template's hook registrations to ``existing``, only where absent. Returns whether
    anything changed. No other key of ``existing`` is touched."""

    changed = False
    existing_hooks = existing.setdefault("hooks", {})
    for event, entries in template.get("hooks", {}).items():
        existing_entries = existing_hooks.setdefault(event, [])
        for entry in entries:
            if entry not in existing_entries:
                existing_entries.append(entry)
                changed = True
    return changed


def _merge_permissions_deny(existing: dict, template: dict) -> bool:
    """Append the template's ``permissions.deny`` rules that ``existing`` lacks, at the end, never
    reordering or removing. Returns whether anything changed. No other key is touched."""

    wanted = template.get("permissions", {}).get("deny", [])
    missing = [rule for rule in wanted if rule not in existing.get("permissions", {}).get("deny", [])]
    if not missing:
        return False
    deny = existing.setdefault("permissions", {}).setdefault("deny", [])
    deny.extend(missing)
    return True


def _with_plugin_name(template: dict, plugin_name: str) -> dict:
    """The template with its own plugin entry renamed to ``plugin_name``: the template file keeps its
    placeholder key, and the port writes the name the machine resolved in its place (#30)."""
    renamed = {}
    for key, enabled in template.get("enabledPlugins", {}).items():
        renamed[plugin_name if _split_plugin(key)[0] == PLUGIN_ID else key] = enabled
    return {**template, "enabledPlugins": renamed}


def _merge_enabled_plugins(existing: dict, template: dict) -> bool:
    """Add the template's ``enabledPlugins`` entries to ``existing``, only where the target names
    no such plugin under any marketplace. An entry the target already lists (``true`` or ``false``,
    any marketplace) is kept exactly as it stands. Returns whether anything changed. No other key
    of ``existing`` is touched."""

    changed = False
    existing_plugins = existing.setdefault("enabledPlugins", {})
    named = {_split_plugin(key)[0] for key in existing_plugins}
    for name, enabled in template.get("enabledPlugins", {}).items():
        if _split_plugin(name)[0] not in named:
            existing_plugins[name] = enabled
            named.add(_split_plugin(name)[0])
            changed = True
    return changed


def _read_plugin_enabled(settings_json: Path, plugin_name: str) -> bool | None:
    if not settings_json.is_file():
        return None
    try:
        data = json.loads(settings_json.read_text(encoding="utf-8"))
    except ValueError:
        return None
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return None
    if plugin_name in enabled:
        return enabled[plugin_name]
    # an entry the target already names under another marketplace is the one it keeps (#30)
    for key, value in enabled.items():
        if _split_plugin(key)[0] == PLUGIN_ID:
            return value
    return None


def _cited_paths(text: str, manifest_set: set[str], manifest_basenames: set[str]) -> set[str]:
    cited = set()
    for match in _CITED_PATH_RE.finditer(text):
        candidate = match.group(1)
        if "<" in candidate or ">" in candidate:
            continue
        is_dir = candidate.endswith("/")
        is_file = candidate.endswith(_CITED_FILE_EXTENSION)
        if not (is_dir or is_file):
            continue
        if candidate.startswith(("http:", "https:", "~")):
            continue
        if candidate in manifest_set:
            continue
        is_bare = "/" not in candidate.rstrip("/")
        if is_bare:
            # a bare name equal to a manifest file's basename is that manifest file cited
            # without its folder (e.g. `orch.md`, `core_manifest.txt`): already copied, never
            # stubbed. A folder-qualified path (`docs/agents/local/brain.md`) is a different
            # file even where it shares a basename with a manifest entry (`docs/agents/brain.md`),
            # so this check only fires for a bare candidate.
            if candidate.rstrip("/") in manifest_basenames:
                continue
            # a bare file name is a repo path only when it is one of the target's known root
            # docs; anything else bare is not a repo path at all (`MEMORY.md`, the index of
            # Claude Code's own auto-memory folder).
            if is_file and candidate not in _ROOT_DOC_NAMES:
                continue
        cited.add(candidate)
    return cited


def _is_shared(rel: str) -> bool:
    """A path whose first component is a SHARED dir (`var`, `vendor`, `artifacts`): a tracked file
    there makes every `worktree add` in the target fail (#633), so the port never stubs it."""
    return rel.split("/", 1)[0] in SHARED


def shared_cited_excludes(target: Path, entries: list[str], read_root: Path | None = None) -> list[str]:
    """The `.git/info/exclude` rules (`/<rel>`) for every cited path under a SHARED dir that is
    not already in ``target``; they stay untracked runtime state instead of getting a stub."""

    read_root = read_root or target
    manifest_set = set(entries)
    manifest_basenames = {entry.rsplit("/", 1)[-1] for entry in entries}
    cited: set[str] = set()
    for entry in entries:
        path = read_root / entry
        if not entry.endswith(".md") or not path.is_file():
            continue
        cited |= _cited_paths(path.read_text(encoding="utf-8", errors="replace"),
                              manifest_set, manifest_basenames)
    return ["/" + rel for rel in sorted(cited) if _is_shared(rel)]


def _stub_plan(target: Path, entries: list[str], read_root: Path) -> list[str]:
    """Paths a core doc cites that are not manifest files and not already in ``target``, reading
    each doc from ``read_root`` (the home, in a dry run). A cited directory lists its `.gitkeep`."""

    manifest_set = set(entries)
    manifest_basenames = {entry.rsplit("/", 1)[-1] for entry in entries}
    cited: set[str] = set()
    for entry in entries:
        path = read_root / entry
        if not entry.endswith(".md") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        cited |= _cited_paths(text, manifest_set, manifest_basenames)
    out = []
    for rel in sorted(cited):
        if _is_shared(rel):
            continue
        dest = target / rel
        if rel.endswith("/"):
            if not dest.is_dir():
                out.append(rel + ".gitkeep")
        elif not dest.exists():
            out.append(rel)
    return out


def stub_cited_paths(target: Path, entries: list[str]) -> list[str]:
    """A stub for every path a copied core file cites that is not a manifest file and not
    already there (the target's own files, written by ``write_own_files``, or an earlier stub).
    Directories (a citation ending in ``/``) get an empty ``.gitkeep``; files get a one-line
    placeholder naming this as the target's own file, never overwritten."""

    manifest_set = set(entries)
    manifest_basenames = {entry.rsplit("/", 1)[-1] for entry in entries}
    cited: set[str] = set()
    for entry in entries:
        # only docs cite paths the way a target needs stubbed; a code file's own docstring
        # naming its own data file (e.g. core_check.py's "never next to a checked target" note)
        # is an implementation detail, not something the target is short of.
        if not entry.endswith(".md"):
            continue
        path = target / entry
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cited |= _cited_paths(text, manifest_set, manifest_basenames)

    written = []
    for rel in sorted(cited):
        if _is_shared(rel):
            continue
        dest = target / rel
        if rel.endswith("/"):
            if dest.is_dir():
                continue
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".gitkeep").write_text("", encoding="utf-8")
            written.append(rel)
        else:
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"# {rel}\n\nThis repo's own file; the port only ensures it exists.\n",
                             encoding="utf-8")
            written.append(rel)
    return written


# -- step 7: labels ------------------------------------------------------------------------------

def load_core_labels(path: Path = LABELS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sync_labels(repo: str, gh_runner=None, labels: dict | None = None) -> tuple[list[str], list[str]]:
    """Returns (created, already_there). One `gh label list` read, then one `gh label create`
    per label missing there."""

    labels = labels if labels is not None else load_core_labels()
    runner = gh_runner or run_gh
    result = runner(["label", "list", "-R", repo, "--json", "name", "--limit", "200"])
    if result.returncode != 0:
        raise PortError(f"gh label list failed: {(result.stderr or '').strip()[:200]}")
    existing = {row["name"] for row in json.loads(result.stdout)}

    created, already_there = [], []
    for name, meta in labels.items():
        if name in existing:
            already_there.append(name)
            continue
        create_result = runner([
            "label", "create", name, "-R", repo,
            "--color", meta["color"], "--description", meta["description"], "--force",
        ])
        if create_result.returncode != 0:
            raise PortError(f"gh label create {name!r} failed: {(create_result.stderr or '').strip()[:200]}")
        created.append(name)
    return created, already_there


# -- the whole port -------------------------------------------------------------------------------

def run_port(target: Path, repo: str, *, brain_title: str | None = None,
             update_rule: str = "word", replace: bool = False,
             home_root: Path = HOME_ROOT, settings_path: Path,
             plugin_cache: Path = PLUGIN_CACHE_ROOT, gh_runner=None, git_runner=None,
             commands_dir: Path | None = None) -> PortReport:
    target = Path(target)

    check_home_clean(home_root)
    plugin_name = check_machine_ready(settings_path, gh_runner=gh_runner, plugin_cache=plugin_cache)
    check_target_ready(target, repo, git_runner=git_runner, home_root=home_root)
    check_no_agents_md_only(target)
    # #45: a monitor folder another REPO holds stops the port before the first write (the shared monitor.md never does, #173)
    _refuse_other_repo_claim(target, repo, _install_folder(target, repo).name)
    # #52: the owner's settings file is read and its edit planned here, before the first write
    planned_settings = _plan_monitor_settings(target, repo, settings_path)

    entries = read_manifest(MANIFEST_PATH)
    clashes = find_clashes(target, entries, root=home_root)
    if clashes and not replace:
        raise PortError(
            "the target already holds these manifest files with other content (use --replace): "
            + ", ".join(clashes)
        )

    report = PortReport(plugin_name=plugin_name)
    owner_dirty = uncommitted_paths(target)  # before the first write: what the owner had edited (#39)
    report.copied, report.kept = copy_manifest(target, entries, root=home_root)

    home_commit = _home_commit(home_root, git_runner=git_runner)
    home_repo = _home_repo(home_root)
    report.own_files_written = write_own_files(
        target, repo, brain_title, update_rule, home_repo=home_repo, core_version=home_commit,
        plugin_name=plugin_name,
    )
    report.stubs_written = stub_cited_paths(target, entries)
    report.own_files_written += write_monitor_stubs(target, repo, home_root)
    installed = install_monitor(target, repo, settings_path, planned_settings, commands_dir=commands_dir)
    report.monitor_folder, report.monitor_registered = str(installed.folder), installed.registered
    report.plugin_enabled = _read_plugin_enabled(target / ".claude" / "settings.json", plugin_name)

    report.labels_created, report.labels_existing = sync_labels(repo, gh_runner=gh_runner)
    report.runtime_excludes_written = ensure_runtime_excludes(
        target, git_runner=git_runner, extra_rules=shared_cited_excludes(target, entries))
    report.next_step = next_step_message(repo, brain_title)
    # last: the runtime excludes above are part of what git ignores in the target (issue #39)
    written = [*report.copied, *report.own_files_written, *report.stubs_written]
    result = commit_written(target, written,
                            f"orchestration core ported from {home_repo} at {home_commit}",
                            owner_dirty=owner_dirty)
    report.commit_sha, report.committed, report.ignored = result.sha, result.committed, result.ignored
    report.left_uncommitted = result.left
    return report


def plan_port(target: Path, repo: str, *, replace: bool = False,
              home_root: Path = HOME_ROOT, settings_path: Path,
              plugin_cache: Path = PLUGIN_CACHE_ROOT, gh_runner=None, git_runner=None) -> tuple[list[str], list[str]]:
    """The read-only half of `run_port` (--dry-run): the same validation, then (files, labels) the
    port would write / create. Touches nothing in the target; the only gh call is `label list`.
    The validation needs the target to be a git repo whose remote names ``repo``, so an empty
    scratch dir passes only once it holds a `.git` and that remote."""

    target = Path(target)
    check_home_clean(home_root)
    check_machine_ready(settings_path, gh_runner=gh_runner, plugin_cache=plugin_cache)
    check_target_ready(target, repo, git_runner=git_runner, home_root=home_root)
    check_no_agents_md_only(target)
    _plan_monitor_settings(target, repo, settings_path)  # #52: a settings error stops the dry run as the port
    entries = read_manifest(MANIFEST_PATH)
    clashes = find_clashes(target, entries, root=home_root)
    if clashes and not replace:
        raise PortError(
            "the target already holds these manifest files with other content (use --replace): "
            + ", ".join(clashes)
        )
    files = list(entries)
    for rel in ("config/orchestration.json", "CLAUDE.md", CODING_STANDARDS_ENTRY, PATTERNS_ENTRY, OPENCODE_ENTRY,
                ".claude/settings.json"):
        if not (target / rel).is_file():
            files.append(rel)
    files += [rel for rel in _stub_plan(target, entries, home_root) if rel not in files]

    labels = load_core_labels()
    runner = gh_runner or run_gh
    existing: set[str] = set()
    try:
        result = runner(["label", "list", "-R", repo, "--json", "name", "--limit", "200"])
        if result.returncode == 0:
            existing = {row["name"] for row in json.loads(result.stdout)}
    except Exception:
        existing = set()  # the repo cannot be read: every label is "would create"
    return files, [name for name in labels if name not in existing]


def monitor_dry_run_line(target: Path, repo: str) -> str:
    """#45: what the port's monitor step would do, for the dry runs of `port` and `target add`."""
    return f"would install the monitor at {_install_folder(target, repo)} and register it"


def run_dry_run(target: Path, repo: str, **kwargs) -> str:
    files, labels = plan_port(target, repo, **kwargs)
    lines = [f"dry run: {len(files)} files and {len(labels)} labels would be written into {target}; "
             "nothing was written",
             monitor_dry_run_line(target, repo)]
    lines += [f"file {rel}" for rel in files]
    lines += [f"label {name}" for name in labels]
    return chr(10).join(lines)


def _home_repo(home_root: Path) -> str:
    from .core_settings import load as load_settings
    try:
        return load_settings()["repo"]
    except Exception:
        return "unknown"


def _home_commit(home_root: Path, git_runner=None) -> str:
    runner = git_runner or (lambda args: subprocess.run(
        ["git", "-C", str(home_root), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=no_window_flags(),
    ))
    result = runner(["rev-parse", "HEAD"])
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


# -- step 9: the port's commit (issue #39) --------------------------------------------------------

#: bounded git calls (seconds, a machine file's unit): a commit runs the target's hooks, so it gets longer.
GIT_CALL_TIMEOUT = 60
GIT_COMMIT_TIMEOUT = 300


@dataclass
class CommitResult:
    sha: str = ""
    committed: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


def uncommitted_paths(target: Path) -> set[str]:
    """Every file the target holds with an uncommitted change before the port writes: modified or staged
    against HEAD, or untracked (each file, not its folder). Read once, before the first write (#39)."""

    result = _git_call(target, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], GIT_CALL_TIMEOUT)
    if result.returncode != 0:
        raise PortError(f"git status failed in {target}: {_first_line(result)}")
    fields = result.stdout.split("\0")
    paths: set[str] = set()
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":  # a rename or copy: its old name is the next field
            i += 1
    return paths


def _owner_edited(path: str, owner_dirty: set[str]) -> bool:
    if path in owner_dirty:
        return True
    return path.endswith("/") and any(dirty.startswith(path) for dirty in owner_dirty)


def _git_call(target: Path, args: list[str], timeout: int, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(target), *args], capture_output=True, text=True, input=stdin,
            encoding="utf-8", errors="replace", creationflags=no_window_flags(), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise PortError(f"git {args[0]} in {target} did not finish within {timeout // 60} min")


def _first_line(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return (text.splitlines() or [""])[0][:200]


def commit_written(target: Path, paths: list[str], message: str,
                   owner_dirty: set[str] = frozenset()) -> CommitResult:
    """The port's commit: a written path the owner had already edited (``owner_dirty``, read before the
    write) is never staged and is listed in ``left``; the rest go through ``_commit_paths``."""

    paths = list(dict.fromkeys(paths))
    left = [p for p in paths if _owner_edited(p, owner_dirty)]
    result = _commit_paths(target, [p for p in paths if p not in left], message)
    result.left = left
    return result


def _commit_paths(target: Path, paths: list[str], message: str) -> CommitResult:
    """Stages exactly ``paths`` and commits only them on the target's current branch. Never ``git add -A``
    or ``.``; ``git commit -- <paths>`` takes those paths alone, so a file the owner staged himself stays
    staged and out of the commit. A path the target's gitignore covers is left out and reported in
    ``ignored``; a path with no change against HEAD is left out too (a rerun makes no commit). A failed
    git call raises ``PortError`` with one line naming it; the files stay written."""

    if not paths:
        return CommitResult()
    checked = _git_call(target, ["check-ignore", "-z", "--stdin"], GIT_CALL_TIMEOUT, "\0".join(paths) + "\0")
    if checked.returncode not in (0, 1):  # 1 = none of the paths is ignored
        raise PortError(f"git check-ignore failed in {target}: {_first_line(checked)}")
    ignored = [p for p in checked.stdout.split("\0") if p] if checked.returncode == 0 else []
    skip = set(ignored)
    to_add = [p for p in paths if p not in skip]
    if not to_add:
        return CommitResult(ignored=ignored)

    added = _git_call(target, ["add", "--", *to_add], GIT_CALL_TIMEOUT)
    if added.returncode != 0:
        raise PortError(f"git add failed in {target}: {_first_line(added)}")
    staged = _git_call(target, ["diff", "--cached", "--name-only", "-z", "--", *to_add], GIT_CALL_TIMEOUT)
    if staged.returncode != 0:
        raise PortError(f"git diff failed in {target}: {_first_line(staged)}")
    committed = [p for p in staged.stdout.split("\0") if p]
    if not committed:
        return CommitResult(ignored=ignored)

    try:
        require_clean_built(message, where="the port's commit")
    except ClosingKeywordError as exc:
        raise PortError(str(exc)) from exc
    done = _git_call(target, ["commit", "-m", message, "--", *committed], GIT_COMMIT_TIMEOUT)
    if done.returncode != 0:
        raise PortError(f"git commit failed in {target}: {_first_line(done)}")
    head = _git_call(target, ["rev-parse", "HEAD"], GIT_CALL_TIMEOUT)
    if head.returncode != 0:
        raise PortError(f"git rev-parse failed in {target}: {_first_line(head)}")
    return CommitResult(sha=head.stdout.strip(), committed=committed, ignored=ignored)


# -- CLI ------------------------------------------------------------------------------------------

def add_subparser(sub) -> None:
    port = sub.add_parser("port", help="copy the orchestration core into a target repo (issue #152)")
    port.add_argument("--target", required=True, type=Path)
    port.add_argument("--repo", required=True)
    port.add_argument("--brain-title")
    port.add_argument("--update-rule", choices=["word", "auto"], default="word")
    port.add_argument("--replace", action="store_true")
    port.add_argument("--dry-run", action="store_true",
                      help="validate, print every file and label the port would write, write nothing")


def run_port_command(args, *, settings_path: Path) -> int:
    """The `port` CLI; `settings_path` is the owner's settings file, given by the CLI (cli.py)."""
    try:
        if getattr(args, "dry_run", False):
            print(run_dry_run(args.target, args.repo, replace=args.replace, settings_path=settings_path))
            return 0
        report = run_port(
            args.target, args.repo, brain_title=args.brain_title,
            update_rule=args.update_rule, replace=args.replace, settings_path=settings_path,
        )
    except PortError as exc:
        print(str(exc))
        return 1
    print(report.render())
    return 0
