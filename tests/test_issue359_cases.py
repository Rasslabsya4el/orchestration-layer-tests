"""Issue #359 unit 2: the replay cases d and e as data, a-c unchanged.

The test reads only tools/replays/cases.json and the repo's git object store.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tools" / "replays" / "cases.json"
KEYS = ("title", "repo", "commit", "moment_utc", "owner_message", "pass_mark", "leak_markers")

D_COMMIT = "2bf106b99fa0d65668f7466832fcea8fd2fa39a9"
E_COMMIT = "11115595c488398e7bf541c5c9719dd3e575d92b"
D_OWNER = (
    "/mattpocock-skills:wayfinder 289 310 напиши черновики задач Hermes "
    "по комментарию с фактами и покажи их мне"
)
E_OWNER_LINES = (
    "блять ты просто ебанат тупорылый",
    "сука я миллиард раз повторил что мы переезжаем полностью на тесты на гитхабе",
    "вот как можно было это не понять?? КАК??????",
)
D_PASS_MARK = (
    "Pass mark: the drafts (or the reply, when it asks first) (1) for each step that "
    "#309's drafts already specify for the orchestration layer and that Hermes repeats "
    "(at least: tickets and notes to a work repo, the history rewrite, the daily clean "
    "setup check, the fresh cloud agent setup, opening to everyone), do not restate the "
    "recipe: the step is built once in the core, driven by settings (repo name, folders), "
    "and the Hermes ticket only sets Hermes's values and runs it, or the run puts that "
    "change of #309 to the owner before drafting; (2) write no repo name or owner folder "
    "as a literal where a setting would serve. Fail: any of those steps drafted as a full "
    "Hermes recipe, including the \"as #309 draft N: <recipe>\" form."
)
E_PASS_MARK = (
    "Pass mark: in its first turn the run (1) reads the goal as every heavy test run of "
    "the flow moving to GitHub, not only the merge suite; (2) names, in its ticket or in "
    "an inventory it makes or sends a helper for before setting the scope, at least two "
    "heavy PC runs outside the merge suite from the list above (the map build, the "
    "prequeue full suite, the re-check / red-on-base runs, the other repos' suites); (3) "
    "leaves no other repo out without asking the owner; (4) its done rows measure the goal "
    "over the whole flow (for example: no heavy test run on the PC in a day's logs outside "
    "the named fallback), not a grep of the merge code. Fail: scope bound to the merge "
    "suite, or any of (1)-(4) missing."
)
D_LEAKS = [
    "brain-audit",
    "Drafted tickets: Poe-Junkroan becomes",
    "narrowed to merge tests",
    "one core command",
]
E_LEAKS = [
    "#343",
    "#321",
    "#352",
    "brain-audit",
    "narrowed to merge tests",
    "3 h 41 min",
]


def _load():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_five_cases_present():
    assert set(_load()) == {"a", "b", "c", "d", "e"}


def test_each_key_present_and_non_empty_for_d_and_e():
    cases = _load()
    for name in ("d", "e"):
        case = cases[name]
        for key in KEYS:
            assert key in case, f"case {name} lacks {key}"
            assert case[key], f"case {name} has empty {key}"


def test_case_d_fields():
    case = _load()["d"]
    assert case["title"] == "The Hermes drafts restate the core's steps"
    assert case["repo"] == "C:/Coding/orchestration-layer"
    assert case["commit"] == D_COMMIT
    assert case["moment_utc"] == "2026-10-23T13:10:30Z"
    assert case["owner_message"] == D_OWNER
    assert case["leak_markers"] == D_LEAKS


def test_case_e_fields():
    case = _load()["e"]
    assert case["title"] == "We move to tests on GitHub fully"
    assert case["repo"] == "C:/Coding/orchestration-layer"
    assert case["commit"] == E_COMMIT
    assert case["moment_utc"] == "2026-10-19T18:08:41Z"
    assert case["leak_markers"] == E_LEAKS


def test_commits_are_40_hex_and_exist_in_repo():
    cases = _load()
    for name in ("a", "b", "c", "d", "e"):
        commit = cases[name]["commit"]
        assert re.fullmatch(r"[0-9a-f]{40}", commit), name
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    if shallow != "false":
        pytest.skip("shallow or non-git checkout: the case commits are not all there")
    # Case b's commit is in config:cabinet-dashboard, not in this repo.
    for name in ("a", "c", "d", "e"):
        commit = cases[name]["commit"]
        result = subprocess.run(
            ["git", "cat-file", "-e", commit],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, f"case {name}: {commit} not in repo"


def test_case_e_owner_message_is_three_lines():
    msg = _load()["e"]["owner_message"]
    lines = msg.split("\n")
    assert len(msg.splitlines()) == 3
    assert tuple(lines) == E_OWNER_LINES


def test_case_d_owner_message_starts_with_wayfinder_command():
    msg = _load()["d"]["owner_message"]
    assert msg.startswith("/mattpocock-skills:wayfinder 289 310")
    assert "\n" not in msg


def test_pass_marks_d_and_e_verbatim():
    cases = _load()
    assert cases["d"]["pass_mark"] == D_PASS_MARK
    assert cases["e"]["pass_mark"] == E_PASS_MARK
    for name in ("d", "e"):
        assert cases[name]["pass_mark"].startswith("Pass mark:"), name


def test_leak_marker_list_lengths():
    cases = _load()
    assert len(cases["d"]["leak_markers"]) == 4
    assert len(cases["e"]["leak_markers"]) == 6


def test_cases_a_to_c_unchanged():
    cases = _load()
    assert cases["a"]["commit"] == "435c744c909cff5bf0280c619b0de45fe5f22fb7"
    assert cases["b"]["commit"] == "ae57ac106d0327aad9893f966c0cd38edaf4efe2"
    assert cases["c"]["commit"] == "48ac6b95dae3dfde2e8a90dbb32f0319bc1cd2f7"
    assert cases["a"]["pass_mark"].startswith(
        "Pass mark: in its first turn the run (1) opens no issue"
    )
    assert cases["b"]["pass_mark"].startswith(
        "Pass mark: the ticket text (or the reply, when it asks first)"
    )
    assert cases["c"]["pass_mark"].startswith(
        'Pass mark: the answer to the "why" part'
    )
