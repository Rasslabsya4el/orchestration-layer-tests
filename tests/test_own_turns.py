"""Target ticket #645: scripts/own_turns.py -- 10 fake turns, the per-role table hand-counted."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import own_turns as ot  # noqa: E402
import token_meter as tm  # noqa: E402

SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 8, tzinfo=timezone.utc)


def _turn(i: int, tool: tuple[str, dict] | None, cache_read: int, model="claude-sonnet-5-5", output=0, day=3):
    content = [{"type": "text", "text": "x"}]
    if tool:
        content = [{"type": "tool_use", "id": f"tu{i}", "name": tool[0], "input": tool[1]}]
    return {"type": "assistant", "timestamp": f"2026-10-0{day}T10:{i:02d}:00Z",
            "message": {"id": f"m{i}", "model": model, "content": content,
                        "usage": {"input_tokens": 0, "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": 0, "output_tokens": output}}}


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")


@pytest.fixture()
def projects(tmp_path: Path) -> Path:
    bash = lambda c: ("Bash", {"command": c})  # noqa: E731
    orch = [{"type": "custom-title", "customTitle": "orch repo #7"}] + [
        _turn(1, bash("cd /x && cat a.txt | head"), 1_000_000),
        _turn(2, bash("grep -n foo a.py"), 1_000_000),
        _turn(3, ("Read", {"file_path": "/a"}), 1_000_000),
        _turn(4, bash("gh issue view 1"), 1_000_000),
        _turn(5, bash("python -m pytest t"), 1_000_000),
        _turn(6, bash("python scripts/job_run.py submit"), 1_000_000),
        _turn(7, bash("python scripts/foo.py"), 1_000_000),
    ]
    brain = [{"type": "custom-title", "customTitle": tm.BRAIN_TITLE_LITERAL}] + [
        _turn(8, bash('python -c "print(1)"'), 2_000_000),
        _turn(9, bash("git status"), 2_000_000),
        _turn(10, None, 2_000_000),
    ]
    _write(tmp_path / "orch1.jsonl", orch)
    # failed starts (#645 a): a <synthetic> turn and a 0-token turn in a session of its own, plus one inside orch1
    orch.append(_turn(11, None, 5_000_000, model="<synthetic>"))
    orch.append(_turn(12, None, 0))
    _write(tmp_path / "orch1.jsonl", orch)
    _write(tmp_path / "orch2.jsonl", [{"type": "custom-title", "customTitle": "orch repo #8"},
                                      _turn(13, None, 0, model="<synthetic>")])
    _write(tmp_path / "brain1.jsonl", brain)
    # a find spawn under the ORCH session: 2 turns, 2 tool calls, priced as haiku-5-5
    spawn = [_turn(21, ("Read", {}), 90_000, output=10_000), _turn(22, ("Grep", {}), 90_000, output=10_000)]
    _write(tmp_path / "orch1" / "subagents" / "agent-a.jsonl", spawn)
    (tmp_path / "orch1" / "subagents" / "agent-a.meta.json").write_text(
        json.dumps({"agentType": "researcher-find"}), encoding="utf-8")
    return tmp_path


def test_classify_command():
    c = ot.classify_command
    assert c("cd /x && sed -n 1,5p f") == "reads"
    assert c("sed -i s/a/b/ f") == "other"
    assert c("python -c \"open('f','w').write('x'); print(1)\"") == "other"
    assert c("python -m tools.agentops dispatch") == "plumbing"
    assert c("python scripts/load_gauge.py --need 1") == "runs"
    assert c("ls | wc -l") == "reads"


def test_per_role_table_hand_counted(projects: Path):
    prices = tm.load_prices(tm.DEFAULT_PRICES_PATH)
    stats, zero, haiku = ot.collect(projects, prices, SINCE, UNTIL, {"orch", "brain"}, None)
    o, b = stats["orch"], stats["brain"]
    assert o.counts == {"reads": 2, "tool reads": 1, "plumbing": 2, "runs": 1, "other": 1, "no command": 0}
    assert b.counts == {"reads": 1, "tool reads": 0, "plumbing": 1, "runs": 0, "other": 0, "no command": 1}
    # sonnet-5-5 cache read $0.10/M: 1M = $0.10 per ORCH turn, 2M = $0.20 per brain turn
    assert o.read_money == pytest.approx(0.30) and o.money["plumbing"] == pytest.approx(0.20)
    assert b.read_money == pytest.approx(0.20) and b.money["no command"] == pytest.approx(0.20)
    assert zero == {"orch": 0, "brain": 1}
    costs, calls = haiku[0], haiku[1]
    # per spawn turn: 90k*0.01/M + 10k*0.5/M = $0.0059; two turns $0.0118; 2 tool calls
    assert costs == [pytest.approx(0.0118)] and calls == [2]
    assert haiku[3] == [2]  # tool_use blocks, reference only
    assert o.excluded_turns == 3 and o.excluded_sessions == 1 and o.unpriced == 0
    assert len(o.sessions) == 1
    text = ot.render(stats, zero, haiku, ["orch", "brain"])
    assert "$0.02 = median find spawn $0.01 x 3 read turns / 2 API calls" in text
    assert "excluded (<synthetic>/no model/0 tokens): 3 turns, 1 sessions" in text
    assert "median find spawn at Haiku 5.5: $0.0118" in text
    assert "sessions with 0 researcher spawns: 0 of 1" in text
    assert "sessions with 0 researcher spawns: 1 of 1" in text


# #78: a turn whose every command only reads is reads (a file lookup in any position: first, behind an
# assignment, in a loop body, after timeout N, after cd). A turn that might write (a redirect, tee, rm,
# a write call) is never reads. Waits, probes and process queries are not reads.
READ_TURNS = [
    "python -c \"\nimport json\nprint(json.load(open('x.json')))\"",
    "python - <<'EOF'\nimport json\nprint(1)\nEOF",
    "python -c \"import json, re\nprint(json.dumps(1), re.I)\"",
    "python -c \"import json\nprint(json.load(open('artifacts/x.json')))\"",
    "W=/c/Coding/x; sed -n 1,76p $W/src/a.py; grep -n 'def' $W/t.py",
    "cd /x && cat a.txt | head",
    "timeout 5 cat f",
    "for f in a b; do head -1 $f; done",
    "W=/c/x; cat $W/a",
    "cat a 2>&1 | tail -3",
    # a write word inside a read command's arguments is a name, not a write
    "grep -n pytest scripts/job_run.py tests/x.py | head -5",
    "cat var/a.log | grep start | tail -3",
    "ls var 2>/dev/null",
    "Get-ChildItem var > $null",
    "Get-Content x | Select-String y",
    "Test-Path var/x",
    "powershell -NoProfile -Command \"Get-Content x | Select-String y\"",
]
PLUMBING_TURNS = [
    # gh and git are plumbing in every position, never reads
    "W=/c/Coding/x; git -C $W diff f50 --stat",
    "W=/c/Coding/x; git -C $W merge --no-edit origin/orch-432",
    "cd /x && git status",
    "timeout 60 gh issue view 17 --json state --jq '.state'",
    "for n in 569 553; do gh api repos/o/r/issues/$n/dependencies/blocked_by --jq '.[] | \"x\"'; done",
    "for n in 544 564; do echo \"=== #$n\"; gh issue view $n --json title; done",
    "for n in 470 477; do gh issue view $n; done; python -m tools.agentops merge origin/orch-470",
    # the first command keeps its class when another segment does not read
    "gh issue view 1 --json title; cat f",
]
OTHER_TURNS = [
    # wait loops and polls: sleep, seq, true, tr and printf are not reads
    "for i in $(seq 1 24); do tail -1 var/x.log; sleep 5; done",
    "F=var/r.json; for i in $(seq 1 55); do [ -s $F ] && break; sleep 10; done; cat $F",
    "python -c \"import time\ntime.sleep(5)\nprint(1)\"",
    # non-file probes: tasklist, Get-CimInstance, a python run that imports repo code
    "tasklist //FI \"PID eq 13844\" | tail -1; ls var/agentops/",
    "powershell -NoProfile -Command \"(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB\"",
    "PYTHONPATH=src python -c \"from acme import x\nprint(x.y)\"",
    "python -c \"import json, subprocess\nprint(1)\"",
    # python snippets that write
    "python -c \"import json\nopen('artifacts/out.txt', 'w').write('x')\nprint(1)\"",
    "python -c \"import json\nopen('f','w').write('x')\nprint(1)\"",
    "python -c \"import json\nopen(p, mode='w')\nprint(1)\"",
    "python -c \"from pathlib import Path\nPath(p).open('w')\nprint(1)\"",
    "python -c \"import os\nos.makedirs('x')\nprint(1)\"",
    "python -c \"import os\nos.replace('a', 'b')\nprint(1)\"",
    "timeout 120 python scripts/token_meter.py --orch-threshold 2>&1 | tail -8",
    "sed -i 's/a/b/' f && cat f",
    "sed -n -i 's/a/b/' f",
    "sed -n --in-place 's/a/b/' f",
    # heredoc: the redirect, pipe and chained rm after the << line still count
    "python - <<'EOF' > out.txt\nimport json\nprint(1)\nEOF",
    "python - <<'EOF' | tee f\nimport json\nprint(1)\nEOF",
    "python - <<'EOF' && rm x\nimport json\nprint(1)\nEOF",
    # redirects: 2>file, 1>file, &>file, >file
    "cat a.txt > out.txt",
    "cat a.txt 2>err.txt",
    "cat a.txt 1>out.txt",
    "cat a.txt &>out.txt",
    "cat a.txt | tee f",
    "cat a.txt && rm x",
    "Get-ChildItem var | Out-File x",
    "cat a.txt; python scripts/job_run.py submit",
    "cat a; powershell -NoProfile -Command \"Stop-Process -Id 12\"",
    "find var -name x -delete",
    "find var -exec rm {} ;",
    # PowerShell: allowlist only, so these are not reads
    "powershell -NoProfile -Command \"Export-Csv x y\"",
    "powershell -NoProfile -Command \"Compress-Archive a b\"",
    "powershell -NoProfile -Command \"Expand-Archive a b\"",
    "powershell -NoProfile -Command \"Update-Thing x\"",
    "powershell -NoProfile -Command \"Install-Package x\"",
    "powershell -NoProfile -Command \"Get-Content x | Set-Content y\"",
    "powershell -NoProfile -Command \"Stop-Process -Id 12\"",
    "ssh buildbox \"Get-Content x\"",
]


@pytest.mark.parametrize("command", READ_TURNS)
def test_reading_turn_is_reads(command: str):
    assert ot.classify_command(command) == "reads"


@pytest.mark.parametrize("command", PLUMBING_TURNS)
def test_gh_and_git_turn_is_plumbing(command: str):
    assert ot.classify_command(command) == "plumbing"


@pytest.mark.parametrize("command", OTHER_TURNS)
def test_writing_running_or_waiting_turn_stays_other(command: str):
    assert ot.classify_command(command) == "other"


def test_command_head_keys():
    h = ot.command_head
    assert h("cd /x && python -c \"print(1)\"") == "python -c"
    assert h("W=/c/Coding/x; git -C $W diff") == "git"
    assert h("PYTHONPATH=src python -c \"x\"") == "python -c"
    assert h("for n in 1 2; do gh view; done") == "gh"
    assert h("timeout 60 gh issue view 1") == "gh"


def test_table_text_names_every_read_word_and_the_rule():
    for word in ot.READ_WORDS:
        assert word in ot.TABLE_TEXT
    assert "a turn counts by its first command" in ot.TABLE_TEXT
    assert "every command only reads counts as reads" in ot.TABLE_TEXT


def test_top_other_lists_commonest_first(projects: Path):
    prices = tm.load_prices(tm.DEFAULT_PRICES_PATH)
    stats, zero, haiku = ot.collect(projects, prices, SINCE, UNTIL, {"orch"}, None)
    text = ot.render(stats, zero, haiku, ["orch"], top_other=3)
    assert "top 3 first commands of the other class (ORCH):" in text
    assert "       1  python scripts/foo.py" in text
