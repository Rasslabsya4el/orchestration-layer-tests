"""#100: the owner's real Claude commands folder and the snapshot the session guard compares (conftest.py).
#173: the owner's real mods folder and the recursive snapshot of it.
#210: the real mods journal, and the decision that excuses a change of the real mods folder it explains.

`REAL_HOME` is read at import, before any test changes HOME, so it is the owner's real home."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


REAL_HOME = Path.home()
REAL_COMMANDS = REAL_HOME / ".claude" / "commands"
REAL_MODS = REAL_HOME / ".claude" / "mods"
REAL_JOURNAL = REAL_MODS.parent / "mods-journal.log"


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """Entry name -> (mtime in ns, size in bytes) of every entry of `folder`; empty when the folder is absent."""
    if not folder.is_dir():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for entry in folder.iterdir():
        st = entry.stat()
        out[entry.name] = (st.st_mtime_ns, st.st_size)
    return out


def mod_folder_names(folder: Path) -> set[str]:
    """#173: the names of the top-level folders of `folder` (one per mod); empty when the folder is absent. Their
    content is not compared: the live monitors rewrite their caches under these folders."""
    if not folder.is_dir():
        return set()
    return {entry.name for entry in folder.iterdir() if entry.is_dir()}


def name_changes(before: set[str], after: set[str]) -> dict[str, list[str]]:
    """The folder names added and removed between two `mod_folder_names` sets, each sorted."""
    return {"added": sorted(after - before), "removed": sorted(before - after)}


def unexplained_changes(
        moved: dict[str, list[str]], entries: list[tuple[datetime, str, str]], start: datetime, end: datetime,
) -> tuple[list[str], dict[str, list[str]]]:
    """#210: split the `name_changes` of the real mods folder into the names a journal line explains and the rest.

    A name in `moved["added"]` is excused by an `install` line for it dated inside the session; a name in
    `moved["removed"]` by a `remove` line. The session start is floored to the second (the journal truncates to
    seconds), so a line in the session's first second counts; a line before the start or after the end does not.
    Returns `(excused names, unexplained changes)`: the excused names sorted and unique, the unexplained changes in
    the `name_changes` shape, each sorted."""
    lo = start.replace(microsecond=0)
    inside = [(name, action) for when, name, action in entries if lo <= when <= end]
    excused_added = [n for n in moved["added"] if (n, "install") in inside]
    excused_removed = [n for n in moved["removed"] if (n, "remove") in inside]
    unexplained = {
        "added": [n for n in moved["added"] if n not in excused_added],
        "removed": [n for n in moved["removed"] if n not in excused_removed],
    }
    return sorted(set(excused_added + excused_removed)), unexplained


def note_line(excused: list[str]) -> str | None:
    """#210: the one note line the session guard prints for the excused names; None when there are none."""
    if not excused:
        return None
    return f"mods folder changed by mods install during the run: {', '.join(excused)}"


def changes(before: dict, after: dict) -> dict[str, list[str]]:
    """The names added, removed and changed (mtime or size) between two snapshots, each sorted."""
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(n for n in set(before) & set(after) if before[n] != after[n]),
    }
