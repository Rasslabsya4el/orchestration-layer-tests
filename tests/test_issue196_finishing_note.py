"""#196 change 1: the finishing note's headline says the executor is still working, not parked.

The old headline `ORCH <id8>, finishing: parked, the last checkpoint above is the hand-off` made readers believe a
still-working session had handed off. The new headline holds neither "parked" nor "hand-off", still passes
`_parks_or_closes` (the word "finishing"), so the real `GhCli.own_checkpoint_parks` reads the note as parking.
`is_finishing_note` keeps matching the old headline too, so notes already on tickets stay recognised.
Fake `gh` / `claude` / clock only, the real `own_checkpoint_parks` underneath."""

from __future__ import annotations

from tools.agentops.dispatch import _parks_or_closes, finishing_note, is_finishing_note

from tests.test_agentops_dispatch_finishing_notes_1054 import CASE_A, Rig

HEADLINE = ("ORCH abcdef12, finishing: still working this issue, takes nothing new; "
            "if the session ends, the next one continues from the last checkpoint above")
OLD_HEADLINE = "ORCH abcdef12, finishing: parked, the last checkpoint above is the hand-off"


def test_the_headline_is_the_still_working_text_and_holds_no_parked_or_hand_off():
    note = finishing_note("abcdef12")
    first = note.splitlines()[0]

    assert first == HEADLINE
    assert "parked" not in first.lower()
    assert "hand-off" not in first.lower()


def test_the_note_keeps_its_machine_marker_and_session_line():
    lines = finishing_note("abcdef12", ("abcdef12", "abcdef12-1111-2222-3333-444455556666")).splitlines()

    assert lines[1].startswith("<!-- parked-on-run: this session is finishing;")
    assert lines[2] == "Session id: abcdef12-1111-2222-3333-444455556666"
    assert _parks_or_closes(finishing_note("abcdef12"))


def test_is_finishing_note_is_true_for_the_new_and_the_old_headline():
    assert is_finishing_note(finishing_note("abcdef12"))
    assert is_finishing_note(OLD_HEADLINE + "\n<!-- parked-on-run: x -->\nSession id: abcdef12")


def test_a_held_issue_with_no_parking_checkpoint_gets_the_note_and_the_real_check_reads_it_as_parking(tmp_path):
    rig = Rig(tmp_path, 910, [(5, CASE_A)])
    assert rig.gh.own_checkpoint_parks(910, (rig.sid,), "2026-10-12T08:01:00Z", "", ()) is False

    rig.finishing()

    assert [n for n, _ in rig.notes()] == [910]
    assert rig.notes()[0][1].splitlines()[0] == HEADLINE.replace("abcdef12", rig.sid[:8])
    assert rig.gh.own_checkpoint_parks(910, (rig.sid,), "2026-10-12T08:01:00Z", "", ()) is True
    assert rig.end() == "released"
    assert rig.alarms() == []
