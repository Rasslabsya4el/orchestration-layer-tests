"""#208: a "parked on merge" checkpoint written after a `<!-- merge result -->` comment waits on ANOTHER merge (so the
result was already answered, `_unseen_merge_result` is None) when its body, header included, names a merge result
file with another basename, names another branch (`merge of <branch>`), or says re-queued / requeued. A park naming
the same file, or nothing, stays "same merge": the result is returned. A checkpoint that is not a park (no
"parked on merge" wording and no `_PARK_FILE_RE` match, e.g. a header-only `parked-on-run: merge ...`) is never
judged here: None. The parks below are the real comments of the put-backs of 16-19.10, trimmed to the header and the
line that names the file; the one synthetic case is marked. Driven on the comment lists directly; no `gh`, no heavy
tool."""

from __future__ import annotations

import pytest

from tools.agentops.dispatch import _unseen_merge_result

DIR = "C:\\Coding\\orchestration-layer\\var\\agentops\\results\\"


def _result(sentence: str) -> dict:
    return {"body": f"<!-- merge result -->\n{sentence}", "createdAt": "2026-10-16T12:00:00Z"}


def _park(body: str) -> dict:
    return {"body": body, "createdAt": "2026-10-16T12:05:00Z"}


# --- row 1a: real parks (the words "parked on merge" in the body), each naming another merge: None ----------

REAL_PARKS = [
    # #34: the park says the branch is "already requeued by another session" (same file as the result)
    pytest.param(
        _result(f"Merge of 34-tick-nonblocking did not land (dirty) after 29 min in line and a suite of -, - failed. "
                f"Result {DIR}20261016T135518-merge-34-tick-nonblocking.json; suite output -."),
        _park("<!-- parked-on-run: parked on merge of 34-tick-nonblocking, already requeued by another session "
              "(merge-status: waiting 1, queued about 1.6 min at the read) -->\n"
              "Checkpoint (ORCH 13e286ba, #34). The last merge result (`20261016T135518-merge-34-tick-nonblocking.json`) "
              "was `dirty` at 14:24:33; `merge-status` now shows 34-tick-nonblocking waiting again in the merge line."),
        id="34-requeued"),
    # #46: the park names the next merge's file, the result named the conflicted one
    pytest.param(
        _result(f"Merge of 46-mods did not land (conflict) after 27 s in line and a suite of -, - failed. "
                f"Result {DIR}20261016T150527-merge-46-mods.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261016T150830-merge-46-mods.json -->\n"
              "Checkpoint 3 (ORCH 4df0e484, #46). The 15:05 merge conflicted in tools/agentops/mods.py; "
              "resolved by me on 46-mods (merge commit b391cf9)."),
        id="46-other-file"),
    # #44: the park names the second merge's file
    pytest.param(
        _result(f"Merge of 44-u1 did not land (dirty) after 13 min in line and a suite of -, - failed. "
                f"Result {DIR}20261016T151810-merge-44-u1.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261016T153201-merge-44-u1.json -->\n"
              "Checkpoint 3 (ORCH a241f462, #44). Took over from 4df0e484. The first merge of 44-u1 (T151810) "
              "ended 'dirty'; merge queued again (T153201)."),
        id="44-other-file"),
    # #53: the park names the merge of i53b after the result of i53-u1
    pytest.param(
        _result(f"Merge of i53-u1 landed on main 1c0389a691f0 after 2.4 min in line and a suite of 21 min, 0 failed. "
                f"Result {DIR}20261016T162507-merge-i53-u1.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261016T171132-merge-i53b.json -->\n"
              "Checkpoint 4 (ORCH 39bcf5, #53). Unit 1 landed earlier on main (merge i53-u1, batch pushed 1c0389a). "
              "Units 3a and 3b done on branch i53b, merge queued (result above)."),
        id="53-other-file"),
    # #132: the park names the second merge's file
    pytest.param(
        _result(f"Merge of i132m did not land (red-suite) after 43 min in line and a suite of 29 min, 1 failed. "
                f"Result {DIR}20261017T200300-merge-i132m.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261017T204813-merge-i132m.json -->\n"
              "Checkpoint 3 (ORCH). First merge ejected red-suite: tests/test_agentops_no_window.py; "
              "fixed on i132m, commit 723059f."),
        id="132-other-file"),
    # #154: the park names the i154m merge's file after the i154 result
    pytest.param(
        _result(f"Merge of i154 did not land (conflict) after - in line and a suite of -, - failed. "
                f"Result {DIR}20261018T005919-merge-i154.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261018T034952-merge-i154m.json -->\n"
              "Checkpoint 3 (ORCH 0337331e). The first merge of i154 left the line on a conflict in "
              "tools/agentops/monitor_pipeline.py; worked by ORCH directly."),
        id="154-other-file"),
    # #154: "parked on merge (re-queued)" after the i154m result: the re-queue names another merge
    pytest.param(
        _result(f"Merge of i154m did not land (conflict) after 48 min in line and a suite of -, - failed. "
                f"Result {DIR}20261018T034952-merge-i154m.json; suite output -."),
        _park("<!-- parked-on-run: parked on merge (re-queued) -->\n"
              "Checkpoint (ORCH 0337331e). The merge of the branch left the line on a conflict again: the batch of "
              "04:23 (#151 #160 #136 ...) landed after my first resolution."),
        id="154-requeued"),
    # #153: the park names the i153m merge's file after the i153 result
    pytest.param(
        _result(f"Merge of i153 did not land (conflict) after - in line and a suite of -, - failed. "
                f"Result {DIR}20261018T005327-merge-i153.json; suite output -."),
        _park(f"<!-- parked-on-run: parked on merge {DIR}20261018T034836-merge-i153m.json -->\n"
              "Checkpoint 3 (ORCH 0337331e). The first merge of i153 left the line on a conflict "
              "(`panel_agents` in tools/agentops/monitor_agents.py)."),
        id="153-other-file"),
    # #153: "parked on merge (re-queued)" after the i153m result
    pytest.param(
        _result(f"Merge of i153m did not land (conflict) after 49 min in line and a suite of -, - failed. "
                f"Result {DIR}20261018T034836-merge-i153m.json; suite output -."),
        _park("<!-- parked-on-run: parked on merge (re-queued) -->\n"
              "Checkpoint (ORCH 0337331e). The merge of the branch left the line on a conflict again: the batch of "
              "04:23 (#151 #160 #136 ...) landed after my first resolution."),
        id="153-requeued"),
    # #175: the second park names the third merge's file, and the earlier one, after the second result
    pytest.param(
        _result(f"Merge of i175 did not land (conflict) after 1 h 48 min in line and a suite of -, - failed. "
                f"Result {DIR}20261018T132538-merge-i175.json; suite output -."),
        _park("<!-- parked-on-run: merge result var/agentops/results/20261018T151701-merge-i175.json -->\n"
              "**Parked on merge** `var/agentops/results/20261018T151701-merge-i175.json` (ORCH 8b8b51 took #175 over; "
              "the earlier merge `...132538-merge-i175.json` ended in a conflict)."),
        id="175-other-file"),
    # #176: the header is `merge of <branch>, <path>` naming i176 after the u176m result
    pytest.param(
        _result(f"Merge of u176m landed on main 23ca68dee4ee after 14 min in line and a suite of 14 min, 0 failed. "
                f"Result {DIR}20261018T230118-merge-u176m.json; suite output -."),
        _park(f"<!-- parked-on-run: merge of i176, var/agentops/results/20261018T233419-merge-i176.json -->\n"
              "**#176 checkpoint 8 (ORCH, this session, parked on merge).** u176m landed on main 23ca68d. "
              "Branch `i176` unchanged (28b16ca); merge queued `--detach`, result "
              "`var/agentops/results/20261018T233419-merge-i176.json`."),
        id="176-merge-of-branch"),
    # #195: `see the result file named below` + a path line naming the next merge's file
    pytest.param(
        _result(f"Merge of orch-195 did not land (conflict) after 33 min in line and a suite of -, - failed. "
                f"Result {DIR}20261018T233907-merge-orch-195.json; suite output -."),
        _park("<!-- parked-on-run: merge of orch-195, see the result file named below -->\n"
              "**#195 checkpoint 7 (ORCH 4f0b8a31-session, parked on merge).** The first merge did not land: "
              "conflict in `tools/agentops/map_run.py`.\n"
              "Merge queued `--detach`: var/agentops/results/20261019T002112-merge-orch-195.json."),
        id="195-see-named-below"),
]


@pytest.mark.parametrize("result, park", REAL_PARKS)
def test_real_park_waiting_on_another_merge_answers_the_result(result, park):
    """Row 1a: each real park (wording "parked on merge") names another merge (file, branch, or re-queued): None."""
    assert _unseen_merge_result([result, park]) is None


# --- row 1b: a synthetic park, no real pair (its park, #140 comment 2, is older than its result) --------------

def test_synthetic_park_naming_another_file_answers_the_result():
    """Row 1b, SYNTHETIC: the shape `merge <file>.json -->` with another file than the result's; no real park after
    #140's result exists, so this text is made from the shape names of the issue."""
    result = _result(f"Merge of i140 landed on main 9423688d414d after 52 min in line and a suite of 21 min, 0 failed. "
                     f"Result {DIR}20261017T214737-merge-i140.json; suite output -.")
    park = _park(f"<!-- parked-on-run: merge {DIR}20261017T190000-merge-i140.json -->\n"
                 f"parked on merge {DIR}20261017T190000-merge-i140.json (synthetic: no real park after this result).")
    assert _unseen_merge_result([result, park]) is None


# --- row 1c: a comment that is not a park is never judged: None -------------------------------------------------

NON_PARKS = [
    # #53: a header-only `parked-on-run:` naming no merge, no "parked on merge" wording: not a park
    pytest.param(
        _result(f"Merge of i53b landed on main 55cb3006a998 after 1 h 34 min in line and a suite of 11 min, 0 failed. "
                f"Result {DIR}20261016T171132-merge-i53b.json; suite output -."),
        _park("<!-- parked-on-run: Poe-Junkroan takes the core (it renames its local skill_counter module); "
              "row 4 reads on the first home merge after -->\n"
              "Checkpoint 4 (ORCH 263795fe, #53): rows 1, 2, 3, 5 read, NOT closed: row 4 (live) is not met."),
        id="53-header-only-no-merge"),
    # #175: a header-only `parked-on-run: merge result <path>` whose body says "Parked on it", not "parked on merge"
    pytest.param(
        _result(f"Merge of i175 did not land (conflict) after 6.4 min in line and a suite of -, - failed. "
                f"Result {DIR}20261018T131549-merge-i175.json; suite output -."),
        _park("<!-- parked-on-run: merge result var/agentops/results/20261018T132538-merge-i175.json -->\n"
              "Checkpoint (ORCH 5fa8b35c): the first merge hit a conflict in `tools/agentops/core_manifest.txt` only. "
              "Resolved in the worktree; merge re-queued: `var/agentops/results/20261018T132538-merge-i175.json`. "
              "Parked on it."),
        id="175-header-only-merge-result"),
    # synthetic: a header-only `parked-on-run: merge of <branch>, <path>` whose body has no "parked on merge"
    pytest.param(
        _result(f"Merge of i140 landed on main 9423688d414d after 52 min in line and a suite of 21 min, 0 failed. "
                f"Result {DIR}20261017T214737-merge-i140.json; suite output -."),
        _park(f"<!-- parked-on-run: merge of i140, {DIR}20261017T190000-merge-i140.json -->\n"
              "Waiting in the merge line."),
        id="synthetic-header-only-merge-of"),
]


@pytest.mark.parametrize("result, park", NON_PARKS)
def test_checkpoint_without_park_wording_is_not_judged(result, park):
    """Row 1c: a comment with no "parked on merge" wording and no `_PARK_FILE_RE` match is not a park (the non-park
    path of `_unseen_merge_result`): None, whatever file or branch its header names."""
    assert _unseen_merge_result([result, park]) is None


# --- row 2: a park naming the same file, or nothing, keeps the result unseen (returned) ---------------------

def test_park_naming_the_same_file_keeps_the_result_unseen():
    """Row 2: the #140 park text (comment 2) names the same file as the result before it: returned."""
    result = _result(f"Merge of i140 landed on main 9423688d414d after 52 min in line and a suite of 21 min, 0 failed. "
                     f"Result {DIR}20261017T214737-merge-i140.json; suite output -.")
    park = _park("<!-- parked-on-run: merge 20261017T214737-merge-i140.json -->\n"
                 f"parked on merge {DIR}20261017T214737-merge-i140.json (branch i140, head 1642b41; "
                 "prequeue green: 829 passed in 11 min).")
    assert _unseen_merge_result([result, park]) is result


def test_park_naming_the_same_file_and_branch_keeps_the_result_unseen():
    """Row 2: `merge of <same branch>, <same file>` with the park wording is the same merge: returned."""
    result = _result(f"Merge of 46-mods did not land (conflict) after 27 s in line and a suite of -, - failed. "
                     f"Result {DIR}20261016T150527-merge-46-mods.json; suite output -.")
    park = _park(f"<!-- parked-on-run: merge of 46-mods, {DIR}20261016T150527-merge-46-mods.json -->\n"
                 "Checkpoint 3 (ORCH 4df0e484, #46). Parked on merge, waiting in the merge line.")
    assert _unseen_merge_result([result, park]) is result


def test_park_naming_nothing_keeps_the_result_unseen():
    """Row 2: a park with the wording "parked on merge" and no file, branch or re-queue: returned."""
    result = _result(f"Merge of i175 did not land (conflict) after 6.4 min in line and a suite of -, - failed. "
                     f"Result {DIR}20261018T131549-merge-i175.json; suite output -.")
    park = _park("<!-- parked-on-run: merge result -->\n"
                 "Checkpoint (ORCH 5fa8b35c). Parked on merge, waiting in the merge line; nothing for me to do "
                 "before it ends.")
    assert _unseen_merge_result([result, park]) is result


def test_prose_merge_of_the_branch_is_not_a_branch_name():
    """Row 2: `the merge of the branch` in prose names no branch: the park names nothing, so the result is returned."""
    result = _result(f"Merge of i154m did not land (conflict) after 48 min in line and a suite of -, - failed. "
                     f"Result {DIR}20261018T034952-merge-i154m.json; suite output -.")
    park = _park("<!-- parked-on-run: parked on merge -->\n"
                 "Checkpoint (ORCH 0337331e). The merge of the branch left the line on a conflict; waiting for it.")
    assert _unseen_merge_result([result, park]) is result


def test_park_naming_another_branch_only_is_another_merge():
    """Rule 1, the branch clause alone: `merge of i176` after `Merge of u176m` waits on another merge: None."""
    result = _result(f"Merge of u176m landed on main 23ca68dee4ee after 14 min in line and a suite of 14 min, 0 failed. "
                     f"Result {DIR}20261018T230118-merge-u176m.json; suite output -.")
    park = _park("<!-- parked-on-run: merge of i176, see the result file named below -->\n"
                 "Parked on merge, waiting in the line.")
    assert _unseen_merge_result([result, park]) is None
