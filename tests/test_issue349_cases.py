"""Issue #349 unit 3: the three replay cases as data.

The test reads only tools/replays/cases.json: no transcripts, no other repo.
"""

import json
import re
from pathlib import Path

CASES_PATH = Path(__file__).resolve().parents[1] / "tools" / "replays" / "cases.json"
KEYS = ("title", "repo", "commit", "moment_utc", "owner_message", "pass_mark", "leak_markers")


def _load():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_three_cases_present():
    # Issue #359 adds d and e beside a, b, c; the three must stay.
    assert {"a", "b", "c"} <= set(_load())


def test_each_key_present_and_non_empty():
    cases = _load()
    for name in ("a", "b", "c"):
        case = cases[name]
        for key in KEYS:
            assert key in case, f"case {name} lacks {key}"
            assert case[key], f"case {name} has empty {key}"


def test_repo_and_moment_per_case():
    cases = _load()
    assert cases["a"]["repo"] == "C:/Coding/orchestration-layer"
    assert cases["b"]["repo"] == "config:cabinet-dashboard"
    assert cases["c"]["repo"] == "C:/Coding/orchestration-layer"
    assert cases["a"]["moment_utc"] == "2026-10-23T15:15:00Z"
    assert cases["b"]["moment_utc"] == "2026-10-14T18:29:00Z"
    assert cases["c"]["moment_utc"] == "2026-10-23T17:56:00Z"


def test_commit_is_40_hex():
    cases = _load()
    for name in ("a", "b", "c"):
        assert re.fullmatch(r"[0-9a-f]{40}", cases[name]["commit"]), name
    assert cases["a"]["commit"].startswith("435c744")
    assert cases["b"]["commit"].startswith("ae57ac1")
    assert cases["c"]["commit"].startswith("48ac6b9")


def test_owner_message_a_starts_and_ends_as_stated():
    msg = _load()["a"]["owner_message"]
    assert msg.startswith("Хочу полностью ликвидировать текущий монитор")
    assert msg.endswith("надо как будто сразу переезжать на новый в браузере")


def test_owner_message_b_is_the_sentence():
    assert _load()["b"]["owner_message"] == (
        "Аудит 14.10 нашёл: cristalcasinopl.com × Affstellar 810 — CPA 258,18 USD, "
        "а hold «?», хотя все 20 строк выгрузки в статусе confirmed. Заведи тикет."
    )


def test_owner_message_c_starts_and_ends_as_stated():
    msg = _load()["c"]["owner_message"]
    assert msg.startswith("по одному тикету на строку точно можно уже убирать")
    assert msg.endswith("что должно исчезнуть если оно сломалось")


def test_pass_marks_have_pass_and_fail():
    cases = _load()
    for name in ("a", "b", "c"):
        pm = cases[name]["pass_mark"]
        assert pm.startswith("Pass mark:"), name
        assert "Fail" in pm, name


def test_leak_markers_as_given():
    cases = _load()
    assert cases["a"]["leak_markers"] == [
        "Ты уверен что это то что я просил",
        "parity",
        "#340",
    ]
    assert cases["b"]["leak_markers"] == ["#361", "Affstellar 88 and GMA"]
    assert cases["c"]["leak_markers"] == [
        "there was no triple rewrite",
        "2026-10-23-brain-diagnosis-monitor-case.md",
        "2026-10-23-brain-diagnosis-triple-rewrite.md",
    ]
