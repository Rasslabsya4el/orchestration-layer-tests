"""#210 unit 2: the session guard excuses a change of the owner's real mods folder that the real journal explains.

`_commands_guard.unexplained_changes` is the pure decision the session fixture calls: a folder added or removed
during the run is excused when a journal line for that name, action `install` (added) or `remove` (removed), is
dated inside the session. The session start is floored to the second, because the journal time is truncated to
seconds. A line dated before the start, or after the end, does not excuse. The session fixture itself is not run
here: these tests use datetimes and a journal file in a temp folder, never the owner's real journal.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import _commands_guard as guard
from tools.agentops import install_folder

START = datetime(2026, 10, 19, 10, 0, 5, 300000)  # the session starts at 10:00:05.3
END = datetime(2026, 10, 19, 10, 30, 0)


def _entries(*rows: tuple[str, str, str]) -> list[tuple[datetime, str, str]]:
    """Journal rows as `journal_entries` returns them: (naive local time, name, action)."""
    return [(datetime.fromisoformat(when), name, action) for when, name, action in rows]


def _moved(added: list[str] | None = None, removed: list[str] | None = None) -> dict[str, list[str]]:
    return {"added": sorted(added or []), "removed": sorted(removed or [])}


# -- row 1: a journaled install dated inside the session is excused, and the note names it ------------------------------

def test_a_journaled_install_inside_the_session_is_excused_and_noted():
    entries = _entries(("2026-10-19T10:05:00", "x", "install"))

    excused, unexplained = guard.unexplained_changes(_moved(added=["x"]), entries, START, END)

    assert excused == ["x"]
    assert unexplained == {"added": [], "removed": []}
    assert guard.note_line(excused) == "mods folder changed by mods install during the run: x"


# -- row 2: a folder added with no journal line stays an error, naming it -----------------------------------------------

def test_a_folder_added_with_no_journal_line_stays_an_error():
    excused, unexplained = guard.unexplained_changes(_moved(added=["stray"]), [], START, END)

    assert excused == []
    assert unexplained == {"added": ["stray"], "removed": []}


# -- row 3: a journal line dated before the session start does not excuse the change ------------------------------------

def test_a_journal_line_before_the_session_start_does_not_excuse():
    entries = _entries(("2026-10-19T10:00:04", "x", "install"))

    excused, unexplained = guard.unexplained_changes(_moved(added=["x"]), entries, START, END)

    assert excused == []
    assert unexplained == {"added": ["x"], "removed": []}


def test_a_journal_line_after_the_session_end_does_not_excuse():
    entries = _entries(("2026-10-19T10:31:00", "x", "install"))

    excused, unexplained = guard.unexplained_changes(_moved(added=["x"]), entries, START, END)

    assert excused == []
    assert unexplained == {"added": ["x"], "removed": []}


# -- a remove line dated inside the session excuses a removed name ---------------------------------------------------

def test_a_remove_line_inside_the_session_excuses_a_removed_name():
    entries = _entries(("2026-10-19T10:12:00", "unslop", "remove"))

    excused, unexplained = guard.unexplained_changes(_moved(removed=["unslop"]), entries, START, END)

    assert excused == ["unslop"]
    assert unexplained == {"added": [], "removed": []}


# -- an install line does not excuse a removal, a remove line does not excuse an addition -----------------------------

def test_an_install_line_does_not_excuse_a_removal():
    entries = _entries(("2026-10-19T10:12:00", "unslop", "install"))

    excused, unexplained = guard.unexplained_changes(_moved(removed=["unslop"]), entries, START, END)

    assert excused == []
    assert unexplained == {"added": [], "removed": ["unslop"]}


def test_a_remove_line_does_not_excuse_an_addition():
    entries = _entries(("2026-10-19T10:12:00", "x", "remove"))

    excused, unexplained = guard.unexplained_changes(_moved(added=["x"]), entries, START, END)

    assert excused == []
    assert unexplained == {"added": ["x"], "removed": []}


# -- a line in the session's first second counts (start floored to the second) ---------------------------------------

def test_a_line_in_the_first_second_of_the_session_counts():
    # START is 10:00:05.300; the journal writes 10:00:05 (truncated), which is the same second
    entries = _entries(("2026-10-19T10:00:05", "x", "install"))

    excused, unexplained = guard.unexplained_changes(_moved(added=["x"]), entries, START, END)

    assert excused == ["x"]
    assert unexplained == {"added": [], "removed": []}


# -- mixed: some changes excused, one not ------------------------------------------------------------------------------

def test_mixed_changes_split_into_excused_names_and_unexplained_changes():
    entries = _entries(
        ("2026-10-19T10:05:00", "x", "install"),
        ("2026-10-19T10:12:00", "unslop", "remove"),
    )
    moved = _moved(added=["stray", "x"], removed=["unslop"])

    excused, unexplained = guard.unexplained_changes(moved, entries, START, END)

    assert excused == ["unslop", "x"]
    assert unexplained == {"added": ["stray"], "removed": []}


def test_no_change_means_no_excused_name_and_no_note():
    excused, unexplained = guard.unexplained_changes(_moved(), [], START, END)

    assert excused == []
    assert unexplained == {"added": [], "removed": []}
    assert guard.note_line(excused) is None


# -- the journal read from a file path: the real journal is read by path, never through install_root() -------------

def test_journal_entries_reads_the_given_path_and_the_guard_excuses_from_it(tmp_path: Path):
    journal = tmp_path / "mods-journal.log"
    journal.write_text(
        "2026-10-19T10:00:04\told\tinstall\t4321\n"
        "2026-10-19T10:05:00\tx\tinstall\t4321\n",
        encoding="utf-8",
    )

    entries = install_folder.journal_entries(journal)
    excused, unexplained = guard.unexplained_changes(_moved(added=["old", "x"]), entries, START, END)

    assert excused == ["x"]
    assert unexplained == {"added": ["old"], "removed": []}


def test_journal_entries_of_a_missing_path_is_empty(tmp_path: Path):
    assert install_folder.journal_entries(tmp_path / "nope.log") == []


def test_the_real_journal_path_is_beside_the_real_mods_folder():
    assert guard.REAL_JOURNAL == guard.REAL_HOME / ".claude" / "mods-journal.log"
