"""#422: a GitHub closing keyword before a ticket reference is found in a text, and the
`closing-check` command refuses a commit range that holds one."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._git_helper import git
from tools.agentops.closing_keywords import find

REPO_ROOT = Path(__file__).resolve().parents[1]

# home 4ece7109, `git show -s --format=%B 4ece7109`
HOME_4ECE7109 = "State: ORCH edded53e closed #348\n"

# orch-it commit 37fa812, the message as `git show -s --format=%B 37fa812` prints it in the orch-it clone
ORCHIT_37FA812 = (
    "actions_enabled: only the workflow token's 403 reads as on; review fixes\n"
    "\n"
    "https://github.com/example-owner/orchestration-layer/issues/411 unit 9 review: a rate-limit 403 and a person's\n"
    "login without admin keep raising; two tests for them; docstring says exactly this; the ticket URL and the\n"
    "duplicate test left the permissions test file.\n"
    "\n"
    "Co-Authored-By: Example Agent <noreply@example.com>\n"
)

URL = "https://github.com/example-owner/orchestration-layer/issues/411"


def test_home_4ece7109_closed_hash_348_is_refused():
    hits = find(HOME_4ECE7109)
    assert len(hits) == 1
    assert hits[0].keyword == "closed"
    assert hits[0].reference == "#348"
    assert hits[0].line == 1


def test_orchit_37fa812_fixes_then_url_on_next_paragraph_is_refused():
    hits = find(ORCHIT_37FA812)
    assert len(hits) == 1
    assert hits[0].keyword == "fixes"
    assert hits[0].reference == URL
    assert hits[0].line == 1
    assert hits[0].line_text.endswith("review fixes")


def test_resolves_with_full_url_is_refused():
    hits = find(f"Resolves {URL}\n")
    assert len(hits) == 1
    assert hits[0].keyword == "Resolves"
    assert hits[0].reference == URL


def test_line_ending_fixed_then_line_starting_with_url_is_refused_on_the_keyword_line():
    text = f"Tidy the flow\nthe test was fixed\n{URL}\n"
    hits = find(text)
    assert len(hits) == 1
    assert hits[0].keyword == "fixed"
    assert hits[0].reference == URL
    assert hits[0].line == 2
    assert hits[0].line_text == "the test was fixed"


@pytest.mark.parametrize("text", [
    f"Ticket: {URL}\n",
    f"Refs {URL}\n",
    "Refs #411\n",
    "closed by a keyword\n",
    "fixed the test\n",
    "closed by a keyword, see Ticket: #411\n",
])
def test_passing_texts_have_no_hit(text):
    assert find(text) == []


def test_owner_repo_hash_form_is_a_reference():
    hits = find("fix example-owner/orchestration-layer#411\n")
    assert len(hits) == 1
    assert hits[0].reference == "example-owner/orchestration-layer#411"


def test_gh_dash_form_is_a_reference():
    hits = find("Closes GH-411\n")
    assert len(hits) == 1
    assert hits[0].keyword == "Closes"
    assert hits[0].reference == "GH-411"


def _cli(args: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run([sys.executable, "-m", "tools.agentops", "closing-check", *args],
                          cwd=str(cwd), env=env, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def _repo_with_bad_and_good(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", f"Ticket: {URL}")
    good = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", f"fix the flow\nreview fixes\n{URL}")
    bad = git(repo, "rev-parse", "HEAD")
    return repo, base, good, bad


def test_cli_range_with_a_bad_commit_exits_1_and_names_it(tmp_path):
    repo, base, _good, bad = _repo_with_bad_and_good(tmp_path)
    proc = _cli(["--range", f"{base}..HEAD"], repo)
    assert proc.returncode == 1
    assert "fixes" in proc.stderr
    assert URL in proc.stderr
    assert "line 2" in proc.stderr
    assert bad[:7] in proc.stderr
    assert "Refs <URL>" in proc.stderr
    assert "Ticket: <URL>" in proc.stderr


def test_cli_range_with_only_good_commits_exits_0(tmp_path):
    repo, base, good, _bad = _repo_with_bad_and_good(tmp_path)
    proc = _cli(["--range", f"{base}..{good}"], repo)
    assert proc.returncode == 0, proc.stderr


def test_cli_stdin_refuses_a_pr_body_with_a_resolves_line(tmp_path):
    proc = _cli(["--stdin"], tmp_path, stdin=f"Some change\n\nResolves {URL}\n")
    assert proc.returncode == 1
    assert "Resolves" in proc.stderr


def test_cli_stdin_passes_a_ticket_line(tmp_path):
    proc = _cli(["--stdin"], tmp_path, stdin=f"Some change\n\nTicket: {URL}\n")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("text", [f"FIXES {URL}", f"Fixes: {URL}", "Fix:" + chr(10) + URL, "Closes:" + chr(10) * 2 + "#7"])
def test_case_colon_and_line_break_forms_are_refused(text):
    assert find(text)
