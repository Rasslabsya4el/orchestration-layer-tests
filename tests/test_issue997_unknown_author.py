"""#997: a session that signs its comments with an id that is neither its dispatcher id nor its transcript uuid
("ORCH c17e", #986) and parks on a merge whose `<!-- merge result -->` lands in the same second is `released`, with no
"ended without" comment and no defect ticket, whichever order the API lists the two comments in. An id the
dispatcher knows as ANOTHER session's keeps its comment foreign. Replays #986's end through the real
`GhCli.own_checkpoint_parks` and the dispatcher's end path (the harness of test_issue852_released_own_note)."""

from __future__ import annotations

import pytest

from tests.test_issue852_released_own_note import _assert_alarm, _assert_released, _end
from tests._area_rule import _no_area_rule  # noqa: F401

PARKED = ("<!-- parked-on-run: merge of 986-c (result var/agentops/results/x.json) -->\n"
          "Checkpoint 10 (ORCH c17e, #986): unit 1 done on branch `986-c`, queued detached.")
MERGE = "<!-- merge result -->\nMerge of 986-c landed on main 9765130acd06 after 0.1 s in line, 0 failed."


@pytest.mark.parametrize("thread", [
    [(5, PARKED), (5, MERGE)],
    [(5, MERGE), (5, PARKED)],
], ids=["parked-then-merge-result", "merge-result-then-parked"])
def test_unknown_signature_parked_with_a_same_second_merge_result_is_released(tmp_path, thread):
    _assert_released(tmp_path, *_end(tmp_path, thread))


def test_the_session_that_names_itself_by_its_real_id_is_still_released(tmp_path):
    named = PARKED.replace("ORCH c17e", "ORCH <id>")
    _assert_released(tmp_path, *_end(tmp_path, [(5, named), (5, MERGE)]))


def test_a_claim_only_note_with_an_unknown_signature_stays_no_checkpoint(tmp_path):
    claim = "Checkpoint 9 (ORCH c17e, #986): claimed; one unit, none needs."
    outcome, gh, core_gh, _ = _end(tmp_path, [(2, claim), (5, MERGE)])
    _assert_alarm(outcome, gh, core_gh)


def test_an_id_the_dispatcher_knows_as_another_session_stays_foreign():
    import json
    import subprocess

    from tools.agentops.dispatch import GhCli

    def parks(other_ids):
        runner = lambda args, stdin=None: subprocess.CompletedProcess(
            args=args, returncode=0, stderr="",
            stdout=json.dumps({"comments": [{"body": PARKED, "createdAt": "2026-10-14T02:18:50Z"}]}))
        return GhCli(runner=runner).own_checkpoint_parks(
            986, ("1a8c43f2",), "2026-10-14T02:13:30Z", "2026-10-14T02:20:22Z", other_ids)

    assert parks(()) is True            # nobody is known as c17e: the comment is this session's own
    assert parks(("c17ec8a7-eab4",)) is False   # c17e is a known other session: not this one's
    assert parks(None) is False         # the older rule: any id that is not this session's is foreign
