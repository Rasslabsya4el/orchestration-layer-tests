"""Issue #93 row 6: `core table` prints `not checked: no leak words` for a repo folder without
config/core_leak_patterns.json, and never a 0 in the L1-L3 cells of that row; a folder with the file
keeps its totals. Seam: core_table.compute_repo_row / format_table (the table's own rows). Planted
tmp_path fixtures only; no network (the gh runner is a stand-in) and the real home file is never read."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import core_table

import token_meter as tm  # noqa: E402  (the home's scripts/token_meter.py, put on sys.path by core_table)

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 9, 30, tzinfo=timezone.utc)
PRICES = {"claude-sonnet-5": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.2, "output": 10.0}}
NOT_CHECKED = "not checked: no leak words"


def _init_target(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "orchestration.json").write_text(json.dumps({
        "repo": f"someone/{name}",
        "brain_chat_title": "Brain",
        "context_line": 150000,
        "claude_projects_dir": name,
        "bridge_dir": str(repo / "var" / "agentops"),
        "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [],
        "home": "someone/home-repo",
        "core_version": "deadbeef",
        "update_rule": "word",
    }, indent=2), encoding="utf-8")
    (repo / "artifacts" / "runs").mkdir(parents=True)
    return repo


def _gh_runner(repo_slug: str):
    """Answers only the timeline calls the table makes: every issue has an empty timeline."""
    def runner(args):
        if args[:2] == ["api", f"repos/{repo_slug}/issues/10/timeline"] or \
                args[:2] == ["api", f"repos/{repo_slug}/issues/20/timeline"]:
            return "[]\n"
        raise AssertionError(f"unexpected gh call: {args}")
    return runner


def _turns():
    return [tm.Turn(
        session_id="sess1", agent_id=None, agent_type=None, message_id="m1",
        model="claude-sonnet-5", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc),
        input_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=0,
        output_tokens=200, issue=10, title=None, turn_index=0,
    )]


def _row(repo: Path, slug: str):
    return core_table.compute_repo_row(
        repo, turns=_turns(),
        closed_issues=[{"number": 10, "closedAt": "2026-09-10T00:00:00Z"}],
        prices=PRICES, since=SINCE, until=UNTIL, gh_runner=_gh_runner(slug),
    )


def _leak_cell(rendered: str) -> str:
    """The leaks column of the target's row: the `L1/L2/L3/H` cell, as printed."""
    header, row = rendered.splitlines()[0], rendered.splitlines()[1]
    end = header.index("leaks") + len("leaks")  # the column is right-aligned in 10 characters
    return row[end - 10:end].strip()


def test_a_target_without_the_file_prints_the_not_checked_line_and_no_zero_in_l1_to_l3(tmp_path):
    repo = _init_target(tmp_path, "bare-target")
    assert not (repo / "config" / "core_leak_patterns.json").exists()
    row = _row(repo, "someone/bare-target")
    rendered = core_table.format_table([row])
    assert NOT_CHECKED in rendered
    cells = _leak_cell(rendered).split("/")
    assert cells[:3] == ["-", "-", "-"]  # L1-L3 not judged: never a 0
    assert "0/0/0" not in rendered and cells[0] != "0"


def test_a_target_with_the_file_prints_its_real_counts_and_no_not_checked_line(tmp_path):
    """No monkeypatch: the real run_check reads the repo's own patterns file and the real manifest; the planted
    `Widget` in docs/agents/PORTING.md (a manifest entry) is the one L1 hit."""
    repo = _init_target(tmp_path, "with-target")
    (repo / "config" / "core_leak_patterns.json").write_text(json.dumps({"L1": "Widget"}), encoding="utf-8")
    (repo / "docs" / "agents").mkdir(parents=True)
    (repo / "docs" / "agents" / "PORTING.md").write_text("# porting\nuse the Widget here\n", encoding="utf-8")
    row = _row(repo, "someone/with-target")
    rendered = core_table.format_table([row])
    assert NOT_CHECKED not in rendered
    assert _leak_cell(rendered).split("/")[:3] == ["1", "0", "0"]
