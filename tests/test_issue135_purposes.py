"""Issue 135, unit 1: `purposes` splits the brain's dollars by the purpose tag in its Bash / PowerShell calls.

Seam: `purpose_split` (a synthetic projects dir in, rows out) and `run_purposes` (the printed table). Every
expected value is written out by hand from the fixture below.

Prices: one million input tokens = $2.00, so 500_000 input tokens = $1.00 (`_usd(n)` = n dollars).
Window: the ISO week 2026-W42 (Monday 2026-10-12 .. Monday 2026-10-19); every stamp is in the middle of it.

Brain session B (custom title = the brain chat title), in file order:
  owner "hello"                                    (no ticket number in it, so the meter attributes no issue)
  b1  $1.00  no tag in force                       -> untagged
  b2  $2.00  Bash "# purpose: grill ..."           -> grill (the turn that carries the tag)
  b3  $3.00                                        -> grill
  owner "next topic"                               (ends the tag)
  b4  $4.00                                        -> untagged
  b5  $5.00  PowerShell "# purpose: ticket ..."    -> ticket
  b6  $6.00  Agent tool call (starts subagent aaa) -> ticket
  b7  $0.50                                        -> ticket
  subagent aaa: two turns $1.00 + $2.00            -> ticket (the purpose in force at b6)
Other sessions: W (an ORCH-like session, no brain title) $9.00, never counted.
Brain $ = 1 + 2 + 3 + 4 + 5 + 6 + 0.5 = 21.50; untagged 5.00, grill 5.00, ticket 11.50; subagent $3.00.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import token_meter  # noqa: E402
from tools.agentops.losses_cost import cost_buckets  # noqa: E402
from tools.agentops.purposes import PURPOSE_WORDS, purpose_split, run_purposes  # noqa: E402

SINCE = datetime(2026, 10, 12, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 19, tzinfo=timezone.utc)
PRICES = {"claude-sonnet-5": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.2, "output": 10.0}}

BRAIN = "bbbb1111-0000-4000-8000-000000000001"
OTHER = "cccc2222-0000-4000-8000-000000000002"


def _usd(dollars: float) -> dict:
    return {"input_tokens": int(dollars * 500_000), "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0}


def _stamp(minute: int) -> str:
    return f"2026-10-14T10:{minute:02d}:00Z"


def _owner(text: str, minute: int) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": _stamp(minute)}


def _turn(message_id: str, dollars: float, minute: int, tool: tuple[str, dict, str] | None = None) -> dict:
    content = [{"type": "text", "text": "ok"}]
    if tool is not None:
        name, tool_input, tool_id = tool
        content.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
    return {"type": "assistant", "timestamp": _stamp(minute),
            "message": {"id": message_id, "model": "claude-sonnet-5", "usage": _usd(dollars), "content": content}}


def _bash(command: str, tool_id: str) -> tuple[str, dict, str]:
    return ("Bash", {"command": command}, tool_id)


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _title(session: str, title: str) -> dict:
    return {"type": "custom-title", "customTitle": title, "sessionId": session}


def _brain_lines(first_tag: str = "grill") -> list[dict]:
    return [
        _title(BRAIN, token_meter.BRAIN_TITLE_LITERAL),
        _owner("hello", 0),
        _turn("b1", 1.00, 1),
        _turn("b2", 2.00, 2, _bash(f"# purpose: {first_tag}\nls", "tu_b2")),
        _turn("b3", 3.00, 3),
        _owner("next topic", 4),
        _turn("b4", 4.00, 5),
        _turn("b5", 5.00, 6, ("PowerShell", {"command": "# purpose: ticket\nGet-ChildItem"}, "tu_b5")),
        _turn("b6", 6.00, 7, ("Agent", {"subagent_type": "researcher-how", "prompt": "look"}, "tu_agent")),
        {"type": "user", "timestamp": _stamp(8),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_agent", "content": "started"}]},
         "toolUseResult": {"status": "async_launched", "agentId": "aaa"}},
        _turn("b7", 0.50, 9),
    ]


def _write_projects(projects: Path, first_tag: str = "grill") -> None:
    _write(projects / f"{BRAIN}.jsonl", _brain_lines(first_tag))
    sub = projects / BRAIN / "subagents"
    _write(sub / "agent-aaa.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "look into it"}, "timestamp": _stamp(8)},
        _turn("s1", 1.00, 10),
        _turn("s2", 2.00, 11),
    ])
    (sub / "agent-aaa.meta.json").write_text(json.dumps({"agentId": "aaa", "agentType": "researcher-how"}),
                                             encoding="utf-8")
    _write(projects / f"{OTHER}.jsonl", [_owner("work", 0), _turn("w1", 9.00, 1)])


@pytest.fixture()
def projects(tmp_path: Path) -> Path:
    path = tmp_path / "projects"
    _write_projects(path)
    return path


def test_each_turn_and_the_subagent_land_in_the_right_purpose(projects: Path):
    split = purpose_split(projects, SINCE, UNTIL, PRICES)
    rows = split.rows
    assert set(rows) == set(PURPOSE_WORDS) | {"untagged"}
    assert (rows["untagged"].turns, round(rows["untagged"].usd, 2)) == (2, 5.00)
    assert (rows["grill"].turns, round(rows["grill"].usd, 2)) == (2, 5.00)
    assert (rows["ticket"].turns, round(rows["ticket"].usd, 2)) == (5, 14.50)
    assert (rows["ticket"].subagent_turns, round(rows["ticket"].subagent_usd, 2)) == (2, 3.00)
    for word in ("closure-review", "notice", "owner", "research"):
        assert (rows[word].turns, rows[word].usd, rows[word].subagent_turns) == (0, 0.0, 0)
    assert rows["grill"].subagent_turns == 0 and rows["untagged"].subagent_turns == 0
    assert split.unknown_words == []


def test_total_equals_the_brain_dollars_of_losses_to_the_cent(projects: Path):
    split = purpose_split(projects, SINCE, UNTIL, PRICES)
    losses_brain = cost_buckets(projects, [], [], SINCE, UNTIL, PRICES).unowned_by_source["brain chat"]
    assert round(losses_brain, 2) == 24.50
    assert round(split.total_usd, 2) == round(losses_brain, 2)
    assert split.total_usd == pytest.approx(sum(row.usd for row in split.rows.values()))


def test_an_unknown_word_counts_as_untagged_and_is_listed_once(tmp_path: Path):
    projects = tmp_path / "p"
    _write_projects(projects, first_tag="gossip")
    split = purpose_split(projects, SINCE, UNTIL, PRICES)
    assert (split.rows["untagged"].turns, round(split.rows["untagged"].usd, 2)) == (4, 10.00)
    assert split.rows["grill"].turns == 0
    assert split.unknown_words == ["gossip"]


def test_a_new_tag_without_an_owner_message_replaces_the_one_in_force(tmp_path: Path):
    projects = tmp_path / "p"
    _write(projects / f"{BRAIN}.jsonl", [
        _title(BRAIN, token_meter.BRAIN_TITLE_LITERAL), _owner("hello", 0),
        _turn("c1", 1.00, 1, _bash("# purpose: grill\nls", "t1")),
        _turn("c2", 2.00, 2),
        _turn("c3", 3.00, 3, _bash("# purpose: research\nls", "t2")),
        _turn("c4", 4.00, 4),
    ])
    rows = purpose_split(projects, SINCE, UNTIL, PRICES).rows
    assert (round(rows["grill"].usd, 2), round(rows["research"].usd, 2)) == (3.00, 7.00)


def test_only_a_command_that_starts_with_the_tag_line_counts(tmp_path: Path):
    projects = tmp_path / "p"
    _write(projects / f"{BRAIN}.jsonl", [
        _title(BRAIN, token_meter.BRAIN_TITLE_LITERAL), _owner("hello", 0),
        _turn("c1", 1.00, 1, _bash("echo hi\n# purpose: grill", "t1")),
        _turn("c2", 2.00, 2, ("Read", {"command": "# purpose: grill"}, "t2")),
        _turn("c3", 3.00, 3),
    ])
    rows = purpose_split(projects, SINCE, UNTIL, PRICES).rows
    assert round(rows["untagged"].usd, 2) == 6.00 and rows["grill"].turns == 0


def test_a_tool_result_or_a_task_notification_is_not_an_owner_message(tmp_path: Path):
    projects = tmp_path / "p"
    _write(projects / f"{BRAIN}.jsonl", [
        _title(BRAIN, token_meter.BRAIN_TITLE_LITERAL), _owner("hello", 0),
        _turn("c1", 1.00, 1, _bash("# purpose: notice\nls", "t1")),
        {"type": "user", "timestamp": _stamp(2),
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
        _owner("<task-notification>\n<task-id>x</task-id>\n</task-notification>", 3),
        {"type": "user", "isMeta": True, "timestamp": _stamp(4),
         "message": {"role": "user", "content": "Another Claude session sent a message"}},
        _turn("c2", 2.00, 5),
    ])
    rows = purpose_split(projects, SINCE, UNTIL, PRICES).rows
    assert round(rows["notice"].usd, 2) == 3.00


def test_a_subagent_of_a_call_before_any_tag_is_untagged(tmp_path: Path):
    projects = tmp_path / "p"
    lines = _brain_lines()
    _write(projects / f"{BRAIN}.jsonl", [lines[0], lines[1], _turn("a0", 1.00, 1,
           ("Agent", {"prompt": "x"}, "tu_agent")), lines[9]])
    sub = projects / BRAIN / "subagents"
    _write(sub / "agent-aaa.jsonl", [_owner("go", 2), _turn("s1", 2.00, 3)])
    (sub / "agent-aaa.meta.json").write_text(json.dumps({"agentId": "aaa", "agentType": "worker"}),
                                             encoding="utf-8")
    rows = purpose_split(projects, SINCE, UNTIL, PRICES).rows
    assert (rows["untagged"].subagent_turns, round(rows["untagged"].subagent_usd, 2)) == (1, 2.00)


def _run(projects: Path, tmp_path: Path, capsys, **kwargs) -> tuple[int, str]:
    settings = {"loss_repos": [{"name": ROOT.name, "root": str(tmp_path), "projects_dir": str(projects)}]}
    args = argparse.Namespace(week="2026-W42", since=None, until=None)
    code = run_purposes(args, settings=settings, prices=PRICES, **kwargs)
    return code, capsys.readouterr().out


def test_the_table_prints_one_row_per_purpose_then_untagged_then_total(projects: Path, tmp_path: Path, capsys):
    code, out = _run(projects, tmp_path, capsys)
    assert code == 0
    lines = out.strip().splitlines()
    first_cells = [line.split("|")[0].strip() for line in lines]
    labels = [*PURPOSE_WORDS, "untagged", "total"]
    start = first_cells.index(PURPOSE_WORDS[0])
    assert first_cells[start:start + len(labels)] == labels
    by_label = {cells: line for cells, line in zip(first_cells, lines)}
    assert "$24.50" in by_label["total"]
    assert "$14.50" in by_label["ticket"] and "59%" in by_label["ticket"]
    assert "2026-W42" in lines[0]


def test_the_unknown_word_is_named_under_the_table(tmp_path: Path, capsys):
    projects = tmp_path / "p"
    _write_projects(projects, first_tag="gossip")
    code, out = _run(projects, tmp_path, capsys)
    assert code == 0
    assert out.count("gossip") == 1
    assert out.strip().splitlines()[-1].endswith("gossip")


def test_a_bad_week_is_exit_2(projects: Path, tmp_path: Path, capsys):
    settings = {"loss_repos": [{"name": ROOT.name, "root": str(tmp_path), "projects_dir": str(projects)}]}
    code = run_purposes(argparse.Namespace(week="nonsense", since=None, until=None), settings=settings, prices=PRICES)
    assert code == 2
    assert "error" in capsys.readouterr().out


def test_since_and_until_replace_the_week(projects: Path, tmp_path: Path, capsys):
    settings = {"loss_repos": [{"name": ROOT.name, "root": str(tmp_path), "projects_dir": str(projects)}]}
    args = argparse.Namespace(week=None, since="2026-10-14T10:05:00+00:00", until="2026-10-14T10:08:30+00:00")
    code = run_purposes(args, settings=settings, prices=PRICES)
    out = capsys.readouterr().out
    assert code == 0
    # b4 (10:05) .. b6 (10:07) are inside: 4 + 5 + 6 = 15
    assert "$15.00" in out
