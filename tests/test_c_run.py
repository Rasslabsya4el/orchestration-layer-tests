"""Issue #376 unit 4: the C session runner (tools/replays/c_run.py), measured on a temp repo of two commits and a fake
claude (a python script that prints a result json and makes a commit on branch c-1). The real CLI shape of
``parse_result`` is the probe of the issue's unit 4 (one call of claude-haiku-5-5, cost 0.0033 USD)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.replays import c_run
from tools.replays.c_run import (CLAUDE_STUB, NO_COST_ON_TIMEOUT, build_command, neutralise_claude_md, parse_result,
                                 peak_context, run_c, trim_claude_md)
from tools.replays.c_seal import SealError, make_clone, sealed_env

TIMEOUT_SECONDS = 120
REPO_ROOT = Path(__file__).resolve().parents[1]
APPENDED = ("Delegate the simple to light models, the hard to strong ones. Do not delegate the small. Work in this clone; "
            "commit on a branch named `c-<N>`; run the tests you need; end with a short report.")

SAMPLE_CLAUDE = """# Repo

## Keep one

Keep this text.

@docs/agents/core-block.md

## Roles (orchestration v2)

Roles table line.

### Brain

brain line

## Keep two

keep two text

## Big efforts: Pocock's wayfinder

wayfinder text

## Skills are hard steps

skills text

## Last

last text
"""

CORE_BLOCK = """CORE BLOCK TEXT

## Core conventions

conventions text

@docs/agents/nested.md
"""

FAKE_CLAUDE = '''import json, os, subprocess, sys, time
from pathlib import Path

marker = os.environ.get("FAKE_MARKER")
if marker:
    Path(marker).write_text("invoked", encoding="utf-8")
if os.environ.get("FAKE_MODE") == "sleep":
    time.sleep(60)
    sys.exit(0)
subprocess.run(["git", "checkout", "-q", "-b", os.environ["FAKE_BRANCH"]], check=True)
Path("solution.txt").write_text("done\\n", encoding="utf-8")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@localhost", "commit", "-q", "-m", "fake"],
               check=True)
subtype = os.environ.get("FAKE_SUBTYPE", "success")
print(json.dumps({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": "report text",
                  "session_id": "fake-session", "total_cost_usd": 1.65, "num_turns": 3, "duration_ms": 90000,
                  "modelUsage": {"claude-opus-5-5": {"costUSD": 1.2}, "claude-haiku-5-5": {"costUSD": 0.45}}}))
'''


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          timeout=TIMEOUT_SECONDS)
    return done.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---- trim_claude_md ---------------------------------------------------------------------------------------------------


def test_trim_expands_the_import_removes_the_three_sections_and_appends(tmp_path: Path) -> None:
    write(tmp_path / "CLAUDE.md", SAMPLE_CLAUDE)
    write(tmp_path / "docs" / "agents" / "core-block.md", CORE_BLOCK)
    text = trim_claude_md(tmp_path, 7)
    assert "CORE BLOCK TEXT" in text
    assert "@docs/agents/core-block.md" not in text
    assert "## Keep one" in text and "Keep this text." in text
    assert "## Keep two" in text and "keep two text" in text
    assert "## Last" in text and "last text" in text
    for gone in ("## Roles (orchestration v2)", "Roles table line.", "### Brain", "brain line",
                 "## Big efforts: Pocock's wayfinder", "wayfinder text", "## Skills are hard steps", "skills text"):
        assert gone not in text
    assert text.rstrip("\n").endswith(APPENDED.replace("<N>", "7"))


def test_trim_expands_the_import_one_level_only(tmp_path: Path) -> None:
    write(tmp_path / "CLAUDE.md", SAMPLE_CLAUDE)
    write(tmp_path / "docs" / "agents" / "core-block.md", CORE_BLOCK)
    text = trim_claude_md(tmp_path, 7)
    assert "@docs/agents/nested.md" in text  # the import inside the expanded file stays a literal line


def test_trim_appended_line_is_verbatim_with_the_number(tmp_path: Path) -> None:
    write(tmp_path / "CLAUDE.md", SAMPLE_CLAUDE)
    write(tmp_path / "docs" / "agents" / "core-block.md", CORE_BLOCK)
    assert trim_claude_md(tmp_path, 376).rstrip("\n").split("\n")[-1] == APPENDED.replace("<N>", "376")


def test_trim_missing_section_raises(tmp_path: Path) -> None:
    write(tmp_path / "CLAUDE.md", SAMPLE_CLAUDE.replace("## Skills are hard steps", "## Other"))
    write(tmp_path / "docs" / "agents" / "core-block.md", CORE_BLOCK)
    with pytest.raises(ValueError):
        trim_claude_md(tmp_path, 7)


def test_trim_real_repo_drops_the_three_sections() -> None:
    text = trim_claude_md(REPO_ROOT, 376)
    assert "## The orchestration core" in text
    assert "## Core conventions" in text  # not named, kept
    assert "## Roles (orchestration v2)" not in text
    assert "## Big efforts: Pocock's wayfinder" not in text
    assert "## Skills are hard steps" not in text
    assert "@docs/agents/core-block.md" not in text
    assert text.rstrip("\n").endswith(APPENDED.replace("<N>", "376"))


# ---- build_command ----------------------------------------------------------------------------------------------------


def test_build_command_carries_each_flag(tmp_path: Path) -> None:
    claude = tmp_path / "claude.exe"
    system = tmp_path / "sys.md"
    argv = build_command(claude, system, "TICKET TEXT", 2.5)
    assert argv[0] == str(claude)
    assert argv[argv.index("-p") + 1] == "TICKET TEXT"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--system-prompt-file") + 1] == str(system)
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert "--agent" not in argv
    assert "stream-json" not in argv


def test_build_command_runs_a_py_stand_in_through_this_python(tmp_path: Path) -> None:
    script = tmp_path / "fake.py"
    argv = build_command(script, tmp_path / "sys.md", "t", 1.0)
    assert argv[:2] == [sys.executable, str(script)]


# ---- parse_result -----------------------------------------------------------------------------------------------------

SAMPLE_RESULT = {
    "duration_api_ms": 170000, "stop_reason": "end_turn", "session_id": "sess-9", "total_cost_usd": 2.5,
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 15, "cache_read_input_tokens": 16,
              "output_tokens": 4},
    "modelUsage": {
        "claude-opus-5-5": {"inputTokens": 10, "outputTokens": 20, "costUSD": 2.0, "contextWindow": 1000000},
        "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 6, "costUSD": 0.5, "contextWindow": 1000000},
    },
    "permission_denials": [], "is_error": False, "num_turns": 4, "subtype": "success", "result": "report",
    "type": "result", "duration_ms": 180000,
}


def test_parse_result_reads_the_real_field_names_with_two_models() -> None:
    parsed = parse_result(json.dumps(SAMPLE_RESULT))
    assert parsed["session_id"] == "sess-9"
    assert parsed["cost_usd"] == 2.5
    assert parsed["per_model"] == {"claude-opus-5-5": 2.0, "claude-haiku-5-5": 0.5}
    assert parsed["turns"] == 4
    assert parsed["duration_min"] == pytest.approx(3.0)
    assert parsed["is_error"] is False
    assert parsed["subtype"] == "success"
    assert parsed["result"] == "report"


def test_parse_result_non_json_raises() -> None:
    with pytest.raises(ValueError):
        parse_result("not json at all")


# ---- peak_context -----------------------------------------------------------------------------------------------------


def test_peak_context_is_the_max_over_assistant_messages(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    write(first, "\n".join([
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}}),
        json.dumps({"type": "assistant", "message": {"id": "m2", "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 4000}}}),
        json.dumps({"type": "assistant", "message": {"id": "m3", "model": "<synthetic>"}}),
    ]) + "\n")
    write(second, json.dumps({"type": "assistant", "message": {"id": "x", "usage": {
        "input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9000}}}) + "\n")
    assert peak_context([first]) == 4053
    assert peak_context([first, second]) == 9001
    assert peak_context([]) == 0


# ---- run_c ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "t")
    git(source, "config", "user.email", "t@localhost")
    write(source / "CLAUDE.md", SAMPLE_CLAUDE)
    write(source / "docs" / "agents" / "core-block.md", CORE_BLOCK)
    write(source / "f.txt", "v1\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    write(source / "f.txt", "v2\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "solution")
    later = git(source, "rev-parse", "HEAD")
    return {"source": source, "base": base, "later": later}


@pytest.fixture
def sealed(repo: dict, tmp_path: Path) -> dict:
    clone = make_clone(repo["source"], repo["base"], tmp_path / "clone")
    state = tmp_path / "state"
    sealed_env(clone, state, deny_checkout=repo["source"])
    return {**repo, "clone": clone, "state": state, "out": tmp_path / "out", "tmp": tmp_path}


@pytest.fixture
def fake(sealed: dict, monkeypatch) -> Path:
    script = sealed["tmp"] / "fake_claude.py"
    write(script, FAKE_CLAUDE)
    marker = sealed["tmp"] / "invoked"
    monkeypatch.setenv("FAKE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_BRANCH", "c-1")
    monkeypatch.delenv("FAKE_MODE", raising=False)
    monkeypatch.delenv("FAKE_SUBTYPE", raising=False)
    return script


def run(sealed: dict, claude: Path, **overrides):
    kwargs = dict(number=1, clone=sealed["clone"], state=sealed["state"], base=sealed["base"],
                  forbid=[sealed["later"]], ticket_text="Make f.txt read v2.", cap_usd=2.5, timeout_min=0.5,
                  out_dir=sealed["out"], claude_bin=claude, issues=(1,))
    kwargs.update(overrides)
    return run_c(**kwargs)


def test_run_c_record_of_a_clean_session(sealed: dict, fake: Path) -> None:
    record = run(sealed, fake)
    assert record["number"] == 1
    assert record["cost_usd"] == pytest.approx(1.65)
    assert record["per_model"] == {"claude-opus-5-5": 1.2, "claude-haiku-5-5": 0.45}
    assert record["cost_pct_of_week"] == pytest.approx(1.65 / 16.5)
    assert 0 <= record["hours"] < 0.5
    assert record["turns"] == 3
    assert record["peak_context"] == 0  # the fake leaves no transcript
    assert record["session_id"] == "fake-session"
    assert record["rc"] == 0
    assert record["stopped"] is False
    assert record["breaks"] == []
    assert record["branch"] is True
    assert record["commits"] == 1
    assert (sealed["tmp"] / "invoked").is_file()


def test_run_c_writes_record_diff_and_prompt(sealed: dict, fake: Path) -> None:
    record = run(sealed, fake)
    persisted = json.loads((sealed["out"] / "c1.json").read_text(encoding="utf-8"))
    assert persisted == record
    diff = (sealed["out"] / "c1.diff").read_text(encoding="utf-8")
    assert "solution.txt" in diff and "+done" in diff
    prompt = (sealed["out"] / "scratch" / "c1-system.md").read_text(encoding="utf-8")
    assert "## Roles (orchestration v2)" not in prompt
    assert "named `c-1`" in prompt


def test_run_c_budget_stop_is_stopped_with_its_break(sealed: dict, fake: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_SUBTYPE", "error_max_budget_usd")
    record = run(sealed, fake)
    assert record["stopped"] is True
    assert record["breaks"] == ["is_error: error_max_budget_usd"]
    assert record["branch"] is True


def test_run_c_timeout_kills_the_session_and_is_stopped(sealed: dict, fake: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "sleep")
    record = run(sealed, fake, timeout_min=0.02)  # about 1.2 s
    assert record["stopped"] is True
    assert record["rc"] is None
    assert "timeout" in record["breaks"]
    assert NO_COST_ON_TIMEOUT in record["breaks"]
    assert record["cost_usd"] is None
    assert record["branch"] is False
    assert record["commits"] == 0
    assert (sealed["out"] / "c1.diff").read_text(encoding="utf-8") == ""


def test_run_c_commits_neither_claude_md_nor_settings(sealed: dict, fake: Path) -> None:
    run(sealed, fake)
    clone = sealed["clone"]
    assert (clone / "CLAUDE.md").read_text(encoding="utf-8") == CLAUDE_STUB
    branch_diff = git(clone, "diff", f"{sealed['base']}..c-1")
    assert "CLAUDE.md" not in branch_diff and "settings.local.json" not in branch_diff
    saved = (sealed["out"] / "c1.diff").read_text(encoding="utf-8")
    assert "CLAUDE.md" not in saved and "settings.local.json" not in saved
    prompt = (sealed["out"] / "scratch" / "c1-system.md").read_text(encoding="utf-8")
    assert "CORE BLOCK TEXT" in prompt


def test_neutralise_stubs_claude_md_skips_its_worktree_and_excludes_settings(sealed: dict) -> None:
    clone = sealed["clone"]
    neutralise_claude_md(clone)
    assert (clone / "CLAUDE.md").read_text(encoding="utf-8") == CLAUDE_STUB
    assert git(clone, "ls-files", "-t", "CLAUDE.md").startswith("S ")
    excluded = (clone / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".claude/settings.local.json" in excluded


def test_run_c_leak_is_refused_before_any_process(sealed: dict, fake: Path) -> None:
    git(sealed["clone"], "-c", "user.name=leak", "-c", "user.email=leak@localhost", "commit", "-q",
        "--allow-empty", "-m", "leak")
    with pytest.raises(SealError):
        run(sealed, fake)
    assert not (sealed["tmp"] / "invoked").exists()
    assert not (sealed["out"] / "c1.json").exists()


def test_main_run_refused_leak_exits_1(sealed: dict, fake: Path, capsys) -> None:
    git(sealed["clone"], "-c", "user.name=leak", "-c", "user.email=leak@localhost", "commit", "-q",
        "--allow-empty", "-m", "leak")
    ticket = sealed["tmp"] / "ticket.txt"
    write(ticket, "Make f.txt read v2.")
    code = c_run.main(["run", "--number", "1", "--clone", str(sealed["clone"]), "--state", str(sealed["state"]),
                       "--base", sealed["base"], "--forbid", sealed["later"], "--ticket", str(ticket),
                       "--cap-usd", "2.5", "--timeout-min", "0.5", "--out", str(sealed["out"]),
                       "--claude-bin", str(fake)])
    assert code == 1
    assert "refused" in capsys.readouterr().out
    assert not (sealed["tmp"] / "invoked").exists()
