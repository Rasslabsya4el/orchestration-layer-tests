"""Issue #376 unit 5: the blind judge of the C-arm comparison (tools/replays/c_blind.py).

build_pair writes one judge prompt (ticket exactly as given, two diffs as X and Y, rubric) and appends the letter
key to a key file that never enters the prompt. Diffs are scrubbed of commit blocks and header lines; leaks checks
what remains of path, header and message lines and of side labels. parse_verdict reads the last VERDICT line;
reveal refuses until all six verdicts are in.
"""

import json
from pathlib import Path

import pytest

from tools.replays.c_blind import (
    KeyLocked,
    _scrub_diff,
    build_pair,
    judge_command,
    leaks,
    parse_verdict,
    reveal,
)

NUMBERS = (349, 219, 135, 218, 127, 238)
TICKET = "Add a fan-out of seven units.\nAcceptance: the suite is green."
DIFF_S = (
    "diff --git a/tools/x.py b/tools/x.py\n"
    "index 1a2b3c4..5d6e7f8 100644\n"
    "--- a/tools/x.py\n"
    "+++ b/tools/x.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new line from side one\n"
)
DIFF_C = (
    "diff --git a/tools/x.py b/tools/x.py\n"
    "index 9f8e7d6..0a1b2c3 100644\n"
    "--- a/tools/x.py\n"
    "+++ b/tools/x.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+alt line from side two\n"
)


def _build(tmp_path: Path, number: int = 349, seed: int = 0, key: Path | None = None,
           ticket: str = TICKET, diff_s: str = DIFF_S, diff_c: str = DIFF_C) -> tuple[Path, Path]:
    key = key or tmp_path / "key.json"
    out = tmp_path / "out"
    path = build_pair(number, ticket, diff_s, diff_c, out, key, seed)
    return path, key


def _key(key_path: Path) -> dict:
    return json.loads(key_path.read_text(encoding="utf-8"))


def test_same_seed_gives_same_order(tmp_path):
    _, key_a = _build(tmp_path / "a", seed=7)
    _, key_b = _build(tmp_path / "b", seed=7)
    assert _key(key_a) == _key(key_b)


def test_both_orders_occur_across_seeds(tmp_path):
    orders = set()
    for seed in range(20):
        _, key = _build(tmp_path / f"s{seed}", seed=seed)
        orders.add(_key(key)["349"]["X"])
    assert orders == {"S", "C"}


def test_key_is_not_in_prompt(tmp_path):
    prompt_path, key_path = _build(tmp_path, seed=3)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert str(key_path) not in prompt
    assert '"X"' not in prompt and '"Y"' not in prompt
    assert key_path.read_text(encoding="utf-8") not in prompt


def test_prompt_file_name_and_rubric(tmp_path):
    prompt_path, _ = _build(tmp_path, number=349, seed=1)
    assert prompt_path.name == "judge-349.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    assert TICKET in prompt
    assert "new line from side one" in prompt
    assert "alt line from side two" in prompt
    for word in ("fit to the goal", "tests", "scope", "simplicity", "file:line"):
        assert word in prompt
    for verdict in ("VERDICT: X", "VERDICT: Y", "VERDICT: tie"):
        assert verdict in prompt


def test_ticket_is_written_exactly_as_given(tmp_path):
    ticket = "ORCH worker layer baseline: the orchestration layer runs one worker per unit.  \n\n"
    prompt_path, _ = _build(tmp_path, ticket=ticket, seed=4)
    assert ticket in prompt_path.read_text(encoding="utf-8")


def test_path_lines_with_side_words_build(tmp_path):
    diff = (
        "diff --git a/.claude/agents/worker.md b/.claude/agents/worker.md\n"
        "--- a/.claude/agents/worker.md\n"
        "+++ b/.claude/agents/worker.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/docs/agents/orch.md b/docs/agents/orch.md\n"
        "--- a/docs/agents/orch.md\n"
        "+++ b/docs/agents/orch.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+alt\n"
    )
    prompt_path, _ = _build(tmp_path, seed=5, diff_s=diff, diff_c=diff)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "diff --git a/.claude/agents/worker.md b/.claude/agents/worker.md" in prompt
    assert "--- a/docs/agents/orch.md" in prompt


def test_path_line_checks_own_branch_only_for_any_number(tmp_path):
    assert leaks("diff --git a/.claude/agents/worker.md b/.claude/agents/worker.md") == []
    assert leaks("--- a/docs/agents/orch.md") == []
    assert leaks("diff --git a/i1.py b/i1.py") == []
    assert "c-127" in leaks("diff --git a/x c-127")


def test_git_log_message_block_is_scrubbed(tmp_path):
    block = (
        "commit 0123456789abcdef0123456789abcdef01234567\n"
        "Author: someone\n"
        "Date: Mon Oct 19 2026\n"
        "\n"
        "    Merge i127 into main\n"
        "    fixes 0123abc of the fan-out\n"
        "\n"
    )
    diff_c = block + DIFF_C
    scrubbed = _scrub_diff(diff_c, 127)
    assert leaks(scrubbed) == []
    assert "Merge i127" not in scrubbed
    assert "0123456789" not in scrubbed and "0123abc" not in scrubbed
    assert "+alt line from side two" in scrubbed
    prompt_path, _ = _build(tmp_path, number=127, seed=6, diff_c=diff_c)
    assert "Merge i127" not in prompt_path.read_text(encoding="utf-8")


def test_removed_double_dash_line_is_body(tmp_path):
    diff_s = DIFF_S + "--- comment about the worker\n"
    prompt_path, _ = _build(tmp_path, seed=7, diff_s=diff_s)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "--- comment about the worker" in prompt


def test_header_co_authored_and_own_branch_are_scrubbed(tmp_path):
    diff_s = "Branch: c-127\n" + DIFF_S + "Co-Authored-By: Claude Haiku 5.5\n"
    prompt_path, _ = _build(tmp_path, number=127, seed=2, diff_s=diff_s)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Co-Authored-By" not in prompt
    assert "c-127" not in prompt
    assert "Branch: <branch>" in prompt
    assert "+new line from side one" in prompt


def test_commit_header_lines_are_dropped(tmp_path):
    diff_c = (
        "commit 0123456789abcdef0123456789abcdef01234567\n"
        "Author: someone\n"
        "Date: Mon Oct 19 2026\n"
        "Subject: fix the fan-out\n"
        + DIFF_C
    )
    prompt_path, _ = _build(tmp_path, seed=6, diff_c=diff_c)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "0123456789abcdef" not in prompt
    assert "Author:" not in prompt and "Subject:" not in prompt and "Date:" not in prompt
    assert "+alt line from side two" in prompt


def test_other_numbers_branch_in_a_header_is_refused(tmp_path):
    diff_s = "Branch: c-349\n" + DIFF_S
    with pytest.raises(ValueError):
        _build(tmp_path, number=127, diff_s=diff_s)


def test_own_number_only_and_code_tokens_untouched(tmp_path):
    diff_s = "Branch: i1\n" + DIFF_S + "+x = i1\n"
    prompt_path, _ = _build(tmp_path, number=1, seed=8, diff_s=diff_s)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Branch: <branch>" in prompt
    assert "+x = i1" in prompt


def test_side_words_in_code_body_are_not_refused(tmp_path):
    diff_s = DIFF_S + "+ORCH worker layer baseline\n"
    prompt_path, _ = _build(tmp_path, seed=9, diff_s=diff_s)
    assert "+ORCH worker layer baseline" in prompt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("label", ["S: merged by side one", "C:"])
def test_leftover_side_label_line_is_refused(tmp_path, label):
    diff_c = DIFF_C + label + "\n"
    with pytest.raises(ValueError):
        _build(tmp_path, diff_c=diff_c)


def test_side_word_in_a_header_line_is_refused(tmp_path):
    diff_s = "Notes: the worker layer\n" + DIFF_S
    with pytest.raises(ValueError):
        _build(tmp_path, diff_s=diff_s)


def test_key_is_appended_and_not_overwritten(tmp_path):
    key = tmp_path / "key.json"
    _build(tmp_path / "one", number=349, seed=0, key=key)
    _build(tmp_path / "two", number=219, seed=1, key=key)
    data = _key(key)
    assert set(data) == {"349", "219"}
    assert set(data["349"]) == {"X", "Y"}
    assert sorted([data["349"]["X"], data["349"]["Y"]]) == ["C", "S"]
    with pytest.raises(ValueError):
        _build(tmp_path / "three", number=349, seed=5, key=key)


def test_leaks_finds_header_and_label_words():
    found = leaks("Subject: the worker layer ORCH\nbaseline line")
    for word in ("worker", "layer", "ORCH", "baseline"):
        assert word in found
    assert leaks("+the worker layer ORCH baseline\n a context line") == []
    assert "S:" in leaks("S: merged\n+x")
    assert "c-127" in leaks("Branch: c-127")


def test_judge_command_is_headless_and_toolless(tmp_path):
    prompt_file = tmp_path / "judge-349.md"
    prompt_file.write_text("x", encoding="utf-8")
    argv = judge_command(prompt_file)
    assert argv[0] == "claude"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "-p" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv
    assert "--agent" not in argv


def test_judge_command_needs_the_prompt_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        judge_command(tmp_path / "missing.md")


@pytest.mark.parametrize(
    "text, want",
    [
        ("analysis\nVERDICT: X", "X"),
        ("analysis\nVERDICT: Y", "Y"),
        ("analysis\nVERDICT: tie", "tie"),
        ("VERDICT: X\nmore\nVERDICT: tie", "tie"),
        ("text\\nVERDICT: Y\\n", "Y"),
    ],
)
def test_parse_verdict_takes_the_last_line(text, want):
    assert parse_verdict(text) == want


@pytest.mark.parametrize("text", ["no verdict here", "VERDICT: Z", ""])
def test_parse_verdict_refuses_when_none(text):
    with pytest.raises(ValueError):
        parse_verdict(text)


def test_reveal_refuses_with_five_verdicts(tmp_path):
    key = tmp_path / "key.json"
    for i, number in enumerate(NUMBERS):
        _build(tmp_path / str(i), number=number, seed=i, key=key)
    five = {n: "X" for n in NUMBERS[:5]}
    with pytest.raises(KeyLocked):
        reveal(key, five)


def test_reveal_works_with_six_verdicts(tmp_path):
    key = tmp_path / "key.json"
    for i, number in enumerate(NUMBERS):
        _build(tmp_path / str(i), number=number, seed=i, key=key)
    data = _key(key)
    verdicts = {n: "X" for n in NUMBERS}
    verdicts[NUMBERS[0]] = "tie"
    result = reveal(key, verdicts)
    assert set(result) == set(NUMBERS)
    assert result[NUMBERS[0]] == "tie"
    for number in NUMBERS[1:]:
        assert result[number] == data[str(number)]["X"]
    assert all(v in {"S", "C", "tie"} for v in result.values())


def test_reveal_takes_a_custom_expected_set(tmp_path):
    key = tmp_path / "key.json"
    for i, number in enumerate(NUMBERS[:2]):
        _build(tmp_path / str(i), number=number, seed=i, key=key)
    result = reveal(key, {349: "Y", 219: "X"}, expected=frozenset({349, 219}))
    assert set(result) == {349, 219}
