"""`target add` (issue #31): deploy the orchestration core into a target in one command, 7 steps.

Run at the home. Each step prints one line `step N <name>: <result>`; the first failing step prints its
line (`stopped: <reason>`), names the route, and the command exits 1 with nothing later touched. A run that
gets through prints the owner's fixed list of three items.

The port's own functions do the work (`port.check_machine_ready`, `port.plan_port`, `port.run_port`); a
target's `core status` is read in-process, its `dispatch --ensure` runs in the target's own CLI.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import port
from .core_update import run_core_status
from .util import no_window_flags

ROUTE = "a stop here is a core issue, written with `core issue`, blocking the deployment ticket"
OWNER_LIST = (
    "run `/setup-matt-pocock-skills` once in the chat \"{title}\" there",
    "set that chat to the brain's model of the roles table",
    "answer the brain's areas table there",
)
CLASH_ROUTE = "a clash is ORCH's to report: the deployment ticket goes needs-info to the brain, no --replace"
#: the port's own text for a clash of manifest files already at the target (`port.plan_port`).
CLASH_PREFIX = "the target already holds these manifest files"
_GITHUB_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_LIVE_RE = re.compile(r"^(live|started) pid (\d+)(?: commit ([0-9a-fA-F]+))?$")


class _Stop(Exception):
    """A step's failure: the reason printed on that step's line, and the route that line names."""

    def __init__(self, reason: str, route: str = ROUTE):
        super().__init__(reason)
        self.route = route


def github_repo(url: str) -> str | None:
    """``owner/name`` of a GitHub remote URL (https or ssh), or None when the URL names no GitHub repo."""
    match = _GITHUB_RE.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _line(n: int, name: str, result: str) -> str:
    return f"step {n} {name}: {result}"


def _stopped(n: int, name: str, reason: str, route: str = ROUTE) -> str:
    return _line(n, name, f"stopped: {reason}; {route}")


def _git(folder: Path, args: list[str], git_runner=None) -> subprocess.CompletedProcess:
    runner = git_runner or (lambda a: subprocess.run(
        ["git", "-C", str(folder), *a], capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=no_window_flags()))
    return runner(args)


def step_target_repo(folder: Path, git_runner=None) -> tuple[str, str]:
    """Step 1: a git repo whose origin is a GitHub `owner/name`. An uncommitted tree is reported, not committed."""
    if not (folder / ".git").exists():
        raise _Stop(f"{folder} is not a git repo (no .git)")
    origin = _git(folder, ["remote", "get-url", "origin"], git_runner)
    if origin.returncode != 0:
        raise _Stop(f"{folder} has no origin remote")
    repo = github_repo(origin.stdout)
    if repo is None:
        raise _Stop(f"{folder}'s origin is not a GitHub repo: {origin.stdout.strip()[:120]}")
    status = _git(folder, ["status", "--porcelain"], git_runner)
    dirty = len([row for row in status.stdout.splitlines() if row.strip()]) if status.returncode == 0 else 0
    tree = "clean" if dirty == 0 else f"{dirty} uncommitted paths reported, not committed"
    return repo, f"{repo}, origin is GitHub; tree {tree}"


def _matching_bracket(text: str, open_at: int) -> int:
    """Index of the bracket closing the one at `open_at`; brackets inside JSON strings do not count."""
    depth = 0
    in_string = False
    i = open_at
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Stop("the home's config has an unclosed targets array")


def insert_target_entry(text: str, repo: str, folder) -> str | None:
    """`text` (the home's config) with {repo, folder} appended to its `targets`; None when `repo` is already listed.
    Only the added lines change, plus the comma that follows the previous last entry; the file's line endings and
    indentation are kept."""
    targets = json.loads(text).get("targets")
    if not isinstance(targets, list):
        raise _Stop("the home's config has no targets list")
    if any(isinstance(entry, dict) and entry.get("repo") == repo for entry in targets):
        return None
    key = re.search(r'"targets"\s*:\s*\[', text)
    if key is None:
        raise _Stop("the home's config has no targets array to append to")
    nl = "\r\n" if "\r\n" in text else "\n"
    key_indent = re.match(r"[ \t]*", text[text.rfind("\n", 0, key.start()) + 1:key.start()]).group(0)
    elem = key_indent + "  "
    block = [elem + "{",
             elem + "  " + f'"repo": {json.dumps(repo)},',
             elem + "  " + f'"folder": {json.dumps(str(folder))}',
             elem + "}"]
    open_at = key.end() - 1
    close_at = _matching_bracket(text, open_at)
    if text[open_at + 1:close_at].strip() == "":
        return text[:open_at + 1] + nl + nl.join(block) + nl + key_indent + text[close_at:]
    last = close_at - 1
    while text[last] in " \t\r\n":
        last -= 1
    return text[:last + 1] + "," + nl + nl.join(block) + text[last + 1:]


def _detached_commit(home_root: Path, message: str, rel: str) -> subprocess.CompletedProcess:
    """`agentops commit --detach` in the home: the same commit as `agentops commit`, run as its own process."""
    return subprocess.run([sys.executable, "-m", "tools.agentops", "commit", "-m", message, rel, "--detach"],
                          cwd=str(home_root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=no_window_flags())


def _home_status(home_root: Path) -> subprocess.CompletedProcess:
    """`git status --porcelain` of the home's config file alone."""
    return subprocess.run(["git", "-C", str(home_root), "status", "--porcelain", "--", "config/orchestration.json"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=no_window_flags())


def add_target_entry(home_root: Path, repo: str, folder, commit_runner=None, home_status_runner=None) -> str:
    """Step 5: append {repo, folder} to the home's targets, then queue the commit of that file through
    `agentops commit --detach`. The folder is stored resolved, in forward slashes. Refuses when the file
    already has uncommitted changes (the commit would take them). A failed launch puts the file back."""
    config = Path(home_root) / "config" / "orchestration.json"
    original = config.read_bytes()
    stored = Path(folder).resolve().as_posix()
    updated = insert_target_entry(original.decode("utf-8"), repo, stored)
    if updated is None:
        return "already listed"
    status = (home_status_runner or _home_status)(Path(home_root))
    if status.returncode != 0:
        raise _Stop(f"git status failed in the home: {(status.stderr or '').strip()[:200]}")
    if status.stdout.strip():
        raise _Stop(f"the home's config/orchestration.json has uncommitted changes, the commit would take them: "
                    f"{status.stdout.strip()[:120]}")
    config.write_bytes(updated.encode("utf-8"))
    result = (commit_runner or _detached_commit)(Path(home_root), f"target add: {repo}", "config/orchestration.json")
    if result.returncode != 0:
        config.write_bytes(original)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise _Stop(f"agentops commit did not launch, the file is put back: {output[:200]}")
    match = re.search(r"result (.+?) pid \d+", result.stdout or "")
    return f"queued (result {match.group(1) if match else '(no result path printed)'})"


def run_target_add(folder, brain_title: str, *, update_rule: str = "word", dry_run: bool = False,
                   home_root: Path = port.HOME_ROOT, settings_path: Path,
                   plugin_cache: Path = port.PLUGIN_CACHE_ROOT, gh_runner=None, git_runner=None,
                   status_reader=None, dispatch_runner=None, commit_runner=None, home_status_runner=None,
                   echo: bool = False) -> tuple[int, list[str]]:
    """The 7 steps. Returns (exit code, every printed line); `echo` prints each line as it is produced."""
    folder = Path(folder)
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if echo:
            print(line)

    names = {1: "target repo", 2: "machine ready", 3: "port dry run", 4: "port",
             5: "target-entry", 6: "core status", 7: "dispatch"}
    port_kw = dict(home_root=home_root, settings_path=settings_path, plugin_cache=plugin_cache,
                   gh_runner=gh_runner, git_runner=git_runner)
    current = 1
    try:
        repo, result = step_target_repo(folder, git_runner)
        say(_line(1, names[1], result))

        current = 2
        plugin = port.check_machine_ready(settings_path, gh_runner=gh_runner, plugin_cache=plugin_cache)
        say(_line(2, names[2], f"plugin {plugin} enabled, gh ok"))

        current = 3
        try:
            files, labels = port.plan_port(folder, repo, **port_kw)
        except port.PortError as exc:
            if str(exc).startswith(CLASH_PREFIX):
                raise _Stop(str(exc), route=CLASH_ROUTE) from exc
            raise
        say(_line(3, names[3], f"{len(files)} files and {len(labels)} labels would be written; nothing written"))

        current = 4
        if dry_run:
            say(_line(4, names[4], "skipped (dry run); " + port.monitor_dry_run_line(folder, repo)))
        else:
            report = port.run_port(folder, repo, brain_title=brain_title, update_rule=update_rule, **port_kw)
            say(_line(4, names[4], f"copied {len(report.copied)} files, wrote {len(report.own_files_written)} "
                                   f"own files, created {len(report.labels_created)} labels"))

        current = 5
        if dry_run:
            say(_line(5, names[5], f"would add {{repo: {repo}, folder: {folder}}} to the home's targets"))
        else:
            say(_line(5, names[5], add_target_entry(home_root, repo, folder, commit_runner, home_status_runner)))

        current = 6
        if dry_run:
            say(_line(6, names[6], "skipped (dry run)"))
        else:
            status = (status_reader or run_core_status)(folder)
            say(_line(6, names[6], f"{status.changes} core changes waiting"))

        current = 7
        if dry_run:
            say(_line(7, names[7], "skipped (dry run)"))
        else:
            say(_line(7, names[7], _ensure_in_target(folder, dispatch_runner)))
    except _Stop as exc:
        say(_stopped(current, names[current], str(exc), exc.route))
        return 1, lines
    except Exception as exc:  # noqa: BLE001 -- any step's failure is one stop line, never a traceback
        reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        say(_stopped(current, names[current], reason))
        return 1, lines

    lines.append("owner's list:")
    for number, item in enumerate(OWNER_LIST, start=1):
        lines.append(f"  {number}. {item.format(title=brain_title)}")
    if echo:
        for line in lines[-4:]:
            print(line)
    return 0, lines


def _ensure_in_target(folder: Path, dispatch_runner=None) -> str:
    """Step 7: the target's own `dispatch --ensure`, run in the target folder; its `live pid ...` line is the result."""
    if dispatch_runner is None:
        def dispatch_runner(cwd: Path):
            return subprocess.run([sys.executable, "-m", "tools.agentops", "dispatch", "--ensure"], cwd=str(cwd),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  creationflags=no_window_flags())
    result = dispatch_runner(folder)
    if result.returncode != 0:
        raise _Stop(f"dispatch --ensure failed: {(result.stderr or '').strip()[:200]}")
    tail = [row.strip() for row in (result.stdout or "").splitlines() if row.strip()]
    last = tail[-1] if tail else "(no output)"
    match = _LIVE_RE.match(last)
    if match is None:
        raise _Stop(f"dispatch --ensure's last line is not `live|started pid <n> [commit <sha>]`: {last}")
    word, pid, commit = match.groups()
    return f"{word} pid {pid} commit {commit[:8]}" if commit else f"{word} pid {pid}"


# -- CLI ------------------------------------------------------------------------------------------

def add_target_subparser(sub) -> None:
    target = sub.add_parser("target", help="deploy the orchestration core into a target (issue #31)")
    target_sub = target.add_subparsers(dest="target_command", required=True)
    add = target_sub.add_parser("add", help="one command: check, port, list the target, start its dispatcher")
    add.add_argument("--folder", required=True, type=Path)
    add.add_argument("--brain-title", required=True)
    add.add_argument("--update-rule", choices=["auto", "word"], default="word")
    add.add_argument("--dry-run", action="store_true",
                     help="run the checks and the port's dry run; write nothing, name the entry it would add")


def run_target_command(args, *, settings_path: Path) -> int:
    rc, _lines = run_target_add(args.folder, args.brain_title, update_rule=args.update_rule,
                                dry_run=args.dry_run, settings_path=settings_path, echo=True)
    return rc
