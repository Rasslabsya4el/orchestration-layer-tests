"""Issue #349 unit 6: the blind case files of the brain-role replays.

build_case_files writes one case file (owner message, pass mark, six shuffled
outputs under A-F) and records the letter -> run key in a separate key file.
check_blind judges a case file for leaks: a missing or empty letter section,
an arm word in a heading, a session or run id from the key, a cost from the key.
"""

import json
import re
import shlex
from pathlib import Path

import pytest

from tools.replays.blind import build_case_files, check_blind

REPO = Path(__file__).resolve().parents[1]
CASES_PATH = REPO / "tools" / "replays" / "cases.json"
LETTERS = "ABCDEF"
MODEL = "claude-fable-5-1"
EFFORT = "high"
KEY_ENTRY_FIELDS = {"case", "letter", "arm", "run_id", "session_id", "cost_usd", "model", "effort"}


def _cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _outputs(tag: str, cost_offset: float = 0.0) -> list:
    outs = []
    for i in range(6):
        writes = [
            {
                "argv": ["gh", "issue", "create", "--title", f"Ticket {tag}{i}"],
                "body": f"Why: body {tag}{i}, line one\nline two of {tag}{i}",
            }
        ]
        if i % 2 == 0:
            writes.append(
                {
                    "argv": ["gh", "issue", "edit", str(9000 + i)],
                    "body": f"edited body {tag}{i}",
                }
            )
        outs.append(
            {
                "run_id": f"RUN-{tag}-{i}",
                "arm": "old" if i < 3 else "new",
                "session_id": f"SESS-{tag}-{i}-7d31",
                "cost_usd": 0.4711 + i + cost_offset,
                "model": MODEL,
                "effort": EFFORT,
                "text": f"Owner-visible answer {tag}{i}: it says something.",
                "writes": writes,
            }
        )
    return outs


def _sections(text: str) -> dict:
    """Map each level-2 heading name to the lines under it (test-side parser)."""
    sections: dict = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def _key(key_path: Path) -> dict:
    return json.loads(key_path.read_text(encoding="utf-8"))


def _entries(key_path: Path, case: str = None) -> list:
    entries = _key(key_path)["entries"]
    return [e for e in entries if case is None or e["case"] == case]


def _letter_to_run(key_path: Path, case: str) -> dict:
    return {e["letter"]: e["run_id"] for e in _entries(key_path, case)}


def _json_block(text: str):
    """The json fenced block of a write, parsed (the first one in the text)."""
    start = text.index("```json\n") + len("```json\n")
    end = text.index("\n```", start)
    return json.loads(text[start:end])


def test_six_sections_each_hold_its_text_and_its_write_bodies(tmp_path):
    outs = _outputs("a")
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=7)
    assert path.parent == tmp_path
    sec = _sections(path.read_text(encoding="utf-8"))
    assert sorted(name for name in sec if len(name) == 1) == list(LETTERS)
    seen = []
    for letter in LETTERS:
        body = sec[letter]
        matching = [o for o in outs if o["text"] in body]
        assert len(matching) == 1, letter
        out = matching[0]
        seen.append(out["run_id"])
        assert body.strip().startswith(out["text"]), letter
        for write in out["writes"]:
            assert shlex.join(write["argv"]) in body, letter
            assert write["body"] in body, letter
    assert sorted(seen) == sorted(o["run_id"] for o in outs)


def test_owner_message_and_pass_mark_equal_cases_json(tmp_path):
    case = _cases()["a"]
    path = build_case_files("a", _outputs("a"), tmp_path, tmp_path / "key.json", seed=1)
    sec = _sections(path.read_text(encoding="utf-8"))
    assert sec["Owner message"].strip() == case["owner_message"]
    assert sec["Pass mark"].strip() == case["pass_mark"]


def test_shuffle_is_deterministic_per_seed_and_differs_between_seeds(tmp_path):
    key_path = tmp_path / "key.json"
    orders = []
    for seed in range(1, 6):
        first = build_case_files("a", _outputs("a"), tmp_path / f"s{seed}a", key_path, seed=seed)
        mapping_first = _letter_to_run(key_path, "a")
        again = build_case_files("a", _outputs("a"), tmp_path / f"s{seed}b", key_path, seed=seed)
        mapping_again = _letter_to_run(key_path, "a")
        assert mapping_first == mapping_again
        assert first.read_text(encoding="utf-8") == again.read_text(encoding="utf-8")
        orders.append(tuple(mapping_first[letter] for letter in LETTERS))
    assert len(set(orders)) >= 2


def test_case_file_has_no_run_id_session_id_arm_label_or_cost(tmp_path):
    outs = _outputs("a")
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=3)
    text = path.read_text(encoding="utf-8")
    for out in outs:
        assert out["run_id"] not in text
        assert out["session_id"] not in text
        assert f"{out['cost_usd']}" not in text
    assert re.search(r"\b(old|new)\b", text, re.IGNORECASE) is None


def test_key_entries_are_right_and_other_cases_survive_a_second_call(tmp_path):
    key_path = tmp_path / "key.json"
    outs_a = _outputs("a")
    path_a = build_case_files("a", outs_a, tmp_path, key_path, seed=3)
    sec = _sections(path_a.read_text(encoding="utf-8"))
    entries_a = _entries(key_path, "a")
    assert len(entries_a) == 6
    by_run = {o["run_id"]: o for o in outs_a}
    for entry in entries_a:
        assert set(entry) == KEY_ENTRY_FIELDS
        out = by_run[entry["run_id"]]
        assert entry["arm"] == out["arm"]
        assert entry["session_id"] == out["session_id"]
        assert entry["cost_usd"] == out["cost_usd"]
        assert entry["model"] == MODEL
        assert entry["effort"] == EFFORT
        assert out["text"] in sec[entry["letter"]]

    build_case_files("b", _outputs("b"), tmp_path / "b", key_path, seed=3)
    entries_b = _entries(key_path, "b")
    assert len(entries_b) == 6
    assert _entries(key_path, "a") == entries_a

    build_case_files("a", _outputs("a2"), tmp_path / "a2", key_path, seed=3)
    assert len(_entries(key_path, "a")) == 6
    assert _entries(key_path, "b") == entries_b
    assert not any(e["run_id"].startswith("RUN-a-") for e in _entries(key_path, "a"))


def test_key_has_18_entries_over_three_cases_with_the_total_and_model_effort(tmp_path):
    key_path = tmp_path / "key.json"
    expected_total = 0.0
    for tag, case in (("a", "a"), ("b", "b"), ("c", "c")):
        outs = _outputs(tag)
        build_case_files(case, outs, tmp_path / case, key_path, seed=4)
        expected_total += sum(o["cost_usd"] for o in outs)
    key = _key(key_path)
    assert len(key["entries"]) == 18
    assert sorted(len(_entries(key_path, c)) for c in ("a", "b", "c")) == [6, 6, 6]
    assert key["total_cost_usd"] == pytest.approx(expected_total)
    assert all(e["model"] == MODEL and e["effort"] == EFFORT for e in key["entries"])

    outs_a2 = _outputs("a2", cost_offset=10.0)
    build_case_files("a", outs_a2, tmp_path / "a2", key_path, seed=4)
    key = _key(key_path)
    assert len(key["entries"]) == 18
    expected_total = sum(o["cost_usd"] for o in outs_a2) + sum(
        o["cost_usd"] for o in _outputs("b") + _outputs("c")
    )
    assert key["total_cost_usd"] == pytest.approx(expected_total)
    assert key["total_cost_usd"] == pytest.approx(sum(e["cost_usd"] for e in key["entries"]))


def test_key_keeps_other_top_level_keys_on_read_modify_write(tmp_path):
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps({"entries": [], "total_cost_usd": 0.0, "note": "kept"}), encoding="utf-8")
    build_case_files("a", _outputs("a"), tmp_path, key_path, seed=5)
    build_case_files("b", _outputs("b"), tmp_path, key_path, seed=5)
    assert _key(key_path)["note"] == "kept"
    assert len(_key(key_path)["entries"]) == 12


def test_empty_text_raises_and_writes_nothing(tmp_path):
    outs = _outputs("a")
    outs[2]["text"] = ""
    with pytest.raises(ValueError):
        build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    assert not (tmp_path / "case-a.md").exists()
    assert not (tmp_path / "key.json").exists()


def test_whitespace_only_text_counts_as_empty(tmp_path):
    outs = _outputs("a")
    outs[5]["text"] = "  \n "
    with pytest.raises(ValueError):
        build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)


def test_output_without_model_or_effort_raises_and_writes_nothing(tmp_path):
    for field in ("model", "effort"):
        outs = _outputs("a")
        del outs[4][field]
        with pytest.raises(ValueError):
            build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    assert not (tmp_path / "case-a.md").exists()
    assert not (tmp_path / "key.json").exists()


def test_five_outputs_raise(tmp_path):
    with pytest.raises(ValueError):
        build_case_files("a", _outputs("a")[:5], tmp_path, tmp_path / "key.json", seed=1)


def test_label_and_close_writes_without_body_render_argv_only_and_never_none(tmp_path):
    outs = _outputs("a")
    outs[0]["writes"] = [
        {"argv": ["gh", "issue", "close", "9001"], "body": None},
        {"argv": ["gh", "issue", "edit", "9002", "--add-label", "parked"]},
    ]
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    text = path.read_text(encoding="utf-8")
    assert "None" not in text
    assert "```text\ngh issue close 9001\n```" in text
    assert "```text\ngh issue edit 9002 --add-label parked\n```" in text


def test_api_write_renders_its_fields_in_full_as_a_json_block(tmp_path):
    fields = {"state": "closed", "labels": ["parked", "done"], "count": 2}
    outs = _outputs("a")
    outs[1]["writes"] = [
        {
            "argv": ["gh", "api", "-X", "PATCH", "repos/o/r/issues/9003"],
            "body": None,
            "fields": fields,
        }
    ]
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    text = path.read_text(encoding="utf-8")
    assert "None" not in text
    assert "gh api -X PATCH repos/o/r/issues/9003" in text
    assert _json_block(text) == fields


def test_list_fields_render_in_full_too(tmp_path):
    fields = ["first", {"k": "v"}, 3]
    outs = _outputs("a")
    outs[1]["writes"] = [
        {"argv": ["gh", "api", "-X", "POST", "repos/o/r/x"], "body": None, "fields": fields}
    ]
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    assert _json_block(path.read_text(encoding="utf-8")) == fields


def test_check_blind_clean_file_has_no_problems(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    assert check_blind(path, key_path) == []


def test_check_blind_catches_a_planted_session_id(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\nleaked SESS-a-3-7d31 here\n")
    problems = check_blind(path, key_path)
    assert problems
    assert any("SESS-a-3-7d31" in p for p in problems)


def test_check_blind_catches_a_planted_cost_as_the_key_holds_it(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\nspent 2.4711 dollars\n")
    problems = check_blind(path, key_path)
    assert any("2.4711" in p for p in problems)


def test_check_blind_cost_match_needs_word_boundaries(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\nsum 12.4711 and 0.47110 here\n")
    assert check_blind(path, key_path) == []


def test_check_blind_catches_a_missing_section(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    text = path.read_text(encoding="utf-8")
    cut = text.index("## C\n")
    end = text.index("\n## ", cut + 1)
    path.write_text(text[:cut] + text[end + 1 :], encoding="utf-8")
    problems = check_blind(path, key_path)
    assert any("C" in p for p in problems)


def test_check_blind_catches_an_arm_word_in_a_heading(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n### old run\n")
    problems = check_blind(path, key_path)
    assert any("old" in p for p in problems)


def test_check_blind_fails_closed_without_key(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _outputs("a"), tmp_path, key_path, seed=2)
    key_path.unlink()
    assert check_blind(path, key_path) != []
