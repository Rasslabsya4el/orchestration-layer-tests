"""The manifest-edit guard (issue 1116): in a target, a merge or a commit may not change
a core file -- a manifest path -- unless the change is a core update's own commit (``Core update: <sha8>``).

A target is a repo whose ``update_rule`` is not ``home`` (``core_check._is_target``); at the home and
in any repo that is not a target nothing is refused. ``port.TARGET_OWNED_ENTRIES`` are the target's
own files and are never refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from .core_check import _is_target, read_manifest
from .port import TARGET_OWNED_ENTRIES

CORE_UPDATE_PREFIX = "Core update: "
#: the manifest a target carries of its own (the home's copy is tools/agentops/core_manifest.txt too)
MANIFEST_IN_ROOT = Path("tools") / "agentops" / "core_manifest.txt"


def manifest_edits(root: Path, changed_paths: Iterable[str],
                   commits: Iterable[tuple[str, Sequence[str]]]) -> list[str]:
    """The manifest paths among ``changed_paths`` that a commit other than a core update's touches.

    ``changed_paths`` is the change's own file list (a branch's diff against main, or a commit's
    paths); ``commits`` is one ``(subject, paths)`` per commit of the change. Empty at the home and
    for a repo that is not a target, and for a manifest without the file."""
    if not _is_target(root):
        return []
    manifest_file = root / MANIFEST_IN_ROOT
    if not manifest_file.exists():
        return []
    guarded = set(read_manifest(manifest_file)) - TARGET_OWNED_ENTRIES
    edited: set[str] = set()
    for subject, paths in commits:
        if subject.startswith(CORE_UPDATE_PREFIX):
            continue
        edited.update(path for path in paths if path in guarded)
    return sorted(path for path in set(changed_paths) if path in edited)


def refusal_message(files: Sequence[str]) -> str:
    """The one line a refused merge or commit prints (the files named, then the way out)."""
    return (f"core file(s) may change in a target only through a core update: {', '.join(files)} -- "
            "write a core issue at the home (`python -m tools.agentops core issue ...`)")
