"""#231 unit 2 (D119): send a repo's snapshot to its test copy, or refuse.

``send(root, branch, remote)`` copies ``snapshot_files(root)`` into a fresh temporary tree FIRST, then runs the
unit-1 scan over that copy (the bytes that get committed, paths included). Any hit, or a machine file that does not
give the ``product`` or ``owner-name`` pattern (a public push cannot be recalled, so an unchecked kind is no clean
check), pushes nothing, prints ``test copy refused: <N> hits, suite on the PC`` and returns a refusal. Also refused,
with a line saying why: a tracked symlink, a ``git add`` that would change a file's bytes, a git call that times out.
A clean copy becomes ONE commit with no parent and is pushed to ``branch`` of ``remote``. The working repo's index,
branches and objects are never touched.

Every git call runs with no ``GIT_*`` variable of the process and with a fixed author, committer and date
(``test-copy <test-copy@localhost>``, 2000-01-01), so no name, address or real time of the owner reaches the public
copy. The push overwrites ``branch`` (``+HEAD:refs/heads/<branch>``): a send is always a parentless commit, so a
second send of the same branch name can only be a forced update, and it is forced for that one ref only. ``main`` and
``master`` are never overwritten.

Stdlib only, and no import of anything under ``src/``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from tools.agentops.core_settings import DEFAULTS
from tools.agentops.test_copy_check import (CheckError, DEFAULT_ROOT, SETTINGS_FILE, git_env, load_patterns, scan,
                                            snapshot_bytes, snapshot_files)
from tools.agentops.util import no_window_flags

#: the most hit lines a refusal prints; the rest are counted only.
MAX_LINES = 50
REMOTE_KEY = "test_copy_remote"
PROTECTED_BRANCHES = ("main", "master")
SYMLINK_MODE = "120000"
GIT_SECONDS = 60
PUSH_SECONDS = 300
#: fixed on every git call: the author, the committer and their dates.
FIXED_ENV = {
    "GIT_AUTHOR_NAME": "test-copy", "GIT_AUTHOR_EMAIL": "test-copy@localhost",
    "GIT_COMMITTER_NAME": "test-copy", "GIT_COMMITTER_EMAIL": "test-copy@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    "GIT_TERMINAL_PROMPT": "0",
}
#: no hook of the machine's git config runs, and git changes no byte on add.
GIT_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
              "-c", "core.safecrlf=false")


class SendResult(NamedTuple):
    sent: bool
    sha: str | None  # the pushed commit, None on a refusal
    reason: str | None = None  # #251: why a refusal refused (hits as ``file:line kind``); None when sent


#: the first hits a refusal's reason names; the rest are counted (``+K more``).
REASON_HITS = 5


class GitTimeout(CheckError):
    """A git call ran past its bound."""


def _setting_remote(root: Path) -> str:
    path = root / SETTINGS_FILE
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckError(f"{SETTINGS_FILE} does not parse ({type(exc).__name__})") from exc
    given = data.get(REMOTE_KEY, DEFAULTS[REMOTE_KEY]) if isinstance(data, dict) else DEFAULTS[REMOTE_KEY]
    if not isinstance(given, str):
        raise CheckError(f"{REMOTE_KEY} in {SETTINGS_FILE} must be a string")
    return given


def _git(cwd: Path, *args: str, timeout: int = GIT_SECONDS, stdin: str | None = None) -> str:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *GIT_CONFIG, *args], capture_output=True, check=True,
                              input=stdin, encoding="utf-8", errors="replace", creationflags=no_window_flags(),
                              env={**git_env(), **FIXED_ENV}, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {args[0]} ran past {timeout} s") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", "") or "").strip().splitlines()
        raise CheckError(f"git {args[0]} failed: {detail[-1] if detail else type(exc).__name__}") from exc
    return done.stdout.strip("\n")


def _refuse(message: str, reason: str) -> SendResult:
    print(message)
    return SendResult(False, None, reason)


def _hits_reason(hits: list, unchecked: list) -> str:
    """#251: the reason of a scan refusal: ``<N> hit(s): file:line kind, ... [+K more]``, then the kinds not checked."""
    parts = []
    if hits:
        shown = ", ".join(f"{hit.file}:{hit.line} {hit.kind}" for hit in hits[:REASON_HITS])
        more = f" +{len(hits) - REASON_HITS} more" if len(hits) > REASON_HITS else ""
        parts.append(f"{len(hits)} {'hit' if len(hits) == 1 else 'hits'}: {shown}{more}")
    if unchecked:
        parts.append("not checked: " + ", ".join(unchecked))
    return "; ".join(parts)


def _index_entries(cwd: Path) -> dict:
    """path -> (mode, blob) of ``git ls-files -s`` in ``cwd``."""
    entries = {}
    for entry in _git(cwd, "ls-files", "-s", "-z").split("\0"):
        meta, _, path = entry.partition("\t")
        if path:
            mode, blob, _stage = meta.split(" ")
            entries[path] = (mode, blob)
    return entries


def _tracked_symlink(root: Path, files: list[str]) -> str | None:
    """The first snapshot path that is a symlink in the index, else None."""
    entries = _index_entries(root)
    return next((rel for rel in files if entries.get(rel, ("", ""))[0] == SYMLINK_MODE), None)


def _content_unchanged(work: Path, copied: list[str]) -> bool:
    """True when every blob ``git add`` stored equals the file's own bytes (hashed without any filter)."""
    staged = _index_entries(work)
    hashed = _git(work, "hash-object", "--no-filters", "--stdin-paths", stdin="\n".join(copied) + "\n").splitlines()
    return len(hashed) == len(copied) and all(staged.get(rel, ("", ""))[1] == oid for rel, oid in zip(copied, hashed))


def send(root: Path = DEFAULT_ROOT, branch: str = "", remote: str | None = None) -> SendResult:
    """Copy, scan the copy, then push it as one parentless commit to ``branch`` of ``remote`` (default: the repo's
    ``test_copy_remote`` setting). Prints the refusal lines or the pushed sha; raises CheckError when the check
    cannot run or a git call fails."""
    try:
        return _send(root, branch, remote)
    except GitTimeout:
        return _refuse("test copy: git timed out, suite on the PC", "test copy: git timed out")


def _send(root: Path, branch: str, remote: str | None) -> SendResult:
    patterns = load_patterns(root)
    files = snapshot_files(root)
    link = _tracked_symlink(root, files)
    if link:
        return _refuse(f"test copy refused: symlink {link}, suite on the PC", f"test copy refused: symlink {link}")
    with tempfile.TemporaryDirectory(prefix="test-copy-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp) / "copy"
        work.mkdir()
        copied = []
        for rel in files:
            source = root / rel
            if not source.is_file():
                continue  # tracked but deleted in the working tree
            target = work / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot_bytes(root, rel, patterns))
            copied.append(rel)
        hits = scan(work, patterns, copied)
        unchecked = [kind for kind in patterns.not_checked if kind in ("product", "owner-name")]
        if hits or unchecked:
            print(f"test copy refused: {len(hits)} hits, suite on the PC")
            for kind in unchecked:
                print(f"not checked: {kind}")
            for hit in hits[:MAX_LINES]:
                print(f"{hit.file}:{hit.line} {hit.kind}")
            if len(hits) > MAX_LINES:
                print(f"... {len(hits) - MAX_LINES} more hit lines not shown")
            return SendResult(False, None, _hits_reason(hits, unchecked))
        remote = remote or _setting_remote(root)
        if not remote:
            return _refuse("test copy: no remote set, suite on the PC", "test copy: no remote set")
        if branch in PROTECTED_BRANCHES:
            return _refuse(f"test copy: branch {branch} is never overwritten, suite on the PC",
                           f"test copy: branch {branch} is never overwritten")
        if not branch or branch.startswith("-"):
            return _refuse(f"test copy: {branch!r} is not a branch name, suite on the PC",
                           f"test copy: {branch!r} is not a branch name")
        _git(root, "check-ref-format", f"refs/heads/{branch}")
        _git(work, "init", "-q")
        info = work / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* -text -filter\n", encoding="utf-8")
        _git(work, "add", "-A", "--force")
        if not _content_unchanged(work, copied):
            return _refuse("test copy refused: content changed on add", "test copy refused: content changed on add")
        _git(work, "commit", "-q", "--allow-empty", "-m", f"test copy {branch}")
        sha = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "-q", remote, f"+HEAD:refs/heads/{branch}", timeout=PUSH_SECONDS)
    print(f"test copy sent: {sha} {branch}")
    return SendResult(True, sha)
