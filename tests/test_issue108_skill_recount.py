"""Issue #108 (unit 1 of 2): scripts/skill_recount.py repeats the #102 count of skill claims.

Synthetic subagent transcripts are written under tmp_path and the CLI is driven in-process with
--projects-dir, so the real ~/.claude/projects is never read. The claim rules follow the hook
tools/hooks/subagent_skill_check.py (its label line, no-claim values, fenced and quoted lines, tail).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import skill_recount  # noqa: E402

PROJECT = "C--Coding-orchestration-layer"
START = "2026-10-20T00:00:00Z"


def _write_unit(projects: Path, folder: str, session: str, agent_id: str, role: str, lines: list[dict]) -> None:
    sub = projects / folder / session / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": role}), encoding="utf-8")
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    (sub / f"agent-{agent_id}.jsonl").write_text(body, encoding="utf-8")


def _prompt(ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": "Do #108"}}


def _text(ts: str, text: str, msg_id: str = "msg-final") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool(ts: str, name: str, tool_input: dict, msg_id: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"toolu-{msg_id}", "name": name, "input": tool_input}],
        },
    }


def _run_json(capsys, projects: Path, repos: list[str], *extra: str) -> dict:
    argv = ["--projects-dir", str(projects), "--json", *extra]
    for repo in repos:
        argv += ["--repo", repo]
    assert skill_recount.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_recount_counts_exact_units(tmp_path, capsys):
    projects = tmp_path / "projects"
    # A: worker, code edit, claims tdd without any Skill call -> one claim, one false claim
    _write_unit(projects, PROJECT, "s1", "a-false", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/scripts/foo.py"}, "m1"),
        _text("2026-10-21T10:05:00Z", "Done.\ntdd: called (red first)"),
    ])
    # B: worker-strong, code edit, tdd Skill call (prefixed name) and a true claim
    _write_unit(projects, PROJECT, "s1", "b-true", "worker-strong", [
        _prompt("2026-10-21T11:00:00Z"),
        _tool("2026-10-21T11:00:30Z", "Skill", {"skill": "mattpocock-skills:tdd"}, "m1"),
        _tool("2026-10-21T11:02:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/tests/test_x.py"}, "m2"),
        _text("2026-10-21T11:09:00Z", "tdd: called\nDone."),
    ])
    # C: worker, code edit, no tdd line and no Skill call -> silent
    _write_unit(projects, PROJECT, "s2", "c-silent", "worker", [
        _prompt("2026-10-22T09:00:00Z"),
        _tool("2026-10-22T09:01:00Z", "Write", {"file_path": "C:/Coding/orchestration-layer/scripts/bar.py"}, "m1"),
        _text("2026-10-22T09:05:00Z", "Done. Tests pass."),
    ])
    # D: worker, first timestamp before START but inside the window -> left_out, its claim is not counted
    _write_unit(projects, PROJECT, "s2", "d-pre-start", "worker", [
        _prompt("2026-10-18T09:00:00Z"),
        _tool("2026-10-18T09:01:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/scripts/old.py"}, "m1"),
        _text("2026-10-18T09:05:00Z", "tdd: called"),
    ])
    # E: researcher-how, ignored even with a code edit
    _write_unit(projects, PROJECT, "s2", "e-research", "researcher-how", [
        _prompt("2026-10-21T12:00:00Z"),
        _tool("2026-10-21T12:01:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/scripts/baz.py"}, "m1"),
        _text("2026-10-21T12:05:00Z", "Done."),
    ])
    # F: a folder whose name only starts like the project; it must never be read
    _write_unit(projects, PROJECT + "-old", "s1", "f-prefix", "worker", [
        _prompt("2026-10-21T13:00:00Z"),
        _tool("2026-10-21T13:01:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/scripts/qux.py"}, "m1"),
        _text("2026-10-21T13:05:00Z", "Done."),
    ])

    result = _run_json(capsys, projects, [f"home={PROJECT}@{START}"])

    repo = result["repos"][0]
    assert repo["repo"] == "home"
    assert repo["units"] == 3
    assert repo["code_edit_units"] == 3
    assert repo["false_claims"] == 1
    assert repo["silent"] == 1
    assert repo["silent_pct"] == 33.3
    assert repo["left_out"] == 1
    assert repo["claims_by_skill"] == {"tdd": {"worker": 1, "worker-strong": 1}}
    assert repo["false_claims_by_skill"] == {"tdd": {"worker": 1, "worker-strong": 0}}
    assert result["total"]["units"] == 3
    assert result["total"]["left_out"] == 1


def test_claim_rules_and_code_edit_path(tmp_path, capsys):
    projects = tmp_path / "projects"
    folder = "C--Coding-x"
    # G: worker, code edit; a wrong-cause diagnosis and two tdd lines, no Skill call:
    # false once per skill per unit -> diagnosing-bugs 1, tdd 1
    _write_unit(projects, folder, "s1", "g", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/x/scripts/a.py"}, "m1"),
        _text("2026-10-21T10:05:00Z", "diagnosis: wrong sign in the sum\ntdd: called\ntdd: called"),
    ])
    # H: worker, edit only in a scratchpad path (not a code-edit unit); "not a defect" is no claim
    _write_unit(projects, folder, "s1", "h", "worker", [
        _prompt("2026-10-21T11:00:00Z"),
        _tool("2026-10-21T11:01:00Z", "Write",
              {"file_path": "C:/Users/user/AppData/Local/Temp/scratchpad/notes.py"}, "m1"),
        _text("2026-10-21T11:05:00Z", "diagnosis: not a defect"),
    ])
    # I: worker-strong, code edit; diagnosing-bugs called (unprefixed), tdd skipped with a reason -> clean
    _write_unit(projects, folder, "s1", "i", "worker-strong", [
        _prompt("2026-10-21T12:00:00Z"),
        _tool("2026-10-21T12:00:30Z", "Skill", {"skill": "diagnosing-bugs"}, "m1"),
        _tool("2026-10-21T12:01:00Z", "Edit", {"file_path": "C:/Coding/x/scripts/b.ps1"}, "m2"),
        _text("2026-10-21T12:05:00Z", "diagnosis: stale cache key\ntdd skipped: docs only"),
    ])

    result = _run_json(capsys, projects, [f"x={folder}"])

    repo = result["repos"][0]
    assert repo["units"] == 3
    assert repo["code_edit_units"] == 2
    assert repo["false_claims"] == 2
    assert repo["silent"] == 0
    assert repo["silent_pct"] == 0.0
    assert repo["left_out"] == 0
    assert repo["claims_by_skill"] == {
        "diagnosing-bugs": {"worker": 1, "worker-strong": 1},
        "tdd": {"worker": 1, "worker-strong": 0},
    }
    assert repo["false_claims_by_skill"] == {
        "diagnosing-bugs": {"worker": 1, "worker-strong": 0},
        "tdd": {"worker": 1, "worker-strong": 0},
    }


def test_no_claim_values_are_not_claims(tmp_path, capsys):
    projects = tmp_path / "projects"
    # J: worker, code edit; skipped / not applicable values are no claim, tdd skipped is no claim
    _write_unit(projects, "C--Coding-j", "s1", "j", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/j/scripts/j.py"}, "m1"),
        _text("2026-10-21T10:05:00Z",
              "diagnosis: skipped (no reproduction)\n"
              "diagnosis: not applicable (docs only)\n"
              "tdd: skipped (no test without the heavy tool)"),
    ])

    result = _run_json(capsys, projects, ["j=C--Coding-j"])

    repo = result["repos"][0]
    assert repo["units"] == 1
    assert repo["code_edit_units"] == 1
    assert repo["claims_by_skill"] == {}
    assert repo["false_claims"] == 0
    assert repo["false_claims_by_skill"] == {}
    assert repo["silent"] == 0


def test_fenced_and_quoted_lines_are_not_claims(tmp_path, capsys):
    projects = tmp_path / "projects"
    # K: worker, code edit; the only claim-looking lines are inside a fence or quoted -> no claim, silent
    _write_unit(projects, "C--Coding-k", "s1", "k", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/k/scripts/k.py"}, "m1"),
        _text("2026-10-21T10:05:00Z",
              "```\ntdd: called\n```\n~~~\ndiagnosis: wrong cause\n~~~\n"
              "> tdd: called\n> diagnosis: wrong cause\nDone."),
    ])

    result = _run_json(capsys, projects, ["k=C--Coding-k"])

    repo = result["repos"][0]
    assert repo["units"] == 1
    assert repo["claims_by_skill"] == {}
    assert repo["false_claims"] == 0
    assert repo["silent"] == 1


def test_claim_before_the_last_3000_characters_is_ignored(tmp_path, capsys):
    projects = tmp_path / "projects"
    # L: the diagnosis line sits far above the last 3000 characters -> not read, silent (code edit, no tdd line)
    filler = "filler line of the final report\n" * 200
    _write_unit(projects, "C--Coding-l", "s1", "l", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/l/scripts/l.py"}, "m1"),
        _text("2026-10-21T10:05:00Z", "diagnosis: wrong sign in the sum\n" + filler + "Done."),
    ])
    # M: the same line in the last 3000 characters -> read, one false claim
    _write_unit(projects, "C--Coding-l", "s1", "m", "worker", [
        _prompt("2026-10-21T11:00:00Z"),
        _text("2026-10-21T11:05:00Z", filler + "diagnosis: wrong sign in the sum\nDone."),
    ])

    result = _run_json(capsys, projects, ["l=C--Coding-l"])

    repo = result["repos"][0]
    assert repo["units"] == 2
    assert repo["claims_by_skill"] == {"diagnosing-bugs": {"worker": 1, "worker-strong": 0}}
    assert repo["false_claims"] == 1
    assert repo["silent"] == 1


def test_since_is_in_and_until_is_out(tmp_path, capsys):
    projects = tmp_path / "projects"
    _write_unit(projects, "C--Coding-bounds", "s1", "before", "worker", [
        _prompt("2026-10-21T09:59:59Z"),
        _text("2026-10-21T09:59:59Z", "Done."),
    ])
    _write_unit(projects, "C--Coding-bounds", "s1", "at-since", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _text("2026-10-21T10:00:00Z", "Done."),
    ])
    _write_unit(projects, "C--Coding-bounds", "s1", "at-until", "worker", [
        _prompt("2026-10-22T10:00:00Z"),
        _text("2026-10-22T10:00:00Z", "Done."),
    ])

    result = _run_json(capsys, projects, ["b=C--Coding-bounds"],
                       "--since", "2026-10-21T10:00:00Z", "--until", "2026-10-22T10:00:00Z")

    repo = result["repos"][0]
    assert repo["units"] == 1
    assert repo["left_out"] == 0


def test_multiedit_only_unit_is_a_code_edit_unit(tmp_path, capsys):
    projects = tmp_path / "projects"
    # N: worker, the only edit is a MultiEdit of a code file, no tdd line -> code edit and silent
    _write_unit(projects, "C--Coding-n", "s1", "n", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "MultiEdit", {"file_path": "C:/Coding/n/scripts/n.py", "edits": []}, "m1"),
        _text("2026-10-21T10:05:00Z", "Done."),
    ])

    result = _run_json(capsys, projects, ["n=C--Coding-n"])

    repo = result["repos"][0]
    assert repo["units"] == 1
    assert repo["code_edit_units"] == 1
    assert repo["silent"] == 1


def test_text_table_has_the_columns(tmp_path, capsys):
    projects = tmp_path / "projects"
    _write_unit(projects, PROJECT, "s1", "a", "worker", [
        _prompt("2026-10-21T10:00:00Z"),
        _tool("2026-10-21T10:01:00Z", "Edit", {"file_path": "C:/Coding/orchestration-layer/scripts/foo.py"}, "m1"),
        _text("2026-10-21T10:05:00Z", "Done."),
    ])
    code = skill_recount.main(["--projects-dir", str(projects), "--repo", f"home={PROJECT}"])
    out = capsys.readouterr().out
    assert code == 0
    # one code-edit unit, silent, no claim: units, code_edit_units, false_claims, silent, silent_pct, left_out
    row = next(line for line in out.splitlines() if line.startswith("home "))
    assert row.split() == ["home", "1", "1", "0", "1", "100.0%", "0"]
