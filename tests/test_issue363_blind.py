"""Issue #363 unit 3b: the blind case files take nine outputs and a second turn.

Seams: build_case_files and check_blind (tools/replays/blind.py) and the
turn-2 answers and round-4 pass marks in tools/replays/cases.json.
A record with ``turns`` renders ``### Turn 1`` (and ``### Turn 2`` when
turns == 2); a record without ``turns`` renders as before.
"""

import json
import shlex
from pathlib import Path

import pytest

from tools.replays.blind import build_case_files, check_blind

REPO = Path(__file__).resolve().parents[1]
CASES_PATH = REPO / "tools" / "replays" / "cases.json"
MODEL = "claude-fable-5-1"
EFFORT = "high"
NINE = list("ABCDEFGHI")
SIX = list("ABCDEF")

A_TURN2 = (
    "Я не вижу, что происходит: по 70 задачам переезда и вообще по всем 4 репо. Хочу открыть "
    "страницу и за минуту понять, какая задача где, что сливается, какие тесты идут на ПК и на "
    "гитхабе, где что застряло и почему. Старый монитор не чинить. Покажи пример, как это будет "
    "выглядеть, потом задачи."
)
D_TURN2 = (
    "Да. Что уже расписано для ядра, делается один раз в ядре и настраивается; в задачах Hermes "
    "только его значения. Никаких имён репо и путей к моей папке прямо в тексте."
)
E_TURN2 = (
    "Полностью — значит все прогоны тестов, которые есть смысл перенести, в любой роли и любом "
    "инструменте и во всех репо: слияние, перепроверки, сборка карты, полный прогон перед очередью, "
    "тесты модов. ПК — только запасной путь, если гитхаб не стартовал."
)


def _cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _record(tag: str, i: int, turns=None) -> dict:
    writes = [
        {
            "argv": ["gh", "issue", "create", "--title", f"Ticket {tag}{i}"],
            "body": f"Why: body {tag}{i}, line one\nline two of {tag}{i}",
        }
    ]
    rec = {
        "run_id": f"RUN-{tag}-{i}",
        "arm": "old" if i % 2 == 0 else "new",
        "session_id": f"SESS-{tag}-{i}-7d31",
        "cost_usd": 0.4711 + i,
        "model": MODEL,
        "effort": EFFORT,
        "text": f"Owner-visible answer {tag}{i}: it says something.",
        "writes": writes,
    }
    if turns is not None:
        rec["turns"] = turns
    if turns == 2:
        rec["text2"] = f"Second turn answer {tag}{i}: a follow-up line."
    return rec


def _nine(tag: str = "a") -> list:
    """Three with turns=2, three with turns=1, three without turns."""
    modes = [2, 2, 2, 1, 1, 1, None, None, None]
    return [_record(tag, i, modes[i]) for i in range(9)]


def _six(tag: str = "a") -> list:
    return [_record(tag, i) for i in range(6)]


def _sections(text: str) -> dict:
    """Map each level-2 heading name to the text under it (test-side parser)."""
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


def _letter_to_run(key_path: Path, case: str) -> dict:
    return {e["letter"]: e["run_id"] for e in _key(key_path)["entries"] if e["case"] == case}


def _matching(outs: list, body: str) -> dict:
    found = [o for o in outs if o["text"] in body]
    assert len(found) == 1
    return found[0]


def test_nine_outputs_give_sections_A_to_I_and_nine_key_letters(tmp_path):
    outs = _nine()
    key_path = tmp_path / "key.json"
    path = build_case_files("a", outs, tmp_path, key_path, seed=11)
    sec = _sections(path.read_text(encoding="utf-8"))
    assert sorted(name for name in sec if len(name) == 1) == NINE
    letters = _letter_to_run(key_path, "a")
    assert sorted(letters) == NINE
    assert sorted(letters.values()) == sorted(o["run_id"] for o in outs)


def test_turn_headings_appear_only_where_the_record_says(tmp_path):
    outs = _nine()
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=11)
    sec = _sections(path.read_text(encoding="utf-8"))
    seen_kinds = {"two": 0, "one": 0, "none": 0}
    for letter in NINE:
        body = sec[letter]
        out = _matching(outs, body)
        turns = out.get("turns")
        if turns == 2:
            seen_kinds["two"] += 1
            assert body.index("### Turn 1") < body.index(out["text"]) < body.index("### Turn 2")
            assert body.index("### Turn 2") < body.index(out["text2"])
        elif turns == 1:
            seen_kinds["one"] += 1
            assert "### Turn 1" in body
            assert "### Turn 2" not in body
        else:
            seen_kinds["none"] += 1
            assert "### Turn" not in body
            assert body.strip().startswith(out["text"])
        if turns is not None:
            assert body.index("### Turn 1") < body.index(out["text"])
        for write in out["writes"]:
            assert write["body"] in body
    assert seen_kinds == {"two": 3, "one": 3, "none": 3}


def test_turn_two_text_is_under_turn_two_and_turn_one_writes_stay_in_turn_one(tmp_path):
    outs = _nine()
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=12)
    sec = _sections(path.read_text(encoding="utf-8"))
    for letter in NINE:
        body = sec[letter]
        out = _matching(outs, body)
        if out.get("turns") == 2:
            turn_two = body[body.index("### Turn 2"):]
            assert out["text2"] in turn_two
            assert out["writes"][0]["body"] not in turn_two
            assert out["writes"][0]["body"] in body[: body.index("### Turn 2")]


def test_nine_outputs_check_blind_is_clean(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _nine(), tmp_path, key_path, seed=13)
    assert check_blind(path, key_path) == []


def test_check_blind_catches_a_missing_letter_of_nine(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _nine(), tmp_path, key_path, seed=13)
    text = path.read_text(encoding="utf-8")
    cut = text.index("## I\n")
    path.write_text(text[:cut], encoding="utf-8")
    problems = check_blind(path, key_path)
    assert any("section I missing" in p for p in problems)


def test_six_outputs_still_build_six_sections_and_check_clean(tmp_path):
    key_path = tmp_path / "key.json"
    outs = _six()
    path = build_case_files("a", outs, tmp_path, key_path, seed=14)
    text = path.read_text(encoding="utf-8")
    sec = _sections(text)
    assert sorted(name for name in sec if len(name) == 1) == SIX
    assert "### Turn" not in text
    assert len(_key(key_path)["entries"]) == 6
    assert check_blind(path, key_path) == []


@pytest.mark.parametrize("count", [5, 7, 8, 10])
def test_counts_other_than_six_or_nine_raise_and_write_nothing(tmp_path, count):
    outs = [_record("x", i) for i in range(count)]
    with pytest.raises(ValueError):
        build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    assert not (tmp_path / "case-a.md").exists()
    assert not (tmp_path / "key.json").exists()


def test_turns_two_without_text2_raises_and_writes_nothing(tmp_path):
    outs = _nine()
    del outs[0]["text2"]
    with pytest.raises(ValueError):
        build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=1)
    assert not (tmp_path / "case-a.md").exists()
    assert not (tmp_path / "key.json").exists()


def test_nine_outputs_seeded_shuffle_is_deterministic_per_seed(tmp_path):
    key_path = tmp_path / "key.json"
    first = build_case_files("a", _nine(), tmp_path / "x", key_path, seed=21)
    mapping_first = _letter_to_run(key_path, "a")
    again = build_case_files("a", _nine(), tmp_path / "y", key_path, seed=21)
    mapping_again = _letter_to_run(key_path, "a")
    assert mapping_first == mapping_again
    assert first.read_text(encoding="utf-8") == again.read_text(encoding="utf-8")


def test_cases_a_d_e_carry_the_turn_two_answers_byte_for_byte():
    cases = _cases()
    assert cases["a"]["turn2_answer"] == A_TURN2
    assert cases["d"]["turn2_answer"] == D_TURN2
    assert cases["e"]["turn2_answer"] == E_TURN2


def test_cases_without_turn_two_answer_print_nothing_new(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("b", _six("b"), tmp_path, key_path, seed=31)
    text = path.read_text(encoding="utf-8")
    assert "Owner answer for turn 2" not in text
    sec = _sections(text)
    assert sec["Pass mark"].strip() == _cases()["b"]["pass_mark"]


def test_header_prints_turn_two_answer_after_pass_mark_for_case_a(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _six("a"), tmp_path, key_path, seed=32)
    text = path.read_text(encoding="utf-8")
    sec = _sections(text)
    assert sec["Pass mark"].strip() == _cases()["a"]["pass_mark"]
    assert sec["**Owner answer for turn 2:**"].strip() == A_TURN2
    assert text.index("## Pass mark") < text.index("**Owner answer for turn 2:**")
    assert text.index("**Owner answer for turn 2:**") < text.index("## A\n")
    assert check_blind(path, key_path) == []


def test_mark_key_pass_mark_r4_replaces_pass_mark_when_asked(tmp_path):
    cases = _cases()
    for name in ("a", "d", "e"):
        path = build_case_files(
            name, _six(name), tmp_path / name, tmp_path / "key.json", seed=33,
            mark_key="pass_mark_r4",
        )
        sec = _sections(path.read_text(encoding="utf-8"))
        assert sec["Pass mark"].strip() == cases[name]["pass_mark_r4"]
        assert cases[name]["pass_mark_r4"] != cases[name]["pass_mark"]
        assert sec["**Owner answer for turn 2:**"].strip() == cases[name]["turn2_answer"]


def test_default_mark_key_prints_pass_mark_not_r4(tmp_path):
    cases = _cases()
    for name in ("a", "d", "e"):
        path = build_case_files(name, _six(name), tmp_path / name, tmp_path / "key.json", seed=34)
        sec = _sections(path.read_text(encoding="utf-8"))
        assert sec["Pass mark"].strip() == cases[name]["pass_mark"]
        assert cases[name]["pass_mark_r4"] not in path.read_text(encoding="utf-8")


def test_mark_key_pass_mark_r4_falls_back_to_pass_mark_for_a_case_without_it(tmp_path):
    cases = _cases()
    path = build_case_files(
        "b", _six("b"), tmp_path, tmp_path / "key.json", seed=35, mark_key="pass_mark_r4"
    )
    sec = _sections(path.read_text(encoding="utf-8"))
    assert sec["Pass mark"].strip() == cases["b"]["pass_mark"]


def test_round_four_case_a_file_with_nine_outputs_is_blind(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files(
        "a", _nine(), tmp_path, key_path, seed=36, mark_key="pass_mark_r4"
    )
    assert check_blind(path, key_path) == []


def _arm_nine(tag: str = "a") -> list:
    """Nine outputs under the round-4 arms (empty, architect, full), three each, same turn shape as _nine."""
    arms = ["empty", "architect", "full"]
    outs = _nine(tag)
    for i, out in enumerate(outs):
        out["arm"] = arms[i % 3]
    return outs


def test_turn_two_writes_render_under_turn_two_after_text2(tmp_path):
    outs = _nine()
    outs[0]["writes2"] = [{"argv": ["gh", "issue", "close", "9101"], "body": "Second turn note zero."}]
    outs[1]["writes2"] = [{"argv": ["gh", "issue", "close", "9102"], "body": None}]
    path = build_case_files("a", outs, tmp_path, tmp_path / "key.json", seed=41)
    sec = _sections(path.read_text(encoding="utf-8"))
    checked = 0
    for letter in NINE:
        body = sec[letter]
        out = _matching(outs, body)
        if out.get("turns") != 2:
            assert "### Turn 2" not in body
            continue
        before, turn_two = body.split("### Turn 2", 1)
        if "writes2" in out:
            checked += 1
            assert turn_two.index(out["text2"]) < turn_two.index("gh issue close")
            for write in out["writes2"]:
                assert shlex.join(write["argv"]) in turn_two
                if write["body"]:
                    assert write["body"] in turn_two
                assert shlex.join(write["argv"]) not in before
            for write in out["writes"]:
                assert shlex.join(write["argv"]) in before
        else:
            assert "### Write" not in turn_two
    assert checked == 2


def test_arm_names_from_the_key_are_checked_in_headings_and_clean_file_passes(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _arm_nine(), tmp_path, key_path, seed=42)
    arms = {e["arm"] for e in _key(key_path)["entries"] if e["case"] == "a"}
    assert arms == {"empty", "architect", "full"}
    assert check_blind(path, key_path) == []


@pytest.mark.parametrize("arm", ["empty", "architect", "full"])
def test_arm_name_in_a_turn_heading_is_a_problem(tmp_path, arm):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _arm_nine(), tmp_path, key_path, seed=42)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("### Turn 1", f"### Turn 1 {arm}", 1), encoding="utf-8")
    problems = check_blind(path, key_path)
    assert any(arm in p and "heading" in p for p in problems)


@pytest.mark.parametrize("arm", ["empty", "architect", "full"])
def test_arm_name_in_the_case_header_is_a_problem(tmp_path, arm):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _arm_nine(), tmp_path, key_path, seed=42)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("# Case a: ", f"# Case a: {arm} ", 1), encoding="utf-8")
    problems = check_blind(path, key_path)
    assert any(arm in p for p in problems)


@pytest.mark.parametrize("arm", ["empty", "architect", "full"])
def test_arm_name_as_a_letter_section_title_is_a_problem(tmp_path, arm):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _arm_nine(), tmp_path, key_path, seed=42)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {arm}\n\nsome words\n")
    problems = check_blind(path, key_path)
    assert any(arm in p for p in problems)


def test_arm_name_in_plain_body_text_is_not_a_heading_problem(tmp_path):
    key_path = tmp_path / "key.json"
    path = build_case_files("a", _arm_nine(), tmp_path, key_path, seed=42)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\nthe full picture, empty of detail\n")
    assert check_blind(path, key_path) == []
