"""Issue 153: `core status` / `core update`, run in a target that received the core through
`port.py`. Both read the home's GitHub repo through `gh` (never a folder on this PC) behind one
small class, `HomeReader`, so tests stub it with a fixture and no network runs.

    python -m tools.agentops core status
    python -m tools.agentops core update

The target's own `update_rule` (`config/orchestration.json`, ADR 0109) decides what each does:
`home` changes nothing (this repo, or any repo pointing at itself), `word` and `auto` both read
and write the same way -- the difference is only *when* `update` is called (on the owner's word,
or once per dispatcher start, `dispatch.py`).

Stdlib only, like every module the core ships with.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core_check import read_manifest
from .core_tests import imported_test_modules, split_core_tests
from . import monitor_install, project_exports
from .merge import MergeLockBusy, MergeLockTimeout, merge_then_checkout_locked
from .port import CODING_STANDARDS_ENTRY, CORE_LOCAL_MODULES, MONITOR_DIR, TARGET_OWNED_ENTRIES, monitor_stub_values
from .port import render_coding_standards
from .port import _merge_enabled_plugins, _merge_hooks, _merge_permissions_deny, load_core_labels, sync_labels
from . import gh_call
from .gh_call import run_gh
from .leak_starter import PATTERNS_ENTRY, write_starter
from .util import no_window_flags, now

HOME_BRANCH = "main"  # the one branch a home keeps its work on (#1140); home_seed.py reads it too

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "orchestration.json"
MANIFEST_ENTRY = "tools/agentops/core_manifest.txt"
ORCH_STATE_ENTRY = "artifacts/runs/orch_session.json"
HOOKS_TEMPLATE_ENTRY = "config/core_templates/settings.hooks.json"
SETTINGS_JSON_ENTRY = ".claude/settings.json"
MONITOR_PROJECT_ENTRY = f"{MONITOR_DIR}/hooks/project.ts"  # #172: the target's own constants; only appended to
ORCHESTRATION_JSON_ENTRY = "config/orchestration.json"
STANDARDS_STUB_ENTRY = "config/core_templates/CODING_STANDARDS.md.stub"
CORE_STANDARDS_ENTRY = "docs/agents/core-standards.md"
HOME_RULE = "home"
COMPARE_PAGE = 1000  # `compare` pages by `per_page`; 1000 is accepted, 1207 commits are two calls
GRAPHQL_QUERY_CHARS = 20000  # one query stays under the Windows command-line limit (32k)
#: #34: how long one tick of the dispatcher, or the home merge's push (#49), waits for the target's merge lock
#: before the core update is refused ``merge running``; well under the dispatcher's `TICK_SECONDS`.
CORE_UPDATE_WAIT_S = 10.0
SNAPSHOT_ENTRY = "var/agentops/core_update_snapshot.json"  # what a killed update leaves (#49)
DISPATCH_OUT_ENTRY = "artifacts/runs/dispatch.out"
STEP_REFUSED_RC = 3  # `core update-steps` exit code: a step refused (the reason is its last stdout line)
CORE_LABELS_ENTRY = "config/core_labels.json"  # the target's own copy of the core's label list (manifest, #122)


class CoreUpdateError(RuntimeError):
    """One line to print; `core update` exits 1 on this, never writes a file."""


class CoreUpdateBusy(CoreUpdateError):
    """The target's merge lock stayed taken for the whole bounded wait (#49): refused `merge running`, 0 files
    written. A busy target is not a failed one."""


class CoreUpdateRefused(CoreUpdateError):
    """A refusal the target's own state or a live ORCH asks for (#49): logged as `refused: <reason>`
    in the target's dispatch.out, where any other CoreUpdateError is logged as `failed: <reason>`."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class CoreUpdateRateLimited(CoreUpdateError):
    """#148: a gh read of the home failed on the GitHub rate limit. `until` is the epoch second the limit resets
    (None when the read showed budget left): the pending mark carries it, so no retry runs before it."""

    def __init__(self, message: str, until: float | None = None) -> None:
        super().__init__(message)
        self.until = until


def _gh_runner(args: list[str]):
    return run_gh(args)


def _reset_or_minute(read) -> float:
    """The rate limit's reset for a failure that said `rate limit`: a read that fails or shows no reset (a secondary
    limit, budget left) is a minute's wait, so the mark always carries an `until`."""
    try:
        reset = read()
    except Exception:  # noqa: BLE001 -- a failed read of the reset is a minute's wait
        reset = None
    return reset if reset is not None else time.time() + 60.0


def _last_page(response: str) -> int | None:
    """#157: the ``page`` number of the ``rel="last"`` link in a ``gh api --include`` response's Link header; 1 when
    the response has headers and no Link header (a single page); None when it has no headers to read."""

    head = response.replace("\r\n", "\n").split("\n\n", 1)[0]
    lines = head.split("\n")
    if not lines or not lines[0].startswith("HTTP/"):
        return None
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() != "link":
            continue
        for part in value.split(","):
            url, _, rel = part.partition(";")
            if rel.strip().replace(" ", "") == 'rel="last"':
                found = re.search(r"[?&]page=(\d+)", url)
                return int(found.group(1)) if found else None
        return None
    return 1


class HomeReader:
    """Reads the home's manifest, files and commit history at ``main``, through ``gh api``. Every
    method takes one ``gh`` call; ``gh_runner`` is injected so tests never touch the network."""

    def __init__(self, home_repo: str, gh_runner=None, rate_limit_reset=None, count_of=None) -> None:
        self.home_repo = home_repo
        self.count_of = count_of  # #157: sha -> commits up to it, counted at a home checkout; None = not had
        self.runner = gh_runner or _gh_runner
        self.rate_limit_reset = rate_limit_reset or gh_call.rate_limit_reset  # #148: injected by tests

    def _api(self, path: str, jq: str | None = None, include: bool = False) -> str:
        args = ["api", f"repos/{self.home_repo}/{path}"]
        if include:
            args.append("--include")
        if jq is not None:
            args += ["--jq", jq]
        result = self.runner(args)
        if result.returncode != 0:
            text = (result.stderr or "").strip()[:200]
            message = f"gh api {path!r} failed: {text}"
            if "rate limit" in text.lower():
                raise CoreUpdateRateLimited(message, _reset_or_minute(self.rate_limit_reset))
            raise CoreUpdateError(message)
        return result.stdout

    def head_sha(self) -> str:
        return self._api(f"commits/{HOME_BRANCH}", jq=".sha").strip()

    def commit_count(self, sha: str) -> int | None:
        """#157: the commits on the home's main up to and including ``sha`` (what ``git rev-list --count`` gives at
        the home). From the injected local counter, which costs no GitHub call; with none, ONE ``commits?sha=``
        call at one commit a page, whose ``rel="last"`` page number is the count (1 with no Link header). Best
        effort: any failure, a rate limit included, is None and never raises or retries."""

        try:
            if self.count_of is not None:
                count = self.count_of(sha)
            else:
                count = _last_page(self._api(f"commits?sha={sha}&per_page=1", include=True))
        except Exception:  # noqa: BLE001 -- a count that cannot be had never fails the update
            return None
        return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else None

    def commits_between(self, base: str, head: str) -> list[str]:
        """SHAs strictly after ``base`` up to and including ``head`` (chronological)."""

        out = self._api(f"compare/{base}...{head}", jq=".commits[].sha")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def commits_between_with_messages(self, base: str, head: str) -> list[dict]:
        """Sha, commit message, parent shas and committer date, strictly after ``base`` up to and
        including ``head`` (chronological). ``compare`` returns at most ``COMPARE_PAGE`` commits a
        page and a bare call stops at 250 (#881), so it is paged until a short page; 1207 commits
        are two calls."""

        jq = (".commits[] | {sha: .sha, message: .commit.message, "
              "parents: [.parents[].sha], date: .commit.committer.date}")
        rows: list[dict] = []
        page = 1
        while True:
            out = self._api(f"compare/{base}...{head}?per_page={COMPARE_PAGE}&page={page}", jq=jq)
            got = [json.loads(line) for line in (raw.strip() for raw in out.splitlines()) if line]
            rows.extend(got)
            if len(got) < COMPARE_PAGE:
                return rows
            page += 1

    def paths_history(self, at_sha: str, paths: list[str], min_date: str) -> dict[str, set[str]]:
        """For each path, the commits (newest first from ``at_sha``, back to ``min_date``) whose own
        change touches it: ``history(path:)`` of the GraphQL API, every path an alias of one query
        and every further page one more query for the paths still running -- a handful of calls
        for the whole manifest, where ``commits/<sha>`` was one call per commit (#881)."""

        owner, name = self.home_repo.split("/", 1)
        found: dict[str, set[str]] = {path: set() for path in paths}
        cursors: dict[str, str | None] = {path: None for path in paths}
        while cursors:
            batch = list(cursors)
            reads: dict[str, dict] = {}
            start = 0
            while start < len(batch):
                fields: list[str] = []
                size = 0
                aliases: dict[str, str] = {}
                while start < len(batch) and size < GRAPHQL_QUERY_CHARS:
                    path = batch[start]
                    alias = f"h{start}"
                    after = f', after: {json.dumps(cursors[path])}' if cursors[path] else ""
                    field = (f'{alias}: object(oid: "{at_sha}") {{ ... on Commit {{ history(first: 100, '
                             f'path: {json.dumps(path)}{after}) {{ nodes {{ oid committedDate }} '
                             f'pageInfo {{ hasNextPage endCursor }} }} }} }}')
                    fields.append(field)
                    aliases[alias] = path
                    size += len(field)
                    start += 1
                query = (f'query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                         f'{{ {" ".join(fields)} }} }}')
                result = self.runner(["api", "graphql", "-f", f"query={query}"])
                if result.returncode != 0:
                    raise CoreUpdateError(f"gh api graphql failed: {(result.stderr or '').strip()[:200]}")
                repo = (json.loads(result.stdout).get("data") or {}).get("repository") or {}
                for alias, path in aliases.items():
                    reads[path] = ((repo.get(alias) or {}).get("history")) or {"nodes": [], "pageInfo": {}}
            cursors = {}
            for path, history in reads.items():
                nodes = history.get("nodes") or []
                found[path].update(node["oid"] for node in nodes)
                info = history.get("pageInfo") or {}
                if (info.get("hasNextPage") and nodes
                        and min(node["committedDate"] for node in nodes) >= min_date):
                    cursors[path] = info.get("endCursor")
        return found

    def core_issues(self, label: str = "core-issue") -> list[dict]:
        """Every issue of this label at the home, open or closed -- one call, shared by every
        range a caller is naming issues for."""

        args = ["issue", "list", "--repo", self.home_repo, "--label", label, "--state", "all",
                "--limit", "500", "--json", "number,title,body,state"]
        result = self.runner(args)
        if result.returncode != 0:
            raise CoreUpdateError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
        return json.loads(result.stdout or "[]")

    def file_bytes(self, path: str, sha: str) -> bytes:
        encoded = self._api(f"contents/{path}?ref={sha}", jq=".content")
        return base64.b64decode(encoded.replace("\n", ""))

    def manifest(self, sha: str) -> list[str]:
        return _manifest_lines(self.file_bytes(MANIFEST_ENTRY, sha))

    def tree(self, sha: str) -> dict[str, str] | None:
        """Every blob of the home at ``sha`` as {path: blob sha}, in ONE call (#148). ``None`` when GitHub says the
        listing was cut (``truncated``, past ~100k entries): a partial tree is never used, the caller reads per file."""

        body = json.loads(self._api(f"git/trees/{sha}?recursive=1"))
        if body.get("truncated"):
            return None
        return {item["path"]: item["sha"] for item in body.get("tree", []) if item.get("type") == "blob"}

    def test_names(self, sha: str) -> set[str]:
        """File names directly under the home's ``tests/`` at ``sha``."""
        listing = self._api(f"git/trees/{sha}:tests", jq=".tree[].path")
        return {line.strip() for line in listing.splitlines() if line.strip()}


def _manifest_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8")
    return [line for line in (raw.strip() for raw in text.splitlines()) if line and not line.startswith("#")]


NUL = chr(0)


def _blob_sha(data: bytes) -> str:
    """The sha git gives a blob of ``data``: what the home's tree lists, computed here without a subprocess."""

    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _status_paths(root: Path) -> set[str] | None:
    """Every path git reports with a change in the target (staged, unstaged, untracked), one call; ``None`` when the
    status cannot be read."""

    status = _git(root, "status", "--porcelain", "-z")
    if status.returncode != 0:
        return None
    changed: set[str] = set()
    fields = iter(part for part in status.stdout.split(NUL) if part)
    for record in fields:
        changed.add(record[3:])
        if record[0] in "RC":  # a rename or copy names its source in the next field
            changed.add(next(fields, ""))
    return changed


def _clean_index_blobs(root: Path) -> dict[str, str]:
    """{tracked path: blob sha git holds} for every path with no change in its status, by two git calls for the
    whole target; empty when the target is not a committable repo (#148). A file whose bytes differ from the home's
    only by a checkout filter (CRLF under autocrlf) still shows the home's blob here."""

    if not _is_committable_repo(root):
        return {}
    listing = _git(root, "ls-files", "-s", "-z")
    changed = _status_paths(root)
    if listing.returncode != 0 or changed is None:
        return {}
    held: dict[str, str] = {}
    for record in listing.stdout.split(NUL):
        meta, _, path = record.partition("	")
        parts = meta.split()
        if path and len(parts) == 3 and parts[2] == "0" and path not in changed:
            held[path] = parts[1]
    return held


def _core_paths_dirty(root: Path) -> bool:
    """True when any path of the target's own manifest has a change git sees, or git cannot say (#148): a target at
    the home's head is skipped only while every core file is as the last update left it."""

    changed = _status_paths(root)
    if changed is None:
        return True
    manifest = root / MANIFEST_ENTRY
    entries = set(read_manifest(manifest)) if manifest.is_file() else set()
    return bool(changed & (entries - set(TARGET_OWNED_ENTRIES)))


class _HomeFiles:
    """The home's bytes at ``head``, each read at most once (#148). With the home's ``tree`` a file the target
    already holds (the same blob, or the same blob in git's index with a clean status) is answered from the
    target's own copy and costs no call; any other is one ``contents`` call. Without a tree (truncated) every file
    is one call, as before."""

    def __init__(self, root: Path, reader: HomeReader, head: str, tree: dict[str, str] | None) -> None:
        self.root, self.reader, self.head, self.tree = root, reader, head, tree
        self.cache: dict[str, bytes] = {}
        self._held: dict[str, str] | None = None

    def get(self, entry: str) -> bytes:
        if entry not in self.cache:
            own = self._own_copy(entry)
            self.cache[entry] = own if own is not None else self.reader.file_bytes(entry, self.head)
        return self.cache[entry]

    def _own_copy(self, entry: str) -> bytes | None:
        if self.tree is None or entry not in self.tree or not (self.root / entry).is_file():
            return None
        data = (self.root / entry).read_bytes()
        if _blob_sha(data) == self.tree[entry]:
            return data
        if self._held is None:
            self._held = _clean_index_blobs(self.root)
        return data if self._held.get(entry) == self.tree[entry] else None


# -- settings --------------------------------------------------------------------------------

def load_settings(root: Path) -> dict:
    path = root / "config" / "orchestration.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_core_version(root: Path, head: str, count: int | None = None) -> bool:
    """Writes the new version, and the version it replaced (``core_version_before``) so a later
    ``core status`` can name what the update just taken closed, without any git or gh lookup. ``count`` (#157) is the
    number of commits on the home's main up to ``head``, written beside it as ``core_version_count``; a head with no
    count leaves no key (a count held for the older head would name the wrong version)."""

    path = root / "config" / "orchestration.json"
    data = load_settings(root)
    previous = data.get("core_version")
    if previous == head:
        return False
    if previous is not None:
        data["core_version_before"] = previous
    data["core_version"] = head
    if count is not None:
        data["core_version_count"] = count
    else:
        data.pop("core_version_count", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# -- the live-ORCH guard (requirement 2, refuses while an ORCH session is live) ----------------

def is_orch_live(root: Path) -> bool:
    """The same state file the dispatcher writes every tick (`dispatch.py`, `STATE_FILE`): true while ANY
    ORCH session is live in this target, finishing ones included (for the callers that need every live
    session). The core update's refusal does not use this: it uses `blocking_orch_live`, which ignores
    finishing sessions (issue #188 rule 2). What the old guard protected (#153 acceptance 6, "a finishing
    session still holds the checkout"): a finishing session writes to the main checkout only under the
    merge-then-checkout lock, which the update waits on, so the update waits for that write, never for the
    session to end. Prefers the full
    ``sessions`` list (issue #191: several may live, and the top-level ``session`` mirrors only the
    sole non-finishing one, so it reads ``null`` when every live session happens to be finishing);
    an older state file with no ``sessions`` key falls back to the top-level ``session``."""

    return _orch_live(root, finishing_counts=True)


def blocking_orch_live(root: Path) -> bool:
    """`is_orch_live` without the finishing sessions (issue #188 rule 2): true only while a session that is
    not finishing is live. This is the core update's refusal. A finishing session takes no new work, and what
    it still does in the main checkout (its merge, its STATE.md entry) runs under the merge-then-checkout lock,
    which `_take_home` waits on (`_target_lock`), so the update never runs during a merge; the update waits for
    that lock, never for the session to end."""

    return _orch_live(root, finishing_counts=False)


def _orch_live(root: Path, *, finishing_counts: bool) -> bool:
    path = root / ORCH_STATE_ENTRY
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    sessions = data.get("sessions")
    if isinstance(sessions, list):
        if finishing_counts:
            return len(sessions) > 0
        # a session entry the file does not describe as finishing counts as live (the conservative reading)
        return any(not (isinstance(entry, dict) and entry.get("finishing")) for entry in sessions)
    # the top-level `session` mirrors only the sole non-finishing session (issue #191), so it always counts
    return bool(data.get("session"))


# -- hook registrations (issue #228: a hook's file can travel to a target through the manifest
# without ever being registered in that target's own `.claude/settings.json`) -------------------

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count_hook_entries(template: dict) -> int:
    return sum(len(entries) for entries in template.get("hooks", {}).values())


def _missing_hook_entries(template: dict, settings: dict) -> int:
    existing_hooks = settings.get("hooks", {})
    missing = 0
    for event, entries in template.get("hooks", {}).items():
        existing_entries = existing_hooks.get(event, [])
        for entry in entries:
            if entry not in existing_entries:
                missing += 1
    return missing


def _missing_hook_registrations(root: Path) -> int:
    """0 when the target has no local copy of the template yet, or its `.claude/settings.json`
    already registers every hook the template names (`core status`, acceptance 4)."""

    template_path = root / HOOKS_TEMPLATE_ENTRY
    settings_path = root / SETTINGS_JSON_ENTRY
    if not template_path.is_file() or not settings_path.is_file():
        return 0
    return _missing_hook_entries(_load_json(template_path), _load_json(settings_path))


def _missing_deny_rules(root: Path) -> list[str]:
    """The template's `permissions.deny` rules the target's `.claude/settings.json` lacks; empty
    when either file is absent (`core status`, #312)."""

    template_path = root / HOOKS_TEMPLATE_ENTRY
    settings_path = root / SETTINGS_JSON_ENTRY
    if not template_path.is_file() or not settings_path.is_file():
        return []
    wanted = _load_json(template_path).get("permissions", {}).get("deny", [])
    have = _load_json(settings_path).get("permissions", {}).get("deny", [])
    return [rule for rule in wanted if rule not in have]


def _preview_hooks_template(root: Path, pending: dict[str, bytes]) -> tuple[int, int, bool, bytes | None]:
    """Merges the target's own (just-copied, or about-to-be-copied) `settings.hooks.json` into its
    `.claude/settings.json`, with the same rules `port.py` uses (`_merge_hooks` /
    `_merge_enabled_plugins`), touching no other key -- but writes nothing: reads the template from
    ``pending`` (about to be written by this same update) when present, else from disk, and returns
    the merged `.claude/settings.json` bytes it *would* write instead of writing them. Used so
    requirement 1 (the preflight) can name every path the update would touch before any write
    happens; the caller writes ``hooks_bytes`` itself once the preflight has passed. Returns
    ``(registered, total, wrote, hooks_bytes)``."""

    template_path = root / HOOKS_TEMPLATE_ENTRY
    template_bytes = pending.get(HOOKS_TEMPLATE_ENTRY)
    if template_bytes is None:
        if not template_path.is_file():
            return (0, 0, False, None)
        template_bytes = template_path.read_bytes()
    template = json.loads(template_bytes.decode("utf-8"))
    total = _count_hook_entries(template)

    settings_path = root / SETTINGS_JSON_ENTRY
    if not settings_path.is_file():
        return (0, total, False, None)

    settings = _load_json(settings_path)
    hooks_changed = _merge_hooks(settings, template)
    plugins_changed = _merge_enabled_plugins(settings, template)
    deny_changed = _merge_permissions_deny(settings, template)
    wrote = hooks_changed or plugins_changed or deny_changed
    new_bytes = None
    if wrote:
        new_bytes = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    registered = total - _missing_hook_entries(template, settings)
    return (registered, total, wrote, new_bytes)


# -- commit + push under the merge lock (issue #284) -------------------------------------------
# The commit step only ever runs against a real git repo with a remote: a target `core_update.py`
# is copied into can be a plain folder in a test fixture, and this must leave that path exactly as
# `run_core_update` behaved before #284 (no git calls at all).

def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=no_window_flags(), env=full_env)


def _is_committable_repo(root: Path, remote: str = "origin") -> bool:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    has_remote = _git(root, "remote", "get-url", remote)
    return has_remote.returncode == 0


def _dirty_paths(root: Path, paths: set[str]) -> list[str]:
    """Requirement 1: which of ``paths`` (relative to ``root``) already have an uncommitted
    change (staged, unstaged or untracked) in the target's own checkout: `git status --porcelain` reads the
    index column and the worktree column alike. A status that cannot be read (non-zero) counts as dirty:
    clean is never assumed (#198), and the undo's `undone, nothing changed` rests on this answer."""

    dirty: list[str] = []
    for path in sorted(paths):
        status = _git(root, "status", "--porcelain", "--", path)
        if status.returncode != 0 or status.stdout.strip():
            dirty.append(path)
    return dirty


def _git_holds(root: Path, entry: str, content: bytes) -> bool:
    """True when ``entry`` is tracked, clean in the working tree, and the blob git would store for
    ``content`` (its autocrlf / attribute filters applied) is the blob it already holds: the bytes on
    disk differ from the home's (CRLF written by a checkout under `core.autocrlf=true`) but the target
    holds exactly this content, so there is nothing to write or commit (#48)."""

    if not _is_committable_repo(root):
        return False
    if _git(root, "status", "--porcelain", "--", entry).stdout.strip():
        return False
    held = _git(root, "rev-parse", f":{entry}")
    if held.returncode != 0:
        return False
    proc = subprocess.run(["git", "hash-object", f"--path={entry}", "--stdin"], cwd=str(root), input=content,
                          capture_output=True, creationflags=no_window_flags())
    return proc.returncode == 0 and proc.stdout.decode("ascii", "replace").strip() == held.stdout.strip()


def _undo_commit(root: Path, paths: list[str]) -> None:
    """After a refused push: drop the local commit and put ``paths`` back exactly as they were
    before it -- whether that means restoring the old content of a path `HEAD~1` already held, or
    removing a path the update itself created (absent from `HEAD~1`, so `git restore --source=HEAD`
    has nothing to restore it *to*). Every git call's exit code is checked; a failure raises
    `CoreUpdateError` naming what is left undone rather than silently leaving a partial state."""

    reset = _git(root, "reset", "--soft", "HEAD~1")
    if reset.returncode != 0:
        raise CoreUpdateError(
            "core update: push refused, and the undo itself failed at 'git reset --soft HEAD~1' "
            f"({(reset.stderr or '').strip()[:200]}); left as the refused commit made them: "
            + ", ".join(paths)
        )

    pre_existing: list[str] = []
    newly_created: list[str] = []
    for path in paths:
        exists = _git(root, "cat-file", "-e", f"HEAD:{path}")
        (pre_existing if exists.returncode == 0 else newly_created).append(path)

    if pre_existing:
        restore = _git(root, "restore", "--staged", "--worktree", "--source=HEAD", "--", *pre_existing)
        if restore.returncode != 0:
            raise CoreUpdateError(
                "core update: push refused, and the undo itself failed restoring "
                f"{', '.join(pre_existing)} ({(restore.stderr or '').strip()[:200]})"
            )

    if newly_created:
        unstage = _git(root, "restore", "--staged", "--", *newly_created)
        if unstage.returncode != 0:
            raise CoreUpdateError(
                "core update: push refused, and the undo itself failed unstaging "
                f"{', '.join(newly_created)} ({(unstage.stderr or '').strip()[:200]})"
            )
        for path in newly_created:
            dest = root / path
            try:
                dest.unlink(missing_ok=True)
            except OSError as exc:
                raise CoreUpdateError(
                    f"core update: push refused, and the undo itself failed removing {path} ({exc!r})"
                )


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _git_reason(proc: subprocess.CompletedProcess) -> str:
    """Why a git call failed, never empty: the first non-empty line of stderr, else of stdout (a hook
    prints its refusal there), else `rc <n>` (#42)."""

    return (_first_line(proc.stderr) or _first_line(proc.stdout) or f"rc {proc.returncode}")[:200]


def _undo_failed_commit(root: Path, before: dict[str, bytes | None], paths: list[str], step: str,
                        detail: str) -> CoreUpdateError:
    """After a failed `git add` / `git commit` (nothing was committed): unstage ``paths`` (best effort:
    a stuck index.lock fails `add` and `reset` alike) and always put every path of ``before`` back (its
    old bytes, or deleted when the update created it), `core_version` included. The text ends
    `undone, nothing changed` when ``paths`` are clean afterwards, else names what is still dirty.
    Returns the error for the caller to raise."""

    _git(root, "reset", "-q", "--", *paths)
    _restore(root, before)
    dirty = _dirty_paths(root, set(paths))
    if dirty:
        return CoreUpdateError(f"core update: git {step} failed ({detail}), and the undo left uncommitted "
                               f"change(s) at {', '.join(dirty)}")
    return CoreUpdateError(f"core update: git {step} failed: {detail}; undone, nothing changed")


def _commit_and_push(root: Path, paths: set[str], head: str, before: dict[str, bytes | None]) -> None:
    """Commits exactly ``paths`` (by name) on the target's current branch and pushes it. The caller holds
    the merge-then-checkout lock (`_target_lock`, taken before the first write, #49): this step never takes
    it, so a run never waits on the lock it already holds. A refused push is undone in full (``_undo_commit``): the local commit is removed and the
    written paths are restored to their pre-update state, touching no other path; never a forced
    push. ``--only`` on the commit keeps the target's own staged-but-not-``paths`` changes staged
    and out of this commit (requirement 2) even though ``git add`` ran first (needed so a brand
    new path is known to git at all before ``--only`` can name it).

    `run_commit` (`merge.py:835`) is not reused here: on a refused push it keeps the local commit
    and retries by rebasing (a policy fit for its own callers), where requirement 3 needs the
    opposite -- undo everything and raise. Plain git under the same lock is a smaller surface for
    that behaviour than adapting `run_commit`'s retry policy."""

    ordered = sorted(paths)
    add = _git(root, "add", "--", *ordered)
    if add.returncode != 0:
        raise _undo_failed_commit(root, before, ordered, "add", _git_reason(add))

    if _git(root, "diff", "--cached", "--quiet", "--", *ordered).returncode == 0:
        return  # git already holds every written path (equal after its filters): nothing to commit or push (#48)

    commit = _git(root, "commit", "-m", f"Core update: {head[:8]}", "--only", "--", *ordered)
    if commit.returncode != 0:
        raise _undo_failed_commit(root, before, ordered, "commit", _git_reason(commit))

    branch_result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_result.stdout.strip() or HOME_BRANCH

    push = _git(root, "push", "origin", branch, env={"AGENTOPS_MAIN_PUSH": "1"})
    if push.returncode != 0:
        _undo_commit(root, ordered)
        dirty = _dirty_paths(root, paths)
        if dirty:
            raise CoreUpdateError(f"core update: push refused ({_git_reason(push)}), and the undo left "
                                  f"uncommitted change(s) at {', '.join(dirty)}")
        raise CoreUpdateError(
            f"core update: push refused ({_git_reason(push)}); undone, nothing changed"
        )


# -- status -----------------------------------------------------------------------------------

@dataclass
class StatusResult:
    changes: int
    target_version: str | None
    home_head: str | None
    message: str
    closed_taken: list[int] = field(default_factory=list)
    closed_waiting: list[int] = field(default_factory=list)
    closed_older: list[int] = field(default_factory=list)


_ISSUE_REF_RE = re.compile(r"#(\d+)")
_REPO_FIELD_RE = re.compile(r"\*\*repo:\*\*\s*(\S+)")


def _referenced_issues(messages: list[str]) -> list[int]:
    """``#N`` references across ``messages``, in first-seen order, never repeated."""

    seen: list[int] = []
    for text in messages:
        for match in _ISSUE_REF_RE.findall(text):
            number = int(match)
            if number not in seen:
                seen.append(number)
    return seen


def _issue_repo(body: str) -> str | None:
    match = _REPO_FIELD_RE.search(body or "")
    return match.group(1) if match else None


def _taken_before(root: Path, settings: dict) -> str | None:
    """The version a taken update replaced: the settings' own record when present, else the
    parent's ``core_version`` at the last commit that changed it in ``config/orchestration.json``
    (the bootstrap case -- an update run before this repo wrote ``core_version_before`` at all).
    Local git only; ``None`` when nothing is found."""

    explicit = settings.get("core_version_before")
    if explicit:
        return explicit

    found = _git(root, "log", "-n1", "-G", "core_version", "--format=%H", "--",
                "config/orchestration.json")
    if found.returncode != 0:
        return None
    sha = found.stdout.strip()
    if not sha:
        return None

    parent = _git(root, "show", f"{sha}^:config/orchestration.json")
    if parent.returncode != 0:
        return None
    try:
        data = json.loads(parent.stdout)
    except ValueError:
        return None
    return data.get("core_version")


def _closed_line(label: str, before: str, after: str, refs: list[int],
                 core_issues: dict[int, dict], repo: str | None) -> str:
    kept = [n for n in refs if n in core_issues]
    head_line = f"{label} ({before[:8]} -> {after[:8]}): {len(kept)} core issue(s)"
    if not kept:
        return head_line
    named = []
    for number in kept:
        issue = core_issues[number]
        marker = " (this repo's)" if _issue_repo(issue.get("body") or "") == repo else ""
        named.append(f"#{number} {issue.get('title', '')}{marker}")
    return head_line + ": " + ", ".join(named)


NO_AREAS_LINE = ("no areas set in config/orchestration.json: the brain proposes them in this chat "
                 "(python -m tools.agentops areas propose)")


def _touches(commit: dict, commits: list[dict], direct: set[str]) -> bool:
    """``direct``: shas whose own diff touches a manifest path. A merge commit also counts when a
    commit of the range reachable through its later parents (and not through its first) is direct."""

    if commit["sha"] in direct:
        return True
    parents = commit.get("parents") or []
    if len(parents) < 2:
        return False
    by_sha = {c["sha"]: c for c in commits}

    def reach(starts: list[str]) -> set[str]:
        seen: set[str] = set()
        stack = [sha for sha in starts if sha in by_sha]
        while stack:
            sha = stack.pop()
            if sha in seen:
                continue
            seen.add(sha)
            stack.extend(p for p in (by_sha[sha].get("parents") or []) if p in by_sha)
        return seen

    return bool((reach(parents[1:]) - reach(parents[:1])) & direct)


def run_core_status(root: Path = ROOT, gh_runner=None) -> StatusResult:
    settings = load_settings(root)
    if settings.get("update_rule") == HOME_RULE:
        message = "this is the home: core status changes nothing"
        return StatusResult(changes=0, target_version=None, home_head=None, message=message)

    if _is_committable_repo(root):
        try:
            _recover_leftover(root, CORE_UPDATE_WAIT_S)  # a killed update's paths first (#49); a busy lock waits
        except CoreUpdateBusy:
            pass
        settings = load_settings(root)

    target_version = settings.get("core_version")
    reader = HomeReader(settings["home"], gh_runner=gh_runner)
    head = reader.head_sha()

    manifest_cache: dict[str, set] = {}

    def manifest_of(sha: str) -> set:
        if sha not in manifest_cache:
            manifest_cache[sha] = set(reader.manifest(sha))
        return manifest_cache[sha]

    def touching(commits: list[dict], manifest_sha: str) -> list[dict]:
        """The commits whose change touches a manifest file: read from the path histories (a few
        gh calls), a merge commit counting when any commit it brought in does (``commits/<sha>``
        of a merge lists the files it brought, which the path histories skip)."""

        if not commits:
            return []
        manifest_set = manifest_of(manifest_sha)
        direct: set[str] = set()
        if manifest_set:
            history = reader.paths_history(manifest_sha, sorted(manifest_set),
                                           min(c["date"] for c in commits))
            for shas in history.values():
                direct |= shas
        return [c for c in commits if _touches(c, commits, direct)]

    waiting_nonempty = bool(target_version) and target_version != head
    waiting_commits: list[dict] = []
    waiting_touching: list[dict] = []
    if not waiting_nonempty:
        changes = 0
    else:
        waiting_commits = reader.commits_between_with_messages(target_version, head)
        waiting_touching = touching(waiting_commits, head)
        changes = len(waiting_touching)

    missing_hooks = _missing_hook_registrations(root)
    if missing_hooks:
        changes += missing_hooks

    missing_deny = _missing_deny_rules(root)
    changes += len(missing_deny)

    lines = [f"{changes} core change(s) waiting ({(target_version or '?')[:8]} -> {head[:8]})"]
    if missing_deny:
        lines.append(f"{SETTINGS_JSON_ENTRY} lacks permissions.deny: {', '.join(missing_deny)} "
                     "(core update adds it)")

    before = _taken_before(root, settings)
    taken_nonempty = bool(before) and before != target_version

    taken_touching: list[dict] = []
    if taken_nonempty:
        taken_commits = reader.commits_between_with_messages(before, target_version)
        taken_touching = touching(taken_commits, target_version)
    taken_refs = _referenced_issues([c["message"] for c in taken_touching])
    waiting_refs = _referenced_issues([c["message"] for c in waiting_touching])

    repo = settings.get("repo")
    # Rule 2: one issue-list call per `core status`, always -- so a core issue this repo's own
    # taken update or an older one closed is never left unaccounted for.
    core_issue_map: dict[int, dict] = {row["number"]: row for row in reader.core_issues()}

    closed_taken: list[int] = []
    closed_waiting: list[int] = []
    if taken_nonempty:
        lines.append(_closed_line("closed by the update taken", before, target_version,
                                  taken_refs, core_issue_map, repo))
        closed_taken = [n for n in taken_refs if n in core_issue_map]
    if waiting_nonempty:
        lines.append(_closed_line("would close if taken", target_version, head,
                                  waiting_refs, core_issue_map, repo))
        closed_waiting = [n for n in waiting_refs if n in core_issue_map]

    named = set(taken_refs) | set(waiting_refs)
    closed_older: list[int] = []
    for number, issue in core_issue_map.items():
        if number in named:
            continue
        if issue.get("state") != "CLOSED":
            continue
        if _issue_repo(issue.get("body") or "") != repo:
            continue
        closed_older.append(number)
        lines.append(f"#{number} {issue.get('title', '')} (this repo's): closed, already "
                    f"in the taken core ({(target_version or '?')[:8]})")

    if not settings.get("areas"):
        lines.append(NO_AREAS_LINE)

    return StatusResult(changes=changes, target_version=target_version, home_head=head,
                        message="\n".join(lines), closed_taken=closed_taken,
                        closed_waiting=closed_waiting, closed_older=closed_older)


# -- collisions with the target's local modules (#53) --------------------------------------------

CLI_ENTRY = "tools/agentops/cli.py"
PACKAGE_DIR = "tools/agentops"  # where a `.name` local module lives (the same rule as extensions.Registry.load)


def _module_files(name: str) -> list[str]:
    """The repo-relative files a `local_modules` name can be, as `extensions.Registry.load` imports it: `.a.b` is
    `tools.agentops.a.b` (`tools/agentops/a/b.py`), any other name is a dotted path from the repo root; a module is
    `<path>.py` or a package `<path>/__init__.py`."""

    base = f"{PACKAGE_DIR}/{name[1:].replace('.', '/')}" if name.startswith(".") else name.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _add_parser_names(source: str) -> list[tuple[str, int]]:
    """Every string literal passed first to an `.add_parser(...)` call in ``source``, with its line. Parsed,
    never imported; a source that does not parse registers nothing."""

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_parser"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.append((node.args[0].value, node.lineno))
    return sorted(found, key=lambda item: item[1])


def _local_module_collisions(root: Path, settings: dict, manifest: list[str], reader: HomeReader,
                             head: str, read_file=None) -> list[str]:
    """One phrase per collision between the target's `local_modules` and the core about to be copied: a module
    file that is a manifest path, or a subcommand the module registers that the home's `cli.py` registers too.
    The modules `port` seeds (`CORE_LOCAL_MODULES`) are core's own and skipped. Reads only; empty when the target
    lists no local module. Known limits: core subcommands registered outside `cli.py`, commands registered through
    `core.command()`, submodules of a package module and a malformed `local_modules` list are not checked."""

    names = settings.get("local_modules")
    if not isinstance(names, list) or not names:
        return []
    core_commands: dict[str, int] = {}
    if CLI_ENTRY in manifest:
        cli_bytes = read_file(CLI_ENTRY) if read_file is not None else reader.file_bytes(CLI_ENTRY, head)
        for command, line in _add_parser_names(cli_bytes.decode("utf-8", "replace")):
            core_commands.setdefault(command, line)
    manifest_paths = set(manifest)
    phrases: list[str] = []
    for name in (n for n in names if isinstance(n, str) and n not in CORE_LOCAL_MODULES):
        for rel in _module_files(name):
            if rel in manifest_paths:
                phrases.append(f"local module {name} ({rel}) collides with core {rel}")
            path = root / rel
            if not path.is_file():
                continue
            local = _add_parser_names(path.read_text(encoding="utf-8", errors="replace"))
            for command in dict.fromkeys(command for command, _ in local):
                if command in core_commands:
                    phrases.append(f"local module {name} ({rel}) collides with core subcommand "
                                   f"'{command}' registered at {CLI_ENTRY}:{core_commands[command]}")
    return phrases


# -- update -----------------------------------------------------------------------------------

@dataclass
class UpdateResult:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    home_head: str | None = None
    hooks_registered: int = 0
    hooks_total: int = 0
    message: str = ""
    kept: list[str] = field(default_factory=list)  # test copies left in place: no same-named file at the home (#73)
    waited: bool = False  # the tree was current only after the lock: a concurrent update brought it there (#76)


def _target_repo(root: Path) -> str:
    """`owner/name` of the target's origin remote (#93), else the root folder's name when origin is unreadable."""
    proc = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=no_window_flags())
    match = _ORIGIN_NAME_RE.search(proc.stdout.strip()) if proc.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else root.name


_ORIGIN_NAME_RE = re.compile(r"([^/:\\]+)[/:\\]([^/:\\]+?)(?:\.git)?[/\\]?$")


def _sync_core_labels(root: Path, gh_runner=None) -> None:
    """#122: the core's labels exist in the target: `port.sync_labels` with the target's own list and repo, creating
    only the missing ones. Best-effort: a failure is one log line and never fails the update; the next taken update
    lists again by itself. Logs only what it created."""

    labels_path = root / CORE_LABELS_ENTRY
    if not labels_path.is_file():
        return
    try:
        created, _ = sync_labels(_target_repo(root), gh_runner=gh_runner, labels=load_core_labels(labels_path))
    except Exception as exc:  # best-effort: the update is taken whatever the label call says
        _log_line(root, f"core update labels: not synced: {_first_line(str(exc)) or type(exc).__name__}; "
                        f"retried at the next update")
        return
    if created:
        _log_line(root, f"core update labels: created {', '.join(created)}")


def run_update_steps(root: Path, gh_runner=None) -> list[str]:
    """The post-copy steps of `core update`, run by whatever code sits in ``root`` right now: writes the
    target's `CODING_STANDARDS.md` from the copied stub and core standards file when the target has none
    (#44; an existing file is never touched), writes the starter `config/core_leak_patterns.json` when the
    target has none (#93; its L3 names the target's folder and origin repo), merges the target's
    `settings.hooks.json` into `.claude/settings.json` (hooks, enabledPlugins, permissions.deny) when anything
    changed, appends to the monitor's `project.ts` each export the hooks import that it lacks (#172, #198), and
    lists the core's labels in the target's repo, creating the missing ones (#122; best-effort, it writes no file). Idempotent. Returns the paths (relative to ``root``) it wrote. `core update` runs this in a fresh
    subprocess AFTER the copy, so a step the update itself brings is done in the same run (#316)."""

    written: list[str] = []
    standards = root / CODING_STANDARDS_ENTRY
    stub = root / STANDARDS_STUB_ENTRY
    core_standards = root / CORE_STANDARDS_ENTRY
    if not standards.is_file() and stub.is_file() and core_standards.is_file():
        rendered = render_coding_standards(stub.read_text(encoding="utf-8"),
                                           core_standards.read_text(encoding="utf-8"))
        standards.write_bytes(rendered.encode("utf-8"))
        written.append(CODING_STANDARDS_ENTRY)

    if write_starter(root, _target_repo(root)) is not None:
        written.append(PATTERNS_ENTRY)

    _, _, wrote, new_bytes = _preview_hooks_template(root, {})
    if wrote:
        (root / SETTINGS_JSON_ENTRY).write_bytes(new_bytes)
        written.append(SETTINGS_JSON_ENTRY)
    if _append_monitor_exports(root):
        written.append(MONITOR_PROJECT_ENTRY)
    _sync_core_labels(root, gh_runner)
    return written


def _append_monitor_exports(root: Path) -> bool:
    """#172, run from the updated code (#198): appends to the target's monitor ``project.ts`` each export the updated
    hooks import that it lacks, from the updated stub with the target's placeholders filled
    (``project_exports.append_missing``); True when it wrote. Nothing missing or no file: no write. A project.ts with
    an uncommitted edit of the target's own raises ``CoreUpdateRefused`` (on a git target with a remote), and a name
    the stub lacks raises ``CoreUpdateError``; the caller puts every path back."""

    hooks = root / MONITOR_DIR / "hooks"
    if not project_exports.missing_names(hooks) or not (hooks / project_exports.PROJECT_FILE).is_file():
        return False
    if _is_committable_repo(root) and _dirty_paths(root, {MONITOR_PROJECT_ENTRY}):
        detail = f"uncommitted change(s) at {MONITOR_PROJECT_ENTRY}"
        raise CoreUpdateRefused(f"core update refused: {detail}", detail)
    try:
        return project_exports.append_missing(hooks, monitor_stub_values(root, load_settings(root).get("repo") or root.name),
                                              home_root=root)
    except ValueError as exc:
        raise CoreUpdateError(f"cannot append to {MONITOR_PROJECT_ENTRY}: {exc}") from exc


def _restore(root: Path, before: dict[str, bytes | None]) -> None:
    """Put every path back as `before` holds it: its old bytes, or deleted when it was absent."""

    for entry, content in before.items():
        path = root / entry
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def _step_failure_line(proc: subprocess.CompletedProcess) -> str:
    """The last non-empty line of the step's stderr (else of its stdout, else its return code): the exception line."""
    for text in (proc.stderr, proc.stdout):
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"rc {proc.returncode}"


def _write_step_output(root: Path, proc: subprocess.CompletedProcess) -> Path:
    """The step's full stderr and stdout, in `artifacts/runs/core_update_steps-<YYYYmmdd-HHMMSS>.err` of the target (#53)."""
    path = root / "artifacts" / "runs" / f"core_update_steps-{time.strftime('%Y%m%d-%H%M%S')}.err"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = proc.stderr or ""
    if proc.stdout:
        text += "\n--- stdout ---\n" + proc.stdout
    path.write_text(text, encoding="utf-8")
    return path


def _subprocess_step_runner(root: Path) -> list[str]:
    """Default step runner: the UPDATED code in a fresh interpreter, cwd = the target root. Falls
    back to the in-process steps only when the target has no importable `tools/agentops` package (a plain
    fixture folder). A failing subprocess raises `CoreUpdateError` that names its exception line and the file
    with the full output (#53); nothing is committed, and
    `run_core_update` puts every copied file back as it was (#352), so a rerun repeats everything."""

    if not (root / "tools" / "agentops" / "__init__.py").is_file():
        return run_update_steps(root)
    proc = subprocess.run([sys.executable, "-m", "tools.agentops", "core", "update-steps"], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=no_window_flags())
    if proc.returncode == STEP_REFUSED_RC:
        reason = _step_failure_line(proc)
        raise CoreUpdateRefused(f"core update refused: {reason}", reason)
    if proc.returncode != 0:
        output = _write_step_output(root, proc)
        raise CoreUpdateError(f"{_step_failure_line(proc)} (full output: {output})")
    try:
        return list(json.loads(proc.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError) as exc:
        raise CoreUpdateError(f"core update: the post-copy steps printed no JSON path list ({exc!r})")


@dataclass
class _Plan:
    """What one update would write, worked out from the target's tree as it is right now."""

    pending_content: dict[str, bytes]
    dropped: list[str]
    pending_removed: list[str]
    hooks_wrote: bool
    touched: set[str]
    kept: list[str] = field(default_factory=list)  # test copies left in place: the home has no file of that name
    copies: list[str] = field(default_factory=list)  # target test copies this update removes (#73)
    home_count: int | None = None  # #157: commits on the home's main up to the taken head, None when not had


def _test_copies(root: Path, new_manifest: list[str], home_tests) -> tuple[list[str], list[str]]:
    """(#73) The target's ``tests/test_*.py`` that only test the manifest's modules (group a of ``split_core_tests``
    against the NEW manifest): ``(copies, kept)``, the paths to remove (the home holds a same-named file) and the
    names with no home copy left in place (a candidate held back because a staying file imports it is in neither list). ``home_tests`` is a callable returning the home's tests file names,
    called only when a candidate exists.

    The removal set is worked to a fixed point: a file whose only touch of the target's own code is a test file being
    removed counts as group a too (``absent``), and goes when the home holds its name; a candidate that a file
    staying in the target imports is held back (and so is what it imported), until no kept file imports a removed one."""

    if not (root / "tests").is_dir():
        return [], []
    with tempfile.TemporaryDirectory() as scratch:
        manifest_file = Path(scratch) / "manifest.txt"
        manifest_file.write_text("\n".join(new_manifest) + "\n", encoding="utf-8")
        only_core, _ = split_core_tests(root, manifest_file)
        if not only_core:
            return [], []
        at_home = home_tests()
        imports = imported_test_modules(root)
        held: set[str] = set()  # candidates a staying file imports: they stay
        while True:
            removal: set[str] = set()
            while True:  # grow: a file stops touching the target's own once its helpers are in the removal set
                only_core, _ = split_core_tests(root, manifest_file, absent=removal)
                grown = removal | {name for name in only_core if name in at_home and name not in held}
                if grown == removal:
                    break
                removal = grown
            needed = {name for name in removal
                      if any(name[: -len(".py")] in stems for other, stems in imports.items() if other not in removal)}
            if not needed:
                break
            held |= needed
    return ([f"tests/{name}" for name in sorted(removal)],
            [name for name in only_core if name not in at_home])


def _plan_update(root: Path, settings: dict, head: str, remote: dict[str, bytes],
                 new_manifest: list[str], old_manifest: list[str], home_tests=None) -> _Plan:
    """Phase 1: works out what would be written/removed from ``remote`` (the home's bytes, read once) and the
    target's tree, without touching anything. Called again once the lock is held (#49): a merge that ran
    while this update waited may have changed the tree."""

    pending_content: dict[str, bytes] = {}
    for entry in new_manifest:
        if entry in TARGET_OWNED_ENTRIES:
            continue  # the target's own answers to Pocock's setup, never rewritten (issue #224)
        content = remote[entry]
        dest = root / entry
        if not dest.is_file() or (dest.read_bytes() != content and not _git_holds(root, entry, content)):
            pending_content[entry] = content

    # every path the new manifest dropped counts as removed; one already gone (never there, or taken by
    # a concurrent update of this target) is removed all the same and is committed only when git knows it (#42)
    dropped = [entry for entry in sorted(set(old_manifest) - set(new_manifest))
               if entry not in TARGET_OWNED_ENTRIES]
    kept: list[str] = []
    copies: list[str] = []
    if home_tests is not None:  # a target keeps only tests of its own files (#73): the home's copy replaces the target's
        copies, kept = _test_copies(root, new_manifest, home_tests)
        dropped = sorted(set(dropped) | set(copies))
    pending_removed = [entry for entry in dropped if (root / entry).is_file()]

    _, _, hooks_wrote, _ = _preview_hooks_template(root, pending_content)

    touched: set[str] = set(pending_content) | set(pending_removed)
    if hooks_wrote:
        touched.add(SETTINGS_JSON_ENTRY)
    if settings.get("core_version") != head:
        touched.add(ORCHESTRATION_JSON_ENTRY)
    return _Plan(pending_content, dropped, pending_removed, hooks_wrote, touched, kept, copies)


#: #198: the git operations a target's checkout can be in the middle of, each with the marker git leaves for it
MID_OPERATIONS = (
    ("MERGE_HEAD", "mid-merge"),
    ("CHERRY_PICK_HEAD", "mid-cherry-pick"),
    ("REVERT_HEAD", "mid-revert"),
    ("rebase-merge", "mid-rebase"),
    ("rebase-apply", "mid-rebase"),
)


def _mid_operation(root: Path) -> str | None:
    """#198: the operation the target's checkout is in the middle of (``mid-merge``, ...), or None. The git dir is
    resolved by git (``--git-path``), so a linked worktree's `.git` file counts; a folder outside git answers None."""

    for marker, label in MID_OPERATIONS:
        proc = _git(root, "rev-parse", "--git-path", marker)
        if proc.returncode == 0 and proc.stdout.strip() and (root / proc.stdout.strip()).exists():
            return label
    return None


def _refuse_mid_operation(root: Path, settings: dict, reader: HomeReader, log: _UpdateLog) -> None:
    """#198: a target mid-merge, mid-cherry-pick, mid-revert or mid-rebase is refused before the lock, the snapshot
    or any write: the operation's own staged state would otherwise be folded into the update's commit."""

    mid = _mid_operation(root)
    if mid is None:
        return
    try:
        log.name(reader.head_sha())  # the refusal's line names the home head when that can be read
    except CoreUpdateError:
        pass  # no home head to name: the refusal stands without its line
    reason = f"{settings.get('repo') or root.name} is {mid}"
    raise CoreUpdateRefused(f"core update refused: {reason}", reason)


def _refuse_dirty(root: Path, plan: _Plan) -> None:
    """The preflight: a touched path with an uncommitted edit of the target's own is refused up front. The
    post-copy steps run the UPDATED code (#316) and may write settings.json where the old preview does not:
    its own uncommitted edit is refused up front too (#352)."""

    dirty = _dirty_paths(root, plan.touched | {SETTINGS_JSON_ENTRY})
    if dirty:
        detail = f"uncommitted change(s) at {', '.join(dirty)}"
        raise CoreUpdateRefused(f"core update refused: {detail}", detail)


# -- the snapshot a killed update leaves (#49) --------------------------------------------------

def _log_line(root: Path, text: str) -> None:
    """One line in the target's own ``artifacts/runs/dispatch.out`` (the file the home merge writes its failure
    line to, `core_targets.py`); a log that cannot be written is not a reason to fail the update."""

    path = root / DISPATCH_OUT_ENTRY
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{now()} | {text}\n")
    except OSError:
        return


def _head_sha(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _write_snapshot(root: Path, home_head: str, before: dict[str, bytes | None], settings: dict) -> dict:
    """Before the first write: the old bytes of every path the update may write (base64; None = absent, so the
    update creates it), the target's HEAD and the two ``core_version`` values, in
    ``var/agentops/core_update_snapshot.json`` (written whole, then moved into place)."""

    snap = {
        "home_head": home_head,
        "target_head": _head_sha(root),
        "core_version": settings.get("core_version"),
        "core_version_before": settings.get("core_version_before"),
        "before": {rel: None if content is None else base64.b64encode(content).decode("ascii")
                   for rel, content in before.items()},
        "created": sorted(rel for rel, content in before.items() if content is None),
    }
    path = root / SNAPSHOT_ENTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return snap


def _read_snapshot(path: Path) -> dict | None:
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        before = snap["before"]
        if not isinstance(before, dict) or not isinstance(snap["target_head"], (str, type(None))):
            return None
        return snap
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _inside(root: Path, rel: str) -> bool:
    try:
        (root / rel).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return not Path(rel).is_absolute()


def _landed(root: Path, snap: dict) -> bool:
    """The update's own commit (subject ``Core update: <home sha8>``, `_commit_and_push`) is in HEAD's history
    since the target head the snapshot recorded. Any other commit proves nothing about the update."""

    home = snap.get("home_head")
    if not home:
        return False
    log = _git(root, "log", "--format=%s", f"{snap['target_head']}..HEAD")
    return log.returncode == 0 and f"Core update: {str(home)[:8]}" in log.stdout.splitlines()


def _head_bytes(root: Path, rel: str) -> bytes | None:
    """``rel`` as HEAD holds it, byte for byte; None when HEAD has no such path."""

    proc = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(root), capture_output=True,
                          creationflags=no_window_flags())
    return proc.stdout if proc.returncode == 0 else None


def _restore_snapshot(root: Path, snap: dict) -> int:
    """Puts the snapshot's paths back (old bytes; deleted when the update created them; unstaged) unless the
    update's own commit landed (``_landed``). Touches no path outside the snapshot. HEAD may have moved on for
    another reason (the target's own commit): a path that commit changed is the target's now and is left alone.
    Returns how many paths differed and were put back; 0 when the update's commit landed."""

    head = _head_sha(root)
    if not snap.get("target_head") or head is None:
        return 0
    moved = head != snap["target_head"]
    if moved and _landed(root, snap):
        return 0
    before: dict[str, bytes | None] = {}
    for rel, text in snap["before"].items():
        if _inside(root, rel):
            before[rel] = None if text is None else base64.b64decode(text)
    if moved:
        before = {rel: content for rel, content in before.items() if _head_bytes(root, rel) == content}
    differing = sum(1 for rel, content in before.items()
                    if ((root / rel).read_bytes() if (root / rel).is_file() else None) != content)
    if before:
        _git(root, "reset", "-q", "--", *before)
        _restore(root, before)
    return differing


def _recover_leftover(root: Path, wait_s: float | None) -> None:
    """A snapshot left by an update that was killed before its commit: under the lock, its paths are put back
    first (one log line) unless the update's own commit is in HEAD's history, and the file is deleted either way
    (an unreadable one is logged as such). A restore that cannot write is a one-line ``CoreUpdateError``. No file:
    nothing, and no lock is taken."""

    if not (root / SNAPSHOT_ENTRY).is_file():
        return
    with _target_lock(root, wait_s):
        _recover_leftover_locked(root)


def _recover_leftover_locked(root: Path) -> None:
    """The body of `_recover_leftover`, for a caller that already holds the target's lock."""

    path = root / SNAPSHOT_ENTRY
    if not path.is_file():  # the update that held the lock finished
        return
    snap = _read_snapshot(path)
    if snap is None:
        path.unlink(missing_ok=True)
        _log_line(root, "core update: unreadable snapshot removed")
        return
    try:
        restored = _restore_snapshot(root, snap)
        path.unlink(missing_ok=True)
    except OSError as exc:  # the file stays: the next run tries again
        raise CoreUpdateError("core update: could not restore the paths a killed update left "
                              f"({_first_line(str(exc))}); the snapshot stays for the next run") from exc
    if restored:
        _log_line(root, f"core update {str(snap['home_head'])[:8]}: restored {restored} path(s) "
                        "left by a killed update")


def _target_lock(root: Path, wait_s: float | None, sha8: str | None = None):
    """The target's merge-then-checkout lock (the one `merge.py`'s `run_commit` takes, resolved against the
    target's own root). A wait that ran out is a refusal, ``merge running``, before anything was written.
    Its ticket is ``{"kind": "core-update", "sha": <home head sha8>}`` (#203: `merge-status` shows it as its own row);
    with ``sha8`` None the ticket has no sha key until the home head is read (#207). The context yields the merge
    lock, so the holder can be named by the sha under the lock once the head is read."""
    ticket = {"kind": "core-update", **({"sha": sha8} if sha8 else {})}

    @contextmanager
    def held():
        try:
            with merge_then_checkout_locked(root / "var" / "locks" / "merge.lock", wait_s=wait_s,
                                            merge_ticket_info=ticket) as (merge_lock, _checkout):
                yield merge_lock
        except (MergeLockBusy, MergeLockTimeout) as exc:  # #188: a holder past the age ceiling is a busy line too
            raise CoreUpdateBusy(f"core update refused: merge running ({exc})") from exc

    return held()


class _UpdateLog:
    """The lines one core update writes to the target's dispatch.out (#49): ``core update <home sha8>: ...``. The
    home sha8 is known once the home head is read; ``begun`` is set when the writes start, so a failure after that
    always gets its one final line."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sha8: str | None = None
        self.begun = False

    def name(self, head: str) -> None:
        self.sha8 = head[:8]

    def line(self, text: str) -> None:
        if self.sha8 is not None:
            _log_line(self.root, f"core update {self.sha8}: {text}")

    def start(self) -> None:
        self.begun = True
        self.line("started")


def run_core_update(root: Path = ROOT, *, gh_runner=None, live_check=None, step_runner=None,
                    lock_wait_s: float | None = None, home_count=None, monitor_runner=None) -> UpdateResult:
    """Takes the home's latest core into ``root``. On a committable target the merge-then-checkout lock is
    taken before the first byte is written and held through the push (#49), so an update never sits half
    applied while it waits for a merge's suite; ``lock_wait_s`` bounds that wait (None: as long as the holder
    lives, `merge.py`'s age ceiling still applies) and a wait that ran out is refused ``merge running``.
    Every update that begins writes its own lines to the target's dispatch.out (#49): ``started`` once the
    lock is held and the writes begin, then one final line ``taken <sha8>``, ``refused: <reason>`` or
    ``failed: <reason>``. An update with nothing to write logs nothing. ``home_count`` (#157) is a callable
    ``sha -> int | None``, the commits on the home's main up to ``sha`` counted at a home checkout: a core push
    passes it, and the taken head's count lands as ``core_version_count`` with no extra GitHub call."""

    settings = load_settings(root)
    if settings.get("update_rule") == HOME_RULE:
        message = "this is the home: core update changes nothing"
        return UpdateResult(message=message)

    reader = HomeReader(settings["home"], gh_runner=gh_runner, count_of=home_count)
    log = _UpdateLog(root)
    try:
        return _take_home(root, settings, reader, log, live_check, step_runner, lock_wait_s, monitor_runner)
    except CoreUpdateBusy:
        log.line("refused: merge running")
        raise
    except CoreUpdateRefused as exc:
        log.line(f"refused: {exc.reason}")
        raise
    except CoreUpdateError as exc:
        log.line(f"failed: {_first_line(str(exc))}")
        raise
    except BaseException as exc:  # any other exit after `started` still ends the log with one line (#49)
        if log.begun:
            log.line("failed: interrupted" if isinstance(exc, KeyboardInterrupt)
                     else f"failed: {_first_line(str(exc)) or type(exc).__name__}")
        raise


def _take_home(root: Path, settings: dict, reader: HomeReader, log: _UpdateLog, live_check, step_runner,
               lock_wait_s: float | None, monitor_runner=None) -> UpdateResult:
    """The body of `run_core_update`: the live refusal; on a committable target then the lock (#76: before any
    read of the home), under it the leftover of a killed update, the home head, the plan, the preflight, the
    writes and the commit (`_apply_logged`); a target without a commit has no lock and plans first."""

    live = live_check if live_check is not None else (lambda: blocking_orch_live(root))  # #188 rule 2
    if live():
        try:
            log.name(reader.head_sha())  # the refusal's line names the home head when that can be read
        except CoreUpdateError:
            pass  # no home head to name: the refusal stands without its line
        raise CoreUpdateRefused("refusing: an ORCH session is live in this target",
                                "an ORCH session is live in this target")
    _refuse_mid_operation(root, settings, reader, log)  # #198: before the lock, the snapshot and any write

    committable = _is_committable_repo(root)
    if not committable:
        head, plan = _read_and_plan(root, settings, reader, log)
        return _apply_logged(root, head, plan, False, step_runner, settings, log, monitor_runner=monitor_runner)

    # #76: the lock first, so a burst of attempts on one target reads the home once, not once each. A killed
    # update's paths are put back first: they are not the target's own change
    try:
        with _target_lock(root, lock_wait_s, log.sha8) as lock:
            return _take_under_lock(root, settings, reader, log, step_runner, monitor_runner, lock=lock)
    except CoreUpdateBusy:
        try:
            log.name(reader.head_sha())  # the refusal's line names the home head when that can be read
        except CoreUpdateError:
            pass  # no home head to name: the refusal stands without its line
        raise


def _take_under_lock(root: Path, settings: dict, reader: HomeReader, log: _UpdateLog,
                     step_runner, monitor_runner=None, *, lock=None) -> UpdateResult:
    """The locked part of `_take_home` on a committable target: the killed update's leftover, then the home head,
    and either nothing more (a concurrent update brought the target there) or the reads, the plan and the apply.
    ``lock`` (#207) is the merge lock held by the caller: once the head is read, the holder is named by its sha8."""

    _recover_leftover_locked(root)
    waited_for = settings.get("core_version")
    settings = load_settings(root)
    head = reader.head_sha()  # after the restore: a home that cannot be read does not block it (#49)
    log.name(head)
    if lock is not None:  # #207: the holder's sha, rewritten in place under the lock already held
        try:
            lock.set_holder_fields({"sha": log.sha8})
        except (MergeLockTimeout, OSError):  # display only: a failed rewrite never aborts the update (MergeLockLost raises)
            pass
    # core_version moved to the home's head while this update waited: another update brought the target
    # here, so nothing more of the home is read. A target that already held the head before the wait is
    # compared file by file as before (#48: a local edit refused, a CRLF copy rewritten).
    if settings.get("core_version") == head != waited_for:
        return UpdateResult(home_head=head, waited=True,
                            message=f"up to date: the target already holds {head[:8]}; nothing written")
    # #148: a target at the home's head with every core file as the last update left it makes the head-sha call and
    # nothing else (no tree, manifest, file, post-copy step). A core file changed here is the full read's to judge.
    if settings.get("core_version") == head and not _core_paths_dirty(root):
        return UpdateResult(home_head=head,
                            message=f"up to date: the target already holds {head[:8]}; nothing written")
    head, plan = _read_and_plan(root, settings, reader, log, head)
    if plan.touched:  # a current target's own uncommitted edit is not an update's to refuse
        _refuse_dirty(root, plan)
    return _apply_logged(root, head, plan, True, step_runner, settings, log, guarded=True,
                         monitor_runner=monitor_runner)


def _read_and_plan(root: Path, settings: dict, reader: HomeReader, log: _UpdateLog,
                   head: str | None = None) -> tuple[str, _Plan]:
    """The reads of a core update and its plan: the home head (unless the caller read it), manifest, collision
    check, every file's bytes, and the tests folder only when a test copy is a candidate."""

    if head is None:
        head = reader.head_sha()
        log.name(head)
    # #148: the home's tree once (head + tree + manifest = 3 calls), then a `contents` call only for a file whose
    # blob the target does not already hold: k changed files cost k + 3 calls. A truncated tree is None: every file
    # is read as before. `remote` keeps the home's bytes per entry (the target's own, equal, bytes for a file it
    # holds), so `_plan_update` sees an unchanged file exactly as when it was fetched.
    tree = reader.tree(head)
    files = _HomeFiles(root, reader, head, tree)
    new_manifest = _manifest_lines(files.get(MANIFEST_ENTRY))
    old_manifest_path = root / MANIFEST_ENTRY
    old_manifest = read_manifest(old_manifest_path) if old_manifest_path.is_file() else []

    # a collision with the target's own local modules is a refusal, never a shadow (#53); nothing is written yet
    collisions = _local_module_collisions(root, settings, new_manifest, reader, head, read_file=files.get)
    if collisions:
        raise CoreUpdateError("core update refused: " + "; ".join(collisions))

    remote = {entry: files.get(entry) for entry in new_manifest if entry not in TARGET_OWNED_ENTRIES}

    names: list[set[str]] = []  # the home's tests folder, only when a test copy is a candidate

    def home_tests() -> set[str]:
        if not names:  # from the tree already read: no call; a truncated tree reads the folder
            names.append(reader.test_names(head) if tree is None else
                         {path[len("tests/"):] for path in tree
                          if path.startswith("tests/") and "/" not in path[len("tests/"):]})
        return names[0]

    plan = _plan_update(root, settings, head, remote, new_manifest, old_manifest, home_tests)
    plan.home_count = reader.commit_count(head)
    return head, plan


def _apply_logged(root: Path, head: str, plan: _Plan, committable: bool, step_runner,
                  settings: dict, log: _UpdateLog, *, guarded: bool = False, monitor_runner=None) -> UpdateResult:
    """`_apply_update` with this update's log lines (#49): ``started`` before the first write and ``taken <sha8>``
    after the commit (the commit this update landed; the home head on a target with no commit). A plan with
    nothing to write runs as before and logs nothing."""

    if not plan.touched:
        return _apply_update(root, head, plan, committable, step_runner, settings, guarded=guarded,
                             monitor_runner=monitor_runner)
    log.start()
    result = _apply_update(root, head, plan, committable, step_runner, settings, guarded=guarded,
                           monitor_runner=monitor_runner)
    copies = [entry for entry in plan.copies if entry in result.removed]
    if copies:
        log.line(f"removed test copies: {', '.join(copies)}")
    landed = (_head_sha(root) if committable else None) or head
    log.line(f"taken {landed[:8]}")
    return result


def _apply_update(root: Path, head: str, plan: _Plan, committable: bool, step_runner,
                  settings: dict | None = None, *, guarded: bool = False, monitor_runner=None) -> UpdateResult:
    """Phases 2 and 3 of a core update: the writes, then the one commit and push. On a committable target
    the caller holds the merge-then-checkout lock for all of it (``guarded``): the snapshot of what is about to be
    written goes to ``var/agentops/core_update_snapshot.json`` first, and a ``finally`` puts every path and
    ``core_version`` back unless the commit landed (#49). A process killed past the ``finally`` leaves the file for
    the next run (``_recover_leftover``)."""

    touched = set(plan.touched)

    # what every path phase 2 or the steps may write held before (None: absent); on a committable
    # target the preflight above made that HEAD's content. A failed step puts it all back (#352)
    before: dict[str, bytes | None] = {}
    for entry in touched | {SETTINGS_JSON_ENTRY, CODING_STANDARDS_ENTRY, PATTERNS_ENTRY, MONITOR_PROJECT_ENTRY}:
        path = root / entry
        before[entry] = path.read_bytes() if path.is_file() else None

    if not guarded:
        return _write_and_commit(root, head, plan, committable, step_runner, before, monitor_runner)

    snap = _write_snapshot(root, head, before, settings if settings is not None else load_settings(root))
    landed = False
    try:
        result = _write_and_commit(root, head, plan, committable, step_runner, before, monitor_runner)
        landed = True
        return result
    finally:
        try:
            if not landed:
                _restore_snapshot(root, snap)
            (root / SNAPSHOT_ENTRY).unlink(missing_ok=True)
        except OSError:
            pass  # the file stays: the next run restores from it


def _write_and_commit(root: Path, head: str, plan: _Plan, committable: bool, step_runner,
                      before: dict[str, bytes | None], monitor_runner=None) -> UpdateResult:
    pending_content, dropped, pending_removed = plan.pending_content, plan.dropped, plan.pending_removed
    touched = set(plan.touched)

    # -- phase 2: write, exactly as planned -----------------------------------------------------

    changed: list[str] = []
    for entry, content in pending_content.items():
        dest = root / entry
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        changed.append(entry)

    removed: list[str] = []
    for entry in dropped:
        gone = entry not in pending_removed
        if not gone:
            try:
                (root / entry).unlink()
            except FileNotFoundError:
                gone = True
        removed.append(entry)
        if gone:
            # `git add` of a path git has never known fails: commit it only when tracked (its deletion)
            if committable and _git(root, "ls-files", "--error-unmatch", "--", entry).returncode == 0:
                touched.add(entry)
            else:
                touched.discard(entry)

    # the post-copy steps come from the UPDATED code (#316); their paths join the one commit
    try:
        step_paths = (step_runner or _subprocess_step_runner)(root)
    except CoreUpdateRefused:  # a step turned the target's own state down (#172): a refusal, not a failure
        _restore(root, before)
        raise
    except Exception as exc:
        _restore(root, before)
        raise CoreUpdateError(f"core update failed in its post-copy steps; the copy is undone, "
                              f"core_version unchanged: {exc}") from exc
    for path in step_paths:
        touched.add(path)
        if path not in changed:
            changed.append(path)
    hooks_registered, hooks_total, _, _ = _preview_hooks_template(root, {})

    _write_core_version(root, head, plan.home_count)

    # -- phase 3: one commit of exactly `touched`, pushed under the lock the caller holds (issue #284) --

    if committable and touched:
        _commit_and_push(root, touched, head, before)

    # #149: a changed monitor file installs that target's monitor, after the commit; a failure is one line, never a raise
    monitor_line = monitor_install.install_after(changed + removed, root, runner=monitor_runner)

    if not changed and not removed and not touched:
        message = f"up to date: the target already holds {head[:8]}; nothing written"
    else:
        message = (f"updated: {len(changed)} file(s) changed, {len(removed)} removed, "
                   f"{hooks_registered}/{hooks_total} hook(s) registered; core_version -> {head[:8]}")
    copies = [entry for entry in removed if entry in plan.copies]
    if copies:
        message += f"; removed test copies: {', '.join(copies)}"
    if plan.kept:
        message += f"; kept (no home copy): {', '.join(plan.kept)}"
    if monitor_line is not None:
        message += f"; {monitor_line}"
    return UpdateResult(changed=changed, removed=removed, home_head=head, kept=list(plan.kept),
                         hooks_registered=hooks_registered, hooks_total=hooks_total, message=message)


# -- CLI ----------------------------------------------------------------------------------------

def add_status_update_subparsers(core_sub) -> None:
    core_sub.add_parser("status", help="how many core changes wait at the home (issue #153)")
    update = core_sub.add_parser("update", help="rewrite the manifest's files to the home's version (issue #153)")
    update.add_argument("--wait", type=float, default=None, metavar="SECONDS",
                        help="give up with `merge running` when the target's merge lock stays taken this long "
                             "(default: wait for the holder)")
    core_sub.add_parser("update-steps", help="internal: the post-copy steps of `core update` (#316)")


def run_status_command(args) -> int:
    try:
        result = run_core_status(ROOT)
    except CoreUpdateError as exc:
        print(str(exc))
        return 1
    print(result.message)
    return 1 if result.changes > 0 else 0


def run_update_steps_command(args) -> int:
    try:
        written = run_update_steps(ROOT)
    except CoreUpdateRefused as exc:  # the runner of the old process maps this code back to a refusal
        print(exc.reason)
        return STEP_REFUSED_RC
    print(json.dumps(written))
    return 0


def run_update_command(args) -> int:
    try:
        result = run_core_update(ROOT, lock_wait_s=getattr(args, "wait", None))
    except CoreUpdateError as exc:
        print(str(exc))
        return 1
    print(result.message)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="core_update")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    update = sub.add_parser("update")
    update.add_argument("--wait", type=float, default=None, metavar="SECONDS")
    args = ap.parse_args(argv)
    if args.command == "status":
        return run_status_command(args)
    if args.command == "update":
        return run_update_command(args)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
