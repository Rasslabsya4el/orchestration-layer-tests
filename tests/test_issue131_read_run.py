"""Issue #131: the brain's read-run counter (tools/hooks/brain_read_run.py).

Seams under test: ``classify`` and ``count_run``, the module's two public functions. Records are
built by hand in the shape of a transcript's JSON lines, so the expected values come from the
issue's rule, not from the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.hooks.brain_read_run import classify, count_run  # noqa: E402


# --- record builders -------------------------------------------------------------------------

def call(cid, name, tool_input=None):
    return {"type": "tool_use", "id": cid, "name": name, "input": tool_input or {}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def read(cid):
    return assistant(call(cid, "Read", {"file_path": "a.md"}))


def bash(cid, command):
    return assistant(call(cid, "Bash", {"command": command}))


def edit(cid):
    return assistant(call(cid, "Edit", {"file_path": "a.md", "old_string": "x", "new_string": "y"}))


def agent(cid):
    return assistant(call(cid, "Agent", {"prompt": "find x"}))


def tool_result(cid):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": cid, "content": "ok"}]}}


def prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def prompt_blocks(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


# --- classify --------------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ["Read", "Grep", "Glob"])
def test_read_tools_are_read(tool_name):
    assert classify(tool_name, {"file_path": "a.md", "pattern": "x"}) == "read"


@pytest.mark.parametrize("command", [
    "cat a | head",
    "git log --oneline",
    "gh issue view 5",
    "gh api repos/x/y",
    "gh api -X GET repos/x/y",
    "cd C:/x && rg foo",
    "sed -n 1,20p f.md",
    "tail -n 5 log.txt",
    "ls -la",
    "find . -name '*.md'",
    "git show HEAD",
    "git diff HEAD~1",
    "gh issue list",
    "gh pr view 7",
    "gh pr list",
    "grep -n x f.md",
])
def test_read_only_bash_is_read(command):
    assert classify("Bash", {"command": command}) == "read"


@pytest.mark.parametrize("command", [
    "gh api -X POST repos/x/y",
    "gh api --method=PATCH repos/x/y",
    "gh api repos/x/y -f title=a",
    "gh api repos/x/y --input body.json",
    "sed -i s/a/b/ f.md",
    "echo x > f",
    "cat a >> f",
    "find . -delete",
    "find . -exec rm {} ;",
    "git push origin HEAD",
])
def test_writing_or_unknown_bash_is_other(command):
    assert classify("Bash", {"command": command}) == "other"


SCRIPTS = ("scripts/status_tool.py",)


@pytest.mark.parametrize("command", [
    "python -m tools.agentops outcome 5 --check agree",
    "python3 -m tools.agentops status",
    "cd C:/x && python -m tools.agentops routes",
])
def test_agentops_status_is_neutral(command):
    assert classify("Bash", {"command": command}) == "neutral"


@pytest.mark.parametrize("command", [
    "python scripts/status_tool.py status",
    "python3 scripts/status_tool.py list",
    "python C:/x/scripts/status_tool.py list",
    "python scripts\\status_tool.py list",
    "cd C:/x && python scripts/status_tool.py status",
])
def test_neutral_script_from_settings_is_neutral(command):
    assert classify("Bash", {"command": command}, neutral_scripts=SCRIPTS) == "neutral"


@pytest.mark.parametrize("command", [
    "python -m tools.agentops routes 2>&1",
    "python -m tools.agentops routes >/dev/null",
    "python -m tools.agentops a && python scripts/status_tool.py b",
])
def test_neutral_with_harmless_redirection_or_neutral_chain(command):
    assert classify("Bash", {"command": command}, neutral_scripts=SCRIPTS) == "neutral"


@pytest.mark.parametrize("command", [
    "python scripts/status_tool.py status",
    "python3 scripts/status_tool.py list",
])
def test_script_is_other_without_the_setting(command):
    assert classify("Bash", {"command": command}) == "other"
    assert classify("Bash", {"command": command}, neutral_scripts=()) == "other"


@pytest.mark.parametrize("command", [
    "python scripts/status_tool.py.bak status",
    "python scripts/other_tool.py status",
    "node scripts/status_tool.py status",
    "python scripts/status_tool.py status > out.txt",
])
def test_script_neutral_needs_exact_suffix_and_plain_python(command):
    assert classify("Bash", {"command": command}, neutral_scripts=SCRIPTS) == "other"


def test_agentops_stays_neutral_with_empty_settings():
    assert classify("Bash", {"command": "python -m tools.agentops status"},
                    neutral_scripts=()) == "neutral"


@pytest.mark.parametrize("command", [
    'grep -E "a|b" f.md',
    "gh issue view 5 -q '.x | .y'",
    'rg "=>" src',
    "rg 'a && b' f.md",
    'cat "a;b" f.md',
])
def test_operators_inside_quotes_do_not_split(command):
    assert classify("Bash", {"command": command}) == "read"


@pytest.mark.parametrize("command", [
    "ls && echo x > f",
    "ls || echo x > f",
    "ls; echo x > f",
    "ls || rm f",
    "cd x && echo x > f",
    "cd x && git push",
    "ls && git push",
    "ls; rm f",
    "python -m tools.agentops routes && git push",
    "python -m tools.agentops routes > f",
])
def test_any_writing_or_unknown_segment_makes_the_command_other(command):
    assert classify("Bash", {"command": command}) == "other"


@pytest.mark.parametrize("command", [
    "python -m tools.agentops x && git log",
    "ls && python -m tools.agentops x",
    "ls 2>/dev/null && python -m tools.agentops x",
])
def test_neutral_segment_with_read_segment_is_read(command):
    assert classify("Bash", {"command": command}) == "read"


@pytest.mark.parametrize("command", [
    "ls 2>/dev/null",
    "git log 2>&1",
    "ls >/dev/null",
    "ls 1>/dev/null",
    "ls &>/dev/null",
    "ls >&2",
    "ls 2>NUL",
    "grep x f.md 2>/dev/null | head -n 3",
])
def test_harmless_redirections_are_dropped_before_classifying(command):
    assert classify("Bash", {"command": command}) == "read"


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "Agent", "Task", "SomeNewTool"])
def test_other_tools_are_other(tool_name):
    assert classify(tool_name, {}) == "other"


# --- count_run -------------------------------------------------------------------------------

def test_three_reads_count_three():
    records = [read("r1"), tool_result("r1"), read("r2"), tool_result("r2"),
               read("r3"), tool_result("r3")]
    assert count_run(records, None) == 3


def test_agentops_between_reads_is_neutral():
    records = [read("r1"), tool_result("r1"),
               bash("n1", "python -m tools.agentops outcome 5"), tool_result("n1"),
               read("r2"), tool_result("r2")]
    assert count_run(records, None) == 2


def test_edit_between_reads_restarts_the_run():
    records = [read("r1"), read("r2"), read("r3"), edit("e1"), tool_result("e1"),
               read("r4"), tool_result("r4")]
    assert count_run(records, None) == 1


def test_agent_call_resets_the_run():
    records = [read("r1"), read("r2"), agent("a1"), tool_result("a1"), read("r3")]
    assert count_run(records, None) == 1


def test_real_prompt_resets_the_run():
    records = [read("r1"), read("r2"), prompt("now look at issue 9"), read("r3")]
    assert count_run(records, None) == 1


def test_real_prompt_as_text_block_resets_the_run():
    records = [read("r1"), read("r2"), prompt_blocks("next"), read("r3")]
    assert count_run(records, None) == 1


def test_tool_result_records_do_not_reset_the_run():
    records = [read("r1"), tool_result("r1"), read("r2"), tool_result("r2")]
    assert count_run(records, None) == 2


def test_parallel_calls_count_only_those_before_current():
    parallel = assistant(call("r1", "Read", {"file_path": "a"}),
                         call("r2", "Read", {"file_path": "b"}),
                         call("r3", "Read", {"file_path": "c"}))
    records = [parallel]
    assert count_run(records, "r2") == 1


def test_parallel_calls_without_current_count_all_three():
    parallel = assistant(call("r1", "Read", {"file_path": "a"}),
                         call("r2", "Read", {"file_path": "b"}),
                         call("r3", "Read", {"file_path": "c"}))
    assert count_run([parallel], None) == 3


def test_unknown_current_id_considers_all_calls():
    records = [read("r1"), read("r2"), read("r3")]
    assert count_run(records, "not-there") == 3


def test_compound_write_segment_resets_the_run():
    records = [read("r1"), bash("w1", "ls && git push"), tool_result("w1"), read("r2")]
    assert count_run(records, None) == 1


def test_neutral_then_read_segment_counts_as_a_read():
    records = [read("r1"), tool_result("r1"),
               bash("n1", "python -m tools.agentops x && git log"), tool_result("n1"),
               read("r2"), tool_result("r2")]
    assert count_run(records, None) == 3


def test_neutral_script_from_settings_between_reads_neither_counts_nor_resets():
    records = [read("r1"), tool_result("r1"),
               bash("n1", "python scripts/status_tool.py status"), tool_result("n1"),
               read("r2"), tool_result("r2")]
    assert count_run(records, None, neutral_scripts=SCRIPTS) == 2


def test_neutral_script_without_settings_resets_the_run():
    records = [read("r1"), tool_result("r1"),
               bash("n1", "python scripts/status_tool.py status"), tool_result("n1"),
               read("r2"), tool_result("r2")]
    assert count_run(records, None) == 1
