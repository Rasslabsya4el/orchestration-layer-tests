"""Issue #104: the SubagentStop check holds a worker's stop once when its final report claims a skill it never called.

The tests plant JSONL transcripts in tmp_path and drive ``run(payload)`` (the verdict) and ``main()`` (the stdin and
stdout shape). Expected verdicts come from the issue's rule, not from the hook's code.
"""
from __future__ import annotations

import io
import json
import sys
import types

import pytest

from tools.hooks import subagent_skill_check as hook


def _text(text):
    return {"type": "text", "text": text}


def _tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def _skill(name):
    return _tool("Skill", skill=name)


def _edit(path, name="Edit"):
    return _tool(name, file_path=path)


def _turn(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _write_transcript(tmp_path, records, name="agent.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


def _payload(tmp_path, records, final=None, agent_type="worker", **extra):
    payload = {
        "session_id": "main-session",
        # the MAIN session's transcript: the hook must never read it (the file is not even created)
        "transcript_path": str(tmp_path / "main-never-read.jsonl"),
        "agent_id": "agent-104",
        "agent_type": agent_type,
        "agent_transcript_path": _write_transcript(tmp_path, records),
        "stop_hook_active": False,
    }
    if final is not None:
        payload["last_assistant_message"] = final
    payload.update(extra)
    return payload


@pytest.fixture(autouse=True)
def _log_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_SUBAGENT_SKILL_CHECK_LOG", str(tmp_path / "check.log"))


# acceptance row 1: a diagnosis line with no diagnosing-bugs call is blocked, and the reason names the skill
def test_row1_diagnosis_claim_without_call_is_blocked(tmp_path):
    records = [_turn(_text("working"))]
    out = hook.run(_payload(tmp_path, records, final="diagnosis: stale cache in the loader"))
    assert out is not None
    assert out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]


# acceptance row 2: the same claim with the call present passes, under either name form
@pytest.mark.parametrize("name", ["diagnosing-bugs", "mattpocock-skills:diagnosing-bugs"])
def test_row2_diagnosis_claim_with_call_passes(tmp_path, name):
    records = [_turn(_skill(name)), _turn(_text("fixed"))]
    assert hook.run(_payload(tmp_path, records, final="diagnosis: stale cache in the loader")) is None


# acceptance row 3: a code edit with no tdd line is blocked; the skipped line passes
def test_row3_code_edit_without_tdd_line_is_blocked(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    out = hook.run(_payload(tmp_path, records, final="Changed the loader."))
    assert out is not None
    assert out["decision"] == "block"
    assert "tdd" in out["reason"]


@pytest.mark.parametrize("line", [
    "tdd: skipped (no test without the numeric tool)",
    "tdd skipped: no test without the numeric tool",
])
def test_row3_code_edit_with_tdd_skipped_passes(tmp_path, line):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    assert hook.run(_payload(tmp_path, records, final=f"Changed the loader.\n{line}")) is None


def test_row3_code_edit_with_tdd_call_and_no_line_passes(tmp_path):
    records = [_turn(_skill("tdd")), _turn(_edit("/work/repo/tools/x.py"))]
    assert hook.run(_payload(tmp_path, records, final="Changed the loader.")) is None


# acceptance row 4: a second stop of the same subagent passes, whatever it says
def test_row4_second_stop_after_block_passes(tmp_path):
    records = [_turn(_text("working"))]
    first = hook.run(_payload(tmp_path, records, final="diagnosis: stale cache"))
    assert first is not None and first["decision"] == "block"
    second = hook.run(_payload(tmp_path, records, final="diagnosis: stale cache", stop_hook_active=True))
    assert second is None


# acceptance row 5: researcher and reviewer transcripts with a false claim are never blocked
@pytest.mark.parametrize("agent_type", ["researcher-how", "researcher-find", "reviewer-spec",
                                        "reviewer-standards", ""])
def test_row5_other_roles_are_never_blocked(tmp_path, agent_type):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    final = "diagnosis: stale cache\ntdd: called\nwriting-for-agents: called"
    assert hook.run(_payload(tmp_path, records, final=final, agent_type=agent_type)) is None


def test_agent_type_missing_is_never_blocked(tmp_path):
    payload = _payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache")
    del payload["agent_type"]
    assert hook.run(payload) is None


@pytest.mark.parametrize("agent_type", ["worker", "worker-strong"])
def test_both_coding_roles_are_checked(tmp_path, agent_type):
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale", agent_type=agent_type))
    assert out is not None and out["decision"] == "block"


# main(): stdin in, the decision JSON out, exit 0
def test_main_prints_decision_json_for_one_block(tmp_path, monkeypatch, capsys):
    payload = _payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache")
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(raw)))
    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]


def test_main_prints_nothing_when_it_passes(tmp_path, monkeypatch, capsys):
    payload = _payload(tmp_path, [_turn(_skill("diagnosing-bugs"))], final="diagnosis: stale cache")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_main_fails_open_on_bad_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"not json {")))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


# every report line that claims a skill
@pytest.mark.parametrize("label, skill", [
    ("tdd", "tdd"),
    ("writing-for-agents", "writing-for-agents"),
    ("domain-modeling", "domain-modeling"),
    ("codebase-design", "codebase-design"),
])
def test_each_report_line_needs_its_skill_call(tmp_path, label, skill):
    final = f"{label}: called"
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final))
    assert out is not None and out["decision"] == "block"
    assert skill in out["reason"]
    assert hook.run(_payload(tmp_path, [_turn(_skill(skill))], final=final)) is None


def test_diagnosis_line_needs_its_skill_call(tmp_path):
    final = "diagnosis: stale cache"
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final))
    assert out is not None and out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]
    assert hook.run(_payload(tmp_path, [_turn(_skill("diagnosing-bugs"))], final=final)) is None


@pytest.mark.parametrize("line", [
    "diagnosis: not a defect",
    "diagnosis: n/a",
    "diagnosis: none",
    "diagnosis: na",
    "diagnosis: no defect",
    "diagnosis: not run",
    "diagnosis: not needed",
    "writing-for-agents: n/a",
    "domain-modeling: not applicable",
    "codebase-design: skipped (no design change)",
])
def test_no_claim_forms_pass(tmp_path, line):
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=line)) is None


# Fix A: markup around the label or around the no-claim value does not turn a no-claim into a claim
@pytest.mark.parametrize("line", [
    "**diagnosis:** not a defect",
    "diagnosis: `n/a`",
    "diagnosis: (none)",
    "diagnosis: **not a defect**",
    "diagnosis: \u2013 n/a",
    "**tdd:** skipped (why)",
    "> ",
])
def test_markup_around_a_no_claim_value_passes(tmp_path, line):
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=line)) is None


def test_markup_around_a_no_claim_still_passes_with_code_edit(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    final = "**tdd:** skipped (why)\n**diagnosis:** not a defect"
    assert hook.run(_payload(tmp_path, records, final=final)) is None


@pytest.mark.parametrize("line", ["`tdd: skipped`", "**tdd:** skipped (why)", "- **tdd:** called"])
def test_markup_wrapped_tdd_line_counts_for_a_code_edit(tmp_path, line):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    if "called" in line:
        records.append(_turn(_skill("tdd")))
    assert hook.run(_payload(tmp_path, records, final=line)) is None


# Fix B: only an explicit list at the start of the value is a no-claim; a value that merely starts with no/not claims
@pytest.mark.parametrize("line, skill", [
    ("diagnosis: No guard on empty list", "diagnosing-bugs"),
    ("diagnosis: not reproduced; the cache keeps the old value", "diagnosing-bugs"),
    ("diagnosis: nothing new", "diagnosing-bugs"),
    ("diagnosis: Not reproduced on CI, a real bug", "diagnosing-bugs"),
])
def test_values_that_merely_start_with_no_or_not_are_claims(tmp_path, line, skill):
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final=line))
    assert out is not None and out["decision"] == "block"
    assert skill in out["reason"]


# Fix C: a line inside a fenced code block, and a blockquote line, is not a report line
def test_fenced_template_does_not_block(tmp_path):
    final = "Format for the report:\n```\ntdd: called / skipped (why)\ndiagnosis: <cause>\n```"
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final)) is None


def test_fenced_template_is_not_a_tdd_line_for_a_code_edit(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    final = "```\ntdd: skipped (why)\n```"
    out = hook.run(_payload(tmp_path, records, final=final))
    assert out is not None and out["decision"] == "block"
    assert "tdd" in out["reason"]


def test_tilde_fence_is_skipped_too(tmp_path):
    final = "~~~\ndiagnosis: stale cache\n~~~"
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final)) is None


def test_a_real_line_after_a_fence_still_counts(tmp_path):
    final = "```\nx = 1\n```\ndiagnosis: stale cache"
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final))
    assert out is not None and out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]


def test_blockquote_line_does_not_block(tmp_path):
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final="> diagnosis: stale cache")) is None


def test_blockquote_tdd_line_is_not_a_tdd_line_for_a_code_edit(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    out = hook.run(_payload(tmp_path, records, final="> tdd: skipped (why)"))
    assert out is not None and out["decision"] == "block"
    assert "tdd" in out["reason"]


# second round: "not called" / "not used" / "not invoked" are no-claim values (real replay lines)
@pytest.mark.parametrize("line", [
    "codebase-design: not called (no new module, no seam moved)",
    "- **writing-for-agents:** not called. I edited nothing, so the skill was not needed",
    "diagnosis: not used",
    "diagnosis: not invoked",
])
def test_not_called_forms_pass(tmp_path, line):
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=line)) is None


def test_bulleted_backtick_claim_still_blocks_without_call(tmp_path):
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final="- `writing-for-agents: called`"))
    assert out is not None and out["decision"] == "block"
    assert "writing-for-agents" in out["reason"]


def test_bulleted_backtick_claim_passes_with_call(tmp_path):
    records = [_turn(_skill("writing-for-agents"))]
    assert hook.run(_payload(tmp_path, records, final="- `writing-for-agents: called`")) is None


# second round: a tdd line counts mid-line too, but only as "called" or "skipped"
def test_docs_only_unit_with_bulleted_tdd_skipped_passes(tmp_path):
    records = [_turn(_edit("/work/repo/docs/agents/x.md"))]
    assert hook.run(_payload(tmp_path, records, final="- `tdd: skipped (docs-only; no code changed)`")) is None


def test_code_edit_with_mid_line_tdd_skipped_passes(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    final = "Changed the loader and the note. tdd: skipped (docs-only)"
    assert hook.run(_payload(tmp_path, records, final=final)) is None


def test_code_edit_with_numbered_tdd_skipped_passes(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    assert hook.run(_payload(tmp_path, records, final="1. tdd: skipped (why)")) is None


def test_code_edit_with_tdd_not_called_is_blocked(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    out = hook.run(_payload(tmp_path, records, final="Changed the loader.\ntdd: not called"))
    assert out is not None and out["decision"] == "block"
    assert "tdd" in out["reason"]


def test_code_edit_with_tdd_continued_is_blocked(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    out = hook.run(_payload(tmp_path, records, final="Changed the loader.\ntdd: continued"))
    assert out is not None and out["decision"] == "block"
    assert "tdd" in out["reason"]


def test_skipped_forms_pass_without_any_call(tmp_path):
    final = "tdd: skipped (no test without the numeric tool)\nwriting-for-agents: skipped (no skill edit)"
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final)) is None


@pytest.mark.parametrize("path", [
    "/work/repo/tools/a.py",
    "/work/repo/web/app.js",
    "/work/repo/web/app.ts",
    "/work/repo/web/app.tsx",
    "/work/repo/tools/run.ps1",
    "/work/repo/tools/run.sh",
    "/work/repo/web/index.html",
    "/work/repo/web/site.css",
    "/work/repo/config/x.json",
])
def test_every_code_extension_counts_as_a_code_edit(tmp_path, path):
    out = hook.run(_payload(tmp_path, [_turn(_edit(path))], final="Changed it."))
    assert out is not None and out["decision"] == "block"
    assert "tdd" in out["reason"]


@pytest.mark.parametrize("tool", ["Write", "MultiEdit"])
def test_write_and_multiedit_count_as_code_edits(tmp_path, tool):
    out = hook.run(_payload(tmp_path, [_turn(_edit("/work/repo/tools/x.py", name=tool))], final="Done."))
    assert out is not None and out["decision"] == "block"


@pytest.mark.parametrize("path", [
    "/work/repo/docs/notes.md",
    "C:/Users/user/scratchpad/check.py",
    "C:/Users/user/AppData/Local/Temp/check.py",
    "/tmp/check.py",
])
def test_markdown_and_scratch_paths_are_not_code_edits(tmp_path, path):
    assert hook.run(_payload(tmp_path, [_turn(_edit(path))], final="Done.")) is None


def test_reads_are_not_edits(tmp_path):
    records = [_turn(_tool("Read", file_path="/work/repo/tools/x.py"))]
    assert hook.run(_payload(tmp_path, records, final="Read it.")) is None


def test_two_problems_make_one_block_naming_both(tmp_path):
    records = [_turn(_edit("/work/repo/tools/x.py"))]
    out = hook.run(_payload(tmp_path, records, final="diagnosis: stale cache"))
    assert out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]
    assert "tdd" in out["reason"]


def test_final_message_falls_back_to_last_transcript_text(tmp_path):
    records = [_turn(_text("working")), _turn(_text("diagnosis: stale cache"))]
    out = hook.run(_payload(tmp_path, records))
    assert out is not None and out["decision"] == "block"
    assert "diagnosing-bugs" in out["reason"]


def test_only_the_last_3000_characters_are_read(tmp_path):
    final = "diagnosis: stale cache\n" + "z" * 3000
    assert hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final)) is None


def test_claim_inside_the_last_3000_characters_is_read(tmp_path):
    final = "z" * 3000 + "\ndiagnosis: stale cache"
    out = hook.run(_payload(tmp_path, [_turn(_text("x"))], final=final))
    assert out is not None and out["decision"] == "block"


def test_skill_call_anywhere_in_transcript_counts(tmp_path):
    records = [_turn(_skill("diagnosing-bugs")), _turn(_text("a")), _turn(_text("b"))]
    assert hook.run(_payload(tmp_path, records, final="diagnosis: stale cache")) is None


def test_block_is_logged_as_one_json_line(tmp_path):
    hook.run(_payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache"))
    lines = (tmp_path / "check.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["agent_id"] == "agent-104"
    assert row["agent_type"] == "worker"
    assert row["problems"] == ["diagnosing-bugs"]


def test_fails_open_on_missing_agent_transcript(tmp_path):
    payload = _payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache")
    payload["agent_transcript_path"] = str(tmp_path / "does-not-exist.jsonl")
    assert hook.run(payload) is None


def test_fails_open_on_malformed_transcript_line(tmp_path):
    payload = _payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache")
    (tmp_path / "agent.jsonl").write_text("{not json\n", encoding="utf-8")
    assert hook.run(payload) is None


def test_fails_open_without_transcript_path(tmp_path):
    payload = _payload(tmp_path, [_turn(_text("x"))], final="diagnosis: stale cache")
    del payload["agent_transcript_path"]
    assert hook.run(payload) is None
