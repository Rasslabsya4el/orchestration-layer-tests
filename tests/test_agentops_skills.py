"""#1066: `python -m tools.agentops skills` counts Skill calls per role, and GLOSSARY.md reads by subagents.

Synthetic jsonl transcripts under tmp_path; the real ~/.claude folder is never read (Path.home is patched).
"""
import json
from pathlib import Path

import pytest

from tools.agentops import core_settings, skill_counter
from tools.agentops.cli import main

PROJECT = "C--Coding-orchestration-layer"
SESSION_ROLE = "session"
WORKER_ROLE = "subagent (worker)"


def _rec(ts, *blocks, text=None):
    content = list(blocks)
    if text is not None:
        content.append({"type": "text", "text": text})
    return {"type": "assistant", "timestamp": ts, "message": {"content": content}}


def _skill(name):
    return {"type": "tool_use", "name": "Skill", "input": {"skill": name}}


def _tool(name, **tool_input):
    return {"type": "tool_use", "name": name, "input": tool_input}


def _write(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home whose .claude/projects holds the project folder, a folder of another project,
    one main session, two subagents (one typed `worker`, one untyped) and records outside the window."""
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    main_dir = root / PROJECT
    _write(main_dir / "sess-main.jsonl", [
        _rec("2026-09-20T10:00:00.000Z", _skill("write-ticket")),
        _rec("2026-09-20T10:05:00.000Z", _skill("mattpocock-skills:grilling")),
        _rec("2026-08-01T10:00:00.000Z", _skill("write-ticket")),  # outside the window: not counted
    ])
    subagents = main_dir / "sess-main" / "subagents"
    _write(subagents / "agent-w1.jsonl", [
        _rec("2026-09-21T09:00:00.000Z", _skill("mattpocock-skills:tdd"),
             _tool("Read", file_path="C:/Coding/poe-junkroan-v2/GLOSSARY.md"), text="work on #1066"),
    ])
    (subagents / "agent-w1.meta.json").write_text(json.dumps({"agentType": "worker"}), encoding="utf-8")
    _write(subagents / "agent-p1.jsonl", [
        _rec("2026-09-21T09:30:00.000Z", _tool("Grep", pattern="tdd", path="docs")),
    ])
    other = root / "C--Coding-other-project"
    _write(other / "sess-other.jsonl", [_rec("2026-09-20T10:00:00.000Z", _skill("write-ticket"))])
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_counter, "MERGE_LOG", home / "no-merge.log")
    return home


@pytest.fixture
def bare_home(tmp_path, monkeypatch):
    """A fake home with an empty project folder; the merge log default points at a file that does not exist."""
    home = tmp_path / "home"
    (home / ".claude" / "projects" / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_counter, "MERGE_LOG", tmp_path / "no-merge.log")
    return home


@pytest.fixture
def keymaster_watched(tmp_path, monkeypatch):
    """#72: the settings name `keymaster` as a watched role (the keymaster's own sessions, Poe's setting)."""
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["skill_watched_roles"] = ["keymaster"]
    settings = tmp_path / "orchestration.json"
    settings.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", settings)


def _main_session(root: Path, name: str, setting: str):
    """A main session whose first record is the agent-setting record, with one diagnosing-bugs call in the window."""
    _write(root / PROJECT / f"{name}.jsonl", [
        {"type": "agent-setting", "agentSetting": setting, "sessionId": name},
        _rec("2026-09-20T10:00:00.000Z", _skill("mattpocock-skills:diagnosing-bugs")),
    ])


def test_keymaster_main_session_gets_its_own_role(bare_home, keymaster_watched):
    root = bare_home / ".claude" / "projects"
    _main_session(root, "km", "keymaster")
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15")
    assert t.calls == {("mattpocock-skills:diagnosing-bugs", "keymaster"): 1}
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert "| skill | total | session | keymaster |" not in text
    assert "| skill | total | keymaster | researcher-find | researcher-how |" in text
    assert "| mattpocock-skills:diagnosing-bugs | 1 | 1 | 0 | 0 |" in text


def test_watched_roles_show_zero_columns_without_calls(home, keymaster_watched):
    t = skill_counter.tally(home / ".claude" / "projects", since="2026-09-11")
    text = skill_counter.render(t, since="2026-09-11", until=None, issue=None)
    header = next(line for line in text.splitlines() if line.startswith("| skill |"))
    for role in ("researcher-find", "researcher-how", "keymaster"):
        assert role in header


def test_other_agent_setting_stays_session(bare_home):
    root = bare_home / ".claude" / "projects"
    _main_session(root, "orch", "orch")
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15")
    assert t.calls == {("mattpocock-skills:diagnosing-bugs", SESSION_ROLE): 1}


def test_removed_resolving_merge_conflicts_is_no_longer_a_watched_row(home):
    t = skill_counter.tally(home / ".claude" / "projects", since="2026-09-11", until="2026-10-15")
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert "resolving-merge-conflicts" not in text


MERGE_LINES = [
    "2026-10-01 10:00:00 | origin/issue-1 | conflict | suite 0.0 s | x",
    "2026-10-02 10:00:00 | origin/issue-2 | CONFLICT | suite 0.0 s | x",
    "2026-10-03 10:00:00 | origin/issue-3 | conflict | suite 0.0 s | x",
    "2026-08-01 10:00:00 | origin/issue-0 | conflict | suite 0.0 s | x",
    "2026-10-04 10:00:00 | origin/issue-4 | merged | suite 0.0 s | x",
]


def test_conflict_lines_in_window_are_counted(bare_home, tmp_path):
    root = bare_home / ".claude" / "projects"
    log = tmp_path / "merge.log"
    log.write_text("\n".join(MERGE_LINES) + "\n", encoding="utf-8")
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15", merge_log=log)
    assert t.conflict_lines == 3
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert ('merge conflicts in window (var/agentops/merge.log lines containing "conflict", '
            'case-insensitive): 3') in text


def test_no_merge_log_counts_zero(bare_home, tmp_path):
    root = bare_home / ".claude" / "projects"
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15", merge_log=tmp_path / "absent.log")
    assert t.conflict_lines == 0


def test_table_counts_and_glossary_line(home):
    t = skill_counter.tally(home / ".claude" / "projects", since="2026-09-11", until="2026-10-15")
    assert t.calls[("write-ticket", SESSION_ROLE)] == 1
    assert t.calls[("mattpocock-skills:grilling", SESSION_ROLE)] == 1
    assert t.calls[("mattpocock-skills:tdd", WORKER_ROLE)] == 1
    assert sum(n for (skill, _), n in t.calls.items() if skill == "write-ticket") == 1
    assert t.folders == [PROJECT]
    assert t.subagents_with_calls == 2
    assert t.subagents_naming_glossary == 1


def test_render_has_skill_rows_and_glossary_line(home):
    t = skill_counter.tally(home / ".claude" / "projects", since="2026-09-11", until="2026-10-15")
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert "| write-ticket | 1 |" in text
    assert "| mattpocock-skills:tdd | 1 |" in text
    assert "| mattpocock-skills:diagnosing-bugs | 0 |" in text  # watched skills show their zero
    assert "subagent transcripts with >= 1 tool call: 2, of them naming GLOSSARY.md in a tool call: 1" in text


def test_issue_filter_counts_only_transcripts_mentioning_the_issue(home):
    root = home / ".claude" / "projects"
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15", issue=1066)
    assert t.calls == {("mattpocock-skills:tdd", WORKER_ROLE): 1}
    assert t.subagents_with_calls == 1
    assert t.subagents_naming_glossary == 1
    none = skill_counter.tally(root, since="2026-09-11", until="2026-10-15", issue=999)
    assert sum(none.calls.values()) == 0


def test_window_end_is_exclusive(home):
    t = skill_counter.tally(home / ".claude" / "projects", since="2026-09-20", until="2026-09-21")
    assert t.calls[("write-ticket", SESSION_ROLE)] == 1
    assert ("mattpocock-skills:tdd", WORKER_ROLE) not in t.calls


def test_command_registered_in_cli(home, capsys):
    rc = main(["skills", "--since", "2026-09-11", "--until", "2026-10-15"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| write-ticket | 1 |" in out
    assert "naming GLOSSARY.md in a tool call: 1" in out


def test_the_project_prefix_is_this_repos_claude_folder():
    from tools.agentops import skill_counter
    assert skill_counter.PROJECT_PREFIX == "C--Coding-orchestration-layer"


def test_transcripts_are_streamed_not_read_whole(home, monkeypatch):
    """#13 finding 3: a transcript (.jsonl) is read line by line, never with read_text; the counts are unchanged."""
    real_read_text = Path.read_text

    def no_whole_transcript_read(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            raise AssertionError(f"read_text on transcript {self.name}: the file is read whole")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_whole_transcript_read)
    root = home / ".claude" / "projects"
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15")
    assert t.calls == {("write-ticket", SESSION_ROLE): 1, ("mattpocock-skills:grilling", SESSION_ROLE): 1,
                       ("mattpocock-skills:tdd", WORKER_ROLE): 1}
    assert t.subagents_with_calls == 2
    assert t.subagents_naming_glossary == 1
    t = skill_counter.tally(root, since="2026-09-11", until="2026-10-15", issue=1066)
    assert t.calls == {("mattpocock-skills:tdd", WORKER_ROLE): 1}
    assert t.subagents_with_calls == 1
    assert t.subagents_naming_glossary == 1
