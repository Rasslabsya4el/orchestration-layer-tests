"""Issue #376 unit 2: tools/replays/c_tickets.py reads a ticket as it stood when ready-for-agent was first added.

Seams: select_ready_version (pure: takes the fetched edits, renames and label events, no network), acceptance_rows
(inline bodies and the saved bodies of #127, #238, #218), base_commit (a temp git repo with merge commits) and
main (its own arguments, with the network reads replaced by fakes).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.replays import c_tickets as ct

FIXTURES = Path(__file__).parent / "fixtures" / "c_tickets"

T0 = "2026-10-01T10:00:00Z"
T_PRE = "2026-10-01T18:00:00Z"
T_LABEL = "2026-10-02T10:00:00Z"
T1 = "2026-10-03T10:00:00Z"
T2 = "2026-10-04T10:00:00Z"


def label(at: str, name: str = "ready-for-agent") -> dict:
    return {"__typename": "LabeledEvent", "createdAt": at, "label": {"name": name}}


def rename(at: str, previous: str, current: str) -> dict:
    return {"__typename": "RenamedTitleEvent", "createdAt": at, "previousTitle": previous, "currentTitle": current}


def edit(at: str, body: str) -> dict:
    return {"editedAt": at, "createdAt": at, "diff": body}


# ---- selection: body and title as they stood at the first ready-for-agent label ----


def test_no_edits_and_no_rename_gives_the_current_title_and_body():
    got = ct.select_ready_version("T now", "B now", [], [], [label(T_LABEL)])
    assert got == {"title": "T now", "body": "B now", "ready_at": T_LABEL, "edit_used": None}


def test_only_an_edit_before_the_label_gives_the_current_body():
    got = ct.select_ready_version("T", "B now", [edit(T0, "B now")], [], [label(T_LABEL)])
    assert got["body"] == "B now"
    assert got["edit_used"] is None


def test_an_edit_after_the_label_gives_the_version_written_before_it():
    edits = [edit(T0, "B0"), edit(T2, "B2")]
    got = ct.select_ready_version("T", "B2", edits, [], [label(T_LABEL)])
    assert got["body"] == "B0"
    assert got["edit_used"] == T0


def test_the_latest_version_at_or_before_the_label_wins():
    edits = [edit(T0, "B0"), edit(T_PRE, "B1"), edit(T2, "B2")]
    got = ct.select_ready_version("T", "B2", edits, [], [label(T_LABEL)])
    assert got["body"] == "B1"
    assert got["edit_used"] == T_PRE


def test_an_edit_exactly_at_the_label_time_counts_as_before_it():
    edits = [edit(T0, "B0"), edit(T_LABEL, "B1"), edit(T2, "B2")]
    got = ct.select_ready_version("T", "B2", edits, [], [label(T_LABEL)])
    assert got["body"] == "B1"
    assert got["edit_used"] == T_LABEL


def test_the_order_of_the_edit_list_does_not_matter():
    edits = [edit(T2, "B2"), edit(T0, "B0"), edit(T1, "B1")]
    got = ct.select_ready_version("T", "B2", edits, [], [label(T_LABEL)])
    assert got["body"] == "B0"


def test_an_edit_after_the_label_with_no_version_before_it_raises():
    with pytest.raises(ValueError):
        ct.select_ready_version("T", "B1", [edit(T1, "B1")], [], [label(T_LABEL)])


def test_title_is_the_previous_title_of_the_first_rename_after_the_label():
    renames = [rename(T0, "first", "old"), rename(T1, "old", "mid"), rename(T2, "mid", "new")]
    got = ct.select_ready_version("new", "B", [], renames, [label(T_LABEL)])
    assert got["title"] == "old"


def test_title_is_the_current_title_when_no_rename_is_after_the_label():
    renames = [rename(T0, "first", "current")]
    got = ct.select_ready_version("current", "B", [], renames, [label(T_LABEL)])
    assert got["title"] == "current"


def test_the_first_ready_for_agent_label_is_the_one_that_counts():
    labels = [label(T0, "bug"), label(T1), label(T2)]
    got = ct.select_ready_version("T", "B", [], [], labels)
    assert got["ready_at"] == T1


def test_no_ready_for_agent_label_raises():
    with pytest.raises(ValueError):
        ct.select_ready_version("T", "B", [], [], [label(T0, "bug")])


# ---- acceptance rows ----

TABLE_BODY = """## For the human
- **Result:** the table holds

### Acceptance rows

| row | command | expected |
|---|---|---|
| 1 | the cause | named with file:line |
| 2 | the suite | 0 failed |

Live row (read by the brain): the next 3 merges show no FAILED line.

## Not in this ticket
- something
"""


def test_table_rows_under_the_acceptance_heading_are_returned_without_header_or_separator():
    assert ct.acceptance_rows(TABLE_BODY) == [
        "| 1 | the cause | named with file:line |",
        "| 2 | the suite | 0 failed |",
    ]


def test_numbered_lines_under_done_when_are_returned_whole():
    body = """## Done when

1. `python -m pytest x -q` passes.
2. Each entry has no path.

## Notes
1. not a row
"""
    assert ct.acceptance_rows(body) == ["1. `python -m pytest x -q` passes.", "2. Each entry has no path."]


def test_result_heading_is_accepted_and_bold_result_bullets_are_not():
    body = "- **Result:** 5 of 5 words\n\n## Result\n\n| # | check |\n|---|---|\n| 1 | a |\n"
    assert ct.acceptance_rows(body) == ["| 1 | a |"]


def test_a_continuation_line_joins_its_numbered_row():
    body = "## Acceptance\n\n1. first part\n   second part\n2. next\n"
    assert ct.acceptance_rows(body) == ["1. first part second part", "2. next"]


def test_a_section_ends_at_the_next_heading_of_the_same_level():
    body = "### Acceptance\n\n1. kept\n\n### Notes\n\n1. not kept\n"
    assert ct.acceptance_rows(body) == ["1. kept"]


def test_no_matching_heading_gives_an_empty_list():
    body = "### Facts\n\n1. a fact\n\n### Not in this ticket\n- x\n"
    assert ct.acceptance_rows(body) == []


def test_an_empty_body_gives_an_empty_list():
    assert ct.acceptance_rows("") == []


def test_crlf_line_endings_give_the_same_rows():
    body = "### Acceptance rows\r\n\r\n1. first\r\n2. second\r\n\r\nafter\r\n"
    assert ct.acceptance_rows(body) == ["1. first", "2. second"]


def test_bullet_lines_under_acceptance_are_rows():
    body = "## Acceptance\n\n- [ ] first check\n- second check\n\n## Notes\n- not a row\n"
    assert ct.acceptance_rows(body) == ["- [ ] first check", "- second check"]


def test_a_bullet_directly_under_a_numbered_row_is_its_own_row():
    body = "## Done when\n\n1. first row\n- own row\n"
    assert ct.acceptance_rows(body) == ["1. first row", "- own row"]


def test_a_continuation_line_joins_its_bullet_row():
    body = "## Result\n\n- [ ] first part\n  second part\n- next\n"
    assert ct.acceptance_rows(body) == ["- [ ] first part second part", "- next"]


def test_issue_127_body_gives_its_four_table_rows():
    rows = ct.acceptance_rows((FIXTURES / "issue127_body.md").read_text(encoding="utf-8"))
    assert len(rows) == 4
    assert rows[0].startswith("| 1 | the cause |")
    assert rows[-1] == "| 4 | `python -m tools.agentops core check` | rc 0 |"


def test_issue_238_body_gives_its_one_numbered_row():
    rows = ct.acceptance_rows((FIXTURES / "issue238_body.md").read_text(encoding="utf-8"))
    assert rows == [
        "1. `python -m pytest tests/test_issue224_wiring.py -q` repeated 20 times in a row: 20 passed.",
    ]


def test_issue_218_body_gives_its_three_numbered_rows():
    rows = ct.acceptance_rows((FIXTURES / "issue218_body.md").read_text(encoding="utf-8"))
    assert len(rows) == 3
    assert rows[0].startswith("1. `GLOSSARY.md` has an entry")
    assert rows[-1] == "3. `python -m tools.agentops core check` rc 0."


def test_issue_349_body_gives_its_five_table_rows():
    rows = ct.acceptance_rows((FIXTURES / "issue349_body.md").read_text(encoding="utf-8"))
    assert len(rows) == 5
    assert rows[0].startswith("| 1 | the three case files on main |")
    assert rows[-1].startswith("| 5 | the arms |")


def test_issue_219_body_gives_its_six_numbered_rows():
    rows = ct.acceptance_rows((FIXTURES / "issue219_body.md").read_text(encoding="utf-8"))
    assert len(rows) == 6
    assert rows[0].startswith("1. A fake suite where test T fails")
    assert rows[-1].startswith("6. `python -m tools.agentops core check` rc 0;")


def test_issue_135_body_gives_its_six_table_rows():
    rows = ct.acceptance_rows((FIXTURES / "issue135_body.md").read_text(encoding="utf-8"))
    assert len(rows) == 6
    assert rows[0].startswith("| 1 | test on a fixture transcript")
    assert rows[-1].startswith("| 6 | live, read by the brain 7 days after the merge")


# ---- base commit: first parent of the first commit on main's first-parent line that names the ticket ----


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    out = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env,
                         timeout=120)
    return out.stdout.strip()


def _commit(repo: Path, name: str, subject: str | None = None) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject or name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def merged_repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """main: a, then b, then the merge M0 of i42-sync (which itself merged main at b, so a merge of main lies on
    M0's second parent), then c, then the merge M1 of u42-x, then the merge of i420-u1 (its base is M1). Built once
    for the module: each git call on this machine takes about half a second, and the tests only read it."""
    repo = tmp_path_factory.mktemp("c_tickets_repo") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.txt")
    _git(repo, "checkout", "-q", "-b", "i42-sync")
    sync = _commit(repo, "sync.txt")
    _git(repo, "checkout", "-q", "main")
    b = _commit(repo, "b.txt")
    _git(repo, "checkout", "-q", "i42-sync")
    _git(repo, "merge", "-q", "--no-ff", "-m", "i42: merge main into i42-sync", "main")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'i42-sync'", "i42-sync")
    _commit(repo, "c.txt")
    _git(repo, "checkout", "-q", "-b", "u42-x")
    _commit(repo, "x.txt")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'u42-x'", "u42-x")
    before_i420 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "i420-u1")
    _commit(repo, "y.txt")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'i420-u1'", "i420-u1")
    return {"repo": repo, "b": b, "sync": sync, "before_i420": before_i420}


def test_base_commit_is_the_first_parent_of_the_earliest_merge_on_main(merged_repo):
    assert ct.base_commit(42, merged_repo["repo"]) == merged_repo["b"]


def test_base_commit_skips_a_second_parent_merge_of_main(merged_repo):
    # the merge of main into i42-sync is only reachable through M0's second parent: its first parent is `sync`
    assert ct.base_commit(42, merged_repo["repo"]) != merged_repo["sync"]


def test_base_commit_does_not_match_a_longer_ticket_number(merged_repo):
    # i420-u1 is a merge of ticket 420; its base is the commit main had before it (M1), not ticket 42's base
    assert ct.base_commit(420, merged_repo["repo"]) == merged_repo["before_i420"]


def test_base_commit_of_a_ticket_with_no_merge_raises(merged_repo):
    with pytest.raises(ValueError):
        ct.base_commit(7, merged_repo["repo"])


def test_base_commit_takes_a_plain_commit_whose_subject_starts_with_the_number(tmp_path: Path):
    # the earlier "State:" commit mentions #77 but does not start with it, so it is not the match
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.txt")
    state = _commit(repo, "s.txt", "State: note about #77")
    _commit(repo, "p.txt", "#77 plain commit")
    assert ct.base_commit(77, repo) == state


# ---- the matcher: which commit subject names a ticket ----


@pytest.mark.parametrize("subject, merge", [
    ("Merge branch 'i42-sync'", True),
    ("Merge branch 'u42-x'", True),
    ("Merge branch 'm42'", True),
    ("#42 the three blind replay case files", False),
    ("#42", False),
])
def test_subject_names_the_ticket(subject: str, merge: bool):
    assert ct.names_ticket(subject, 42, merge)


@pytest.mark.parametrize("subject, number, merge", [
    ("Merge branch 'i420-u1'", 42, True),
    ("Merge branch 'i4200'", 420, True),
    ("Merge branch 'r219'", 219, True),
    ("Merge branch 'a42'", 42, True),
    ("Merge pull request #349 from x/y", 349, True),
    ("#4200 something", 420, False),
    ("i42-sync note", 42, False),
    ("State: brain 23.10 #349 on Opus 5.5", 349, False),
    ("42 a bare number", 42, False),
    ("2026-10-17 daily note", 17, False),
    ("2026-10-17 daily note", 17, True),
    (" 127 fix", 127, False),
])
def test_subject_that_does_not_name_the_ticket(subject: str, number: int, merge: bool):
    assert not ct.names_ticket(subject, number, merge)


# ---- files written for each ticket ----


def test_write_files_puts_title_blank_line_and_body_in_ticket_md(tmp_path: Path):
    record = {"title": "A title", "body": "line one\r\nline two\n", "ready_at": T_LABEL, "edit_used": None}
    ct.write_files(tmp_path / "127", record, ["| 1 | a |", "| 2 | b |"], "b54711a1" + "0" * 32)
    assert (tmp_path / "127" / "ticket.md").read_bytes() == "A title\n\nline one\r\nline two\n".encode("utf-8")
    assert (tmp_path / "127" / "rows.txt").read_text(encoding="utf-8") == "| 1 | a |\n| 2 | b |\n"
    assert (tmp_path / "127" / "base.txt").read_text(encoding="utf-8") == "b54711a1" + "0" * 32 + "\n"


def test_main_writes_one_folder_per_ticket_without_touching_github(tmp_path: Path, monkeypatch):
    def fake_body(number: int, repo: str = "x") -> dict:
        return {"title": f"title {number}", "body": "## Acceptance\n\n1. row\n", "ready_at": T_LABEL, "edit_used": None}

    monkeypatch.setattr(ct, "body_at_ready", fake_body)
    monkeypatch.setattr(ct, "base_commit", lambda number, repo_dir: "c" * 40)
    assert ct.main(["5", "6", "--out-root", str(tmp_path), "--repo-dir", str(tmp_path)]) == 0
    for number in ("5", "6"):
        folder = tmp_path / number
        assert (folder / "ticket.md").read_text(encoding="utf-8") == f"title {number}\n\n## Acceptance\n\n1. row\n"
        assert (folder / "rows.txt").read_text(encoding="utf-8") == "1. row\n"
        assert (folder / "base.txt").read_text(encoding="utf-8") == "c" * 40 + "\n"


def test_main_exits_2_and_names_a_ticket_with_no_acceptance_rows(tmp_path: Path, monkeypatch, capsys):
    def fake_body(number: int, repo: str = "x") -> dict:
        return {"title": "t", "body": "## Facts\n\n1. a fact\n", "ready_at": T_LABEL, "edit_used": None}

    monkeypatch.setattr(ct, "body_at_ready", fake_body)
    monkeypatch.setattr(ct, "base_commit", lambda number, repo_dir: "c" * 40)
    assert ct.main(["9", "--out-root", str(tmp_path), "--repo-dir", str(tmp_path)]) == 2
    assert "#9" in capsys.readouterr().err
    assert not (tmp_path / "9").exists()


def test_git_gives_every_call_a_120_second_timeout(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="abc\n", stderr="")

    monkeypatch.setattr(ct.subprocess, "run", fake_run)
    assert ct._git(".", "rev-parse", "HEAD") == "abc"
    assert seen["timeout"] == 120
