"""Issue #349: the run tree of one replay of the brain's two roles.

``make_run_tree`` puts a sealed standalone clone of the case's repo at the moment's commit (#357: its history ends
at that commit, it holds no ref but HEAD, no remote and no newer object) and lays one arm's overlay on it: the old role (the role files, hooks and ``tools/agentops`` of ``OLD_ROLE_COMMIT``, the per-reply check and the
Stop hook on) or the new one (the draft's three files, the per-reply check and the Stop hook off). The
repo's own part (``CLAUDE.md``, ``docs/STATE.md``, everything outside the overlay) stays as at the moment.
Nothing else is written into the tree, except the records dir's files (#357), and no tree is ever removed here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

#: the role in force today: the commit whose role files and hooks the old arm (and the hooks of the new) take.
OLD_ROLE_COMMIT = "ffce853"

#: the one line the new arm adds to ``tools/hooks/brain_reset_nudge.py``: the first statement of
#: ``_reply_check_text``, so the check text is never built, no ``reply-check`` log line is written and the
#: other three rules (which sit in ``handle_user_prompt_submit`` / ``handle_post_tool_use``) run as before.
REPLY_CHECK_OFF_LINE = "    return None  # replay #349: per-reply check off"

#: the hooks import ``tools.agentops`` of their own commit, so both directories come from the role commit.
OVERLAID_DIRS = ("tools/hooks", "tools/agentops")

STOP_SCRIPT = "state_claim_check.py"
_NUDGE_REL = "tools/hooks/brain_reset_nudge.py"
_SETTINGS_REL = ".claude/settings.json"
_REPLY_CHECK_DEF = re.compile(r'(def _reply_check_text\([^\n]*\n[ \t]+""".*?"""(\r?\n))', re.DOTALL)

GIT_TIMEOUT_SECONDS = 60
SEAL_TIMEOUT_SECONDS = 300  # the clone and the gc of one tree

#: the two lines of ``_inject`` in ``tools/hooks/brain_state_inject.py`` that reach origin: the ``git fetch origin``
#: (whose success makes the hook read ``origin/<default>:docs/STATE.md``, today's hand-off) and the note a failed
#: fetch adds. The edit makes the fetch never happen and drops the note, so both arms inject the working-copy
#: ``docs/STATE.md`` of the moment and nothing else.
_FETCH_LINE = 'fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)'
_FETCH_OFF = "fetch = None  # replay #349: never fetch, never read origin refs; the hand-off is the moment's docs/STATE.md"
_FETCH_NOTE_LINE = "notes.append(FETCH_FAILED_NOTE)"
_FETCH_NOTE_OFF = "pass  # replay #349: no fetch note"
_STATE_HOOK_REL = "tools/hooks/brain_state_inject.py"


def _git_bytes(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout, input=stdin)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def _write(tree: Path, rel: str, data: bytes) -> None:
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _seal_tree(source: Path, tree: Path, commit: str) -> None:
    """``tree`` becomes a standalone repository of ``source`` whose history ends at ``commit`` (#357). A worktree would
    share every ref with the real repo, and a run's ``git fetch origin`` / ``git show origin/main:...`` would read
    today's state. So: a local clone (hardlinked objects, no alternates), the commit checked out detached, every ref
    but HEAD deleted, the remote removed, the reflogs expired and a gc that prunes every object the commit does not
    reach. The clone's own ``user.name`` / ``user.email`` are set when no git config gives them, so a run can commit."""
    subprocess.run(["git", "clone", "--local", "--no-checkout", "-q", str(source), str(tree)], capture_output=True,
                   timeout=SEAL_TIMEOUT_SECONDS, check=True)
    _git_bytes(tree, "checkout", "-q", "--detach", commit, timeout=SEAL_TIMEOUT_SECONDS)
    refs = _git_bytes(tree, "for-each-ref", "--format=%(refname)").decode("utf-8").split()
    if refs:  # --no-deref: `refs/remotes/origin/HEAD` is a symref, deleting it must not delete what it points to
        script = "".join(f"option no-deref\ndelete {ref}\n" for ref in refs).encode("utf-8")
        _git_bytes(tree, "update-ref", "--stdin", stdin=script)
    _git_bytes(tree, "remote", "remove", "origin")
    _git_bytes(tree, "reflog", "expire", "--expire=now", "--all")
    _git_bytes(tree, "gc", "--prune=now", "-q", timeout=SEAL_TIMEOUT_SECONDS)
    for key, fallback in (("user.name", "replay"), ("user.email", "replay@localhost")):
        if subprocess.run(["git", "-C", str(tree), "config", key], capture_output=True, timeout=GIT_TIMEOUT_SECONDS
                          ).returncode != 0:
            _git_bytes(tree, "config", "--local", key, fallback)


def _record_rels(folder: Path) -> list[str]:
    """The files under ``folder`` as sorted posix paths relative to it; none when the folder is absent (#357)."""
    if not folder.is_dir():
        return []
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def _checked_rel(rel: str) -> str:
    """``rel`` as a path under a run tree (#357). Raises ValueError for an empty, absolute or drive-letter path, or one
    with a ``..`` part: such a path would leave the tree."""
    path = PureWindowsPath(rel)
    if not rel or path.drive or path.root or ".." in path.parts:
        raise ValueError(f"records path {rel!r} would leave the run tree")
    return path.as_posix()


def _copy_records(tree: Path, records_dir: Path, case_key: str) -> None:
    """Copy ``<records_dir>/<case_key>/**`` byte-for-byte into ``tree`` at the same relative paths (#357). Every path is
    checked before the first write, so a refused path writes nothing. A missing folder copies nothing."""
    folder = Path(records_dir) / case_key
    rels = [_checked_rel(rel) for rel in _record_rels(folder)]
    for rel in rels:
        _write(tree, rel, (folder / rel).read_bytes())


def _without_stop_check(hooks: dict) -> dict:
    """``hooks`` with every command naming the Stop script removed; an entry or an event left empty goes too."""
    kept: dict = {}
    for event, entries in hooks.items():
        left = []
        for entry in entries:
            commands = [h for h in entry.get("hooks", []) if STOP_SCRIPT not in str(h.get("command", ""))]
            if commands:
                left.append({**entry, "hooks": commands})
        if left:
            kept[event] = left
    return kept


def _indent_of(text: str) -> int:
    """The indent of the first indented line of a JSON file (2 when it has none)."""
    match = re.search(r"^( +)\S", text, re.MULTILINE)
    return len(match.group(1)) if match else 2


def _replace_hooks(tree: Path, role_hooks: dict) -> None:
    """Replace only the top-level ``hooks`` key of the tree's settings, every other key kept in its place and the
    file's indent and trailing newline kept (not byte-identical: JSON is re-dumped)."""
    path = tree / _SETTINGS_REL
    text = path.read_text(encoding="utf-8") if path.exists() else "{}"
    settings = json.loads(text)
    settings["hooks"] = role_hooks
    tail = "\n" if text.endswith("\n") or not path.exists() else ""
    _write(tree, _SETTINGS_REL,
           (json.dumps(settings, indent=_indent_of(text), ensure_ascii=False) + tail).encode("utf-8"))


def _overlay_dir(tree: Path, role_repo: Path, commit: str, rel_dir: str) -> None:
    """``rel_dir`` of the tree becomes exactly the role commit's: overwritten, and files it lacks removed."""
    target = tree / rel_dir
    if target.exists():
        shutil.rmtree(target)
    names = _git_bytes(role_repo, "ls-tree", "-r", "-z", "--name-only", commit, rel_dir).decode("utf-8")
    for rel in filter(None, names.split("\0")):
        _write(tree, rel, _git_bytes(role_repo, "cat-file", "blob", f"{commit}:{rel}"))


def _switch_off_reply_check(tree: Path) -> None:
    path = tree / _NUDGE_REL
    text = path.read_bytes().decode("utf-8")
    match = _REPLY_CHECK_DEF.search(text)
    if match is None:
        raise RuntimeError(f"reply-check: no `_reply_check_text` with a docstring in {path}")
    eol = match.group(2)
    path.write_bytes((text[:match.end()] + REPLY_CHECK_OFF_LINE + eol + text[match.end():]).encode("utf-8"))


def _switch_off_state_fetch(tree: Path) -> None:
    """Both arms: the state hook reads the tree's working-copy ``docs/STATE.md``, never origin (see ``_FETCH_LINE``)."""
    path = tree / _STATE_HOOK_REL
    text = path.read_bytes().decode("utf-8")
    if _FETCH_LINE not in text or _FETCH_NOTE_LINE not in text:
        raise RuntimeError(f"state hook: no fetch line or fetch note in {path}")
    path.write_bytes(text.replace(_FETCH_LINE, _FETCH_OFF).replace(_FETCH_NOTE_LINE, _FETCH_NOTE_OFF).encode("utf-8"))


def make_run_tree(case: dict, arm: str, n: int, base_dir: Path, role_repo: Path,
                  draft_dir: Path | None, records_dir: Path | None = None) -> Path:
    """Create ``<base_dir>/<key>-<arm>-<n>``, a sealed standalone clone of ``case["repo"]`` at ``case["commit"]`` (see
    ``_seal_tree``), lay the arm's overlay on it and return its path. ``arm`` is ``"old"`` or ``"new"``; the new arm needs
    ``draft_dir`` (``brain.md``, ``brain-reference.md``, ``agent.md``). Raises on a bad arm or a failed git call.
    With ``records_dir`` (#357), every file of ``<records_dir>/<key>/`` is copied into the tree after the overlay, at
    the same relative path; a missing folder copies nothing, a path that would leave the tree raises ValueError."""
    if arm not in ("old", "new"):
        raise ValueError(f"arm must be 'old' or 'new', got {arm!r}")
    if arm == "new" and draft_dir is None:
        raise ValueError("the new arm needs draft_dir")
    role_repo = Path(role_repo)
    commit = _git_bytes(role_repo, "rev-parse", "--verify", f"{OLD_ROLE_COMMIT}^{{commit}}").decode().strip()
    tree = Path(base_dir) / f"{case['key']}-{arm}-{n}"
    _seal_tree(Path(case["repo"]), tree, case["commit"])

    if arm == "old":
        for rel in ("docs/agents/brain.md", ".claude/agents/brain.md"):
            _write(tree, rel, _git_bytes(role_repo, "cat-file", "blob", f"{commit}:{rel}"))
    else:
        draft = Path(draft_dir)
        for source, rel in (("brain.md", "docs/agents/brain.md"),
                            ("brain-reference.md", "docs/agents/brain-reference.md"),
                            ("agent.md", ".claude/agents/brain.md")):
            _write(tree, rel, (draft / source).read_bytes())

    for rel_dir in OVERLAID_DIRS:
        _overlay_dir(tree, role_repo, commit, rel_dir)
    role_settings = json.loads(_git_bytes(role_repo, "cat-file", "blob", f"{commit}:{_SETTINGS_REL}"))
    role_hooks = role_settings["hooks"]
    if arm == "new":
        role_hooks = _without_stop_check(role_hooks)
        _switch_off_reply_check(tree)
    _switch_off_state_fetch(tree)
    _replace_hooks(tree, role_hooks)
    if records_dir is not None:
        _copy_records(tree, Path(records_dir), case["key"])
    return tree
