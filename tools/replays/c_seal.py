"""Issue #376: the seal of a C clone, the sealed standalone clone one strong agent redoes a closed ticket in.

``make_clone`` builds it (the replays' ``_seal_tree`` at the ticket's base commit, on one branch), ``sealed_env`` gives
the session's environment (a ``gh`` that answers nothing, no token, memory off, every real checkout denied) and
``check_sealed`` refuses a clone that could show the session the solution. The check runs before any model call.

    python -m tools.replays.c_seal make  --source <repo> --base <sha> --dest <dir> --forbid <sha> ... --issue <N> ... --state <dir>
    python -m tools.replays.c_seal check --clone <dir>  --base <sha> --forbid <sha> ... --issue <N> ... --state <dir>

Both print ``sealed``, or ``refused: <the failed check>`` with exit code 1. ``check`` writes nothing: it reads the shim
dir ``--state`` that ``make`` (``sealed_env``) filled.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tools.replays.arms import _git_bytes, _seal_tree
from tools.replays.launcher import (HARNESS_ROOT, TOKEN_VARS, _disable_memory, _environment, _NO_WINDOW,
                                    _write_shims, check_gh_shim)

DEFAULT_BRANCH = "c-base"
#: the issue numbers the check asks the shim for by default; a caller adds the clone's own ticket.
DEFAULT_ISSUES = (1,)
#: the case key of ``launcher.CASE_REPOS`` whose repo (this one) the stub is stood in for.
CASE_KEY = "c"
GH_TIMEOUT_SECONDS = 60
#: the ``gh`` the stub reaches for its reads: it answers nothing and fails, so no issue text can come through the shim.
_NULL_GH = "null_gh.cmd" if os.name == "nt" else "null_gh"
_NULL_GH_BODY = "@exit /b 1\r\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n"


class SealError(RuntimeError):
    """A clone is not sealed; the message names the failed check."""


def make_clone(source: Path, base: str, dest: Path, branch: str = DEFAULT_BRANCH) -> Path:
    """A standalone clone of ``source`` at ``base`` in ``dest``: history of ``base`` and its ancestors only, no remote,
    one branch (``branch``), reflogs expired and unreachable objects pruned (``_seal_tree`` does all but the branch).
    Returns ``dest``."""
    dest = Path(dest)
    _seal_tree(Path(source), dest, base)
    _git_bytes(dest, "checkout", "-q", "-b", branch)
    return dest


def sealed_env(clone: Path, state: Path, deny_checkout: Path | None = None) -> dict[str, str]:
    """The environment of a session in ``clone``: ``state`` holds the ``gh`` shim whose real gh answers nothing, the
    token variables are gone (the replays' ``REPLAY_<name>`` copies too), the clone's settings turn memory off and
    deny every real checkout (``deny_checkout``, default this harness' own, and the ``targets`` of
    config/orchestration.json). Writes the shims and the settings; ``env_from_state`` only reads."""
    state = Path(state).absolute()
    state.mkdir(parents=True, exist_ok=True)
    _write_shims(state, SimpleNamespace(harness_root=HARNESS_ROOT))
    null_gh = state / _NULL_GH
    null_gh.write_text(_NULL_GH_BODY, encoding="utf-8", newline="")
    os.chmod(null_gh, 0o755)
    _disable_memory(Path(clone), str(deny_checkout or HARNESS_ROOT), HARNESS_ROOT)
    return env_from_state(clone, state)


def env_from_state(clone: Path, state: Path) -> dict[str, str]:
    """The environment ``sealed_env`` returns, built from the shim dir ``state`` as it stands, writing nothing: a check
    of an existing clone sees the shims that are there, a planted one included."""
    state = Path(state).absolute()
    env = _environment(state, {"moment_utc": "1970-01-01T00:00:00Z"}, CASE_KEY)
    env["REPLAY_REAL_GH"] = str(state / _NULL_GH)
    for name in TOKEN_VARS:
        env.pop(f"REPLAY_{name}", None)
    return env


def check_sealed(clone: Path, base: str, forbidden: list[str], env: dict[str, str],
                 issues: tuple[int, ...] = DEFAULT_ISSUES) -> None:
    """Raises ``SealError`` naming the failed check; returns None only when the clone holds no commit that is not an
    ancestor of ``base`` (in any ref, the reflog or the object store), has no remote, holds none of the ``forbidden``
    commits and its ``gh`` answers nothing for each of ``issues`` (the clone's own ticket among them)."""
    clone = Path(clone)
    try:
        base_sha = _git(clone, "rev-parse", "--verify", f"{base}^{{commit}}")
    except RuntimeError as exc:
        raise SealError(f"base {base} is not a commit of the clone ({exc})") from exc
    ancestors = set(_git(clone, "rev-list", base_sha).split())
    reachable = set(_git(clone, "rev-list", "--all", "--reflog").split())
    stored = {line.split()[1] for line in _git(clone, "cat-file", "--batch-all-objects",
                                              "--batch-check=%(objecttype) %(objectname)").splitlines()
              if line.startswith("commit ")}
    for where, commits in (("reachable from a ref or the reflog", reachable), ("kept in the object store", stored)):
        extra = sorted(commits - ancestors)
        if extra:
            raise SealError(f"{len(extra)} commit(s) {where} are not an ancestor of the base {base_sha[:8]}: "
                            f"{', '.join(sha[:8] for sha in extra[:5])}")
    remotes = _git(clone, "remote").split()
    if remotes:
        raise SealError(f"the clone has a remote: {', '.join(remotes)}")
    for sha in forbidden:
        try:
            _git(clone, "cat-file", "-e", sha)
        except RuntimeError:  # the object is not there (or the name is not one): what a sealed clone must answer
            continue
        raise SealError(f"forbidden commit {sha[:8]} is in the clone")
    _check_gh(clone, env, issues)


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", "replace").strip()


def _check_gh(clone: Path, env: dict[str, str], issues: tuple[int, ...]) -> None:
    """The shim resolves first on the session's PATH (``check_gh_shim``) and ``gh issue view <N>`` returns no output and
    a non-zero code, for each of ``issues``."""
    state = env.get("REPLAY_STATE_DIR")
    if not state:
        raise SealError("gh: the environment names no state dir (REPLAY_STATE_DIR), so no shim is in force")
    try:
        check_gh_shim(env, clone, Path(state))
    except RuntimeError as exc:
        raise SealError(str(exc)) from exc
    gh = shutil.which("gh", path=env.get("PATH", ""))  # not subprocess' lookup: on Windows that uses the parent's PATH
    for number in issues:
        try:
            done = subprocess.run([gh, "issue", "view", str(number)], cwd=clone, env=env, capture_output=True, text=True,
                                  timeout=GH_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise SealError(f"gh issue view {number} timed out ({exc})") from exc
        if done.stdout.strip() or done.returncode == 0:
            raise SealError(f"gh issue view {number} answered (rc {done.returncode}): {done.stdout.strip()[:80]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replays.c_seal", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("make", "check"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--base", required=True)
        cmd.add_argument("--forbid", action="append", default=[])
        cmd.add_argument("--state", required=True, type=Path)
        cmd.add_argument("--issue", action="append", type=int, default=[], help="an issue number the gh shim is asked for")
    make = sub.choices["make"]
    make.add_argument("--source", required=True, type=Path)
    make.add_argument("--dest", required=True, type=Path)
    make.add_argument("--branch", default=DEFAULT_BRANCH)
    sub.choices["check"].add_argument("--clone", required=True, type=Path)
    args = parser.parse_args(argv)
    issues = tuple(args.issue) or DEFAULT_ISSUES
    try:
        if args.command == "make":
            clone = make_clone(args.source, args.base, args.dest, args.branch)
            env = sealed_env(clone, args.state, deny_checkout=args.source)
        else:
            clone = args.clone
            env = env_from_state(clone, args.state)
        check_sealed(clone, args.base, args.forbid, env, issues)
    except SealError as exc:
        print(f"refused: {exc}")
        return 1
    print("sealed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
