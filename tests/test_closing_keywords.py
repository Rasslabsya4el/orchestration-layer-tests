"""#422: a GitHub closing keyword before a ticket reference is found in a text, and the
`closing-check` command refuses a commit range that holds one."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._git_helper import git
from tools.agentops import closing_keywords as ck
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


# #454: the helpers the agentops commit, merge and built-message steps call before they write anything.

def test_require_clean_refuses_a_closing_keyword_and_names_it():
    with pytest.raises(ck.ClosingKeywordError) as exc:
        ck.require_clean("State: x closed #425", where="commit 1a2b3c4")
    message = str(exc.value)
    assert "closed" in message
    assert "#425" in message
    assert "commit 1a2b3c4" in message


@pytest.mark.parametrize("text", ["finished #425", "#9 done", "for #3"])
def test_require_clean_passes_texts_without_a_closing_keyword(text):
    assert ck.require_clean(text) is None


def test_require_clean_refuses_when_the_check_itself_raises(monkeypatch):
    def broken(text):
        raise RuntimeError("regex engine gone")
    monkeypatch.setattr(ck, "find", broken)
    with pytest.raises(ck.ClosingKeywordError) as exc:
        ck.require_clean("finished #425")
    assert "failed" in str(exc.value)
    assert "refuses" in str(exc.value)


@pytest.mark.parametrize("text", ["Merge branch 'fix-#7'", "WIP (agentops): closes #7 ended by x"])
def test_require_clean_built_refuses_a_slug_that_carries_a_closing_keyword(text):
    with pytest.raises(ck.ClosingKeywordError):
        ck.require_clean_built(text)


@pytest.mark.parametrize("text", ["Merge branch 'fix-415-u1'", "Core update: abcd1234"])
def test_require_clean_built_passes_a_slug_without_a_reference(text):
    assert ck.require_clean_built(text) is None


def test_check_range_returns_the_sha_and_hit_of_a_commit_with_a_keyword(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Core change\n\nfixes #415")
    bad = git(repo, "rev-parse", "HEAD")
    found = ck.check_range(repo, base, "HEAD")
    assert len(found) == 1
    sha, hit = found[0]
    assert sha == bad[:7]
    assert hit.keyword == "fixes"
    assert hit.reference == "#415"

    git(repo, "commit", "-q", "--amend", "--allow-empty", "-m", "Core change\n\nRefs #415")
    assert ck.check_range(repo, base, "HEAD") == []


# #454 send-back: the built-message check also refuses a hit that the original message carries, and a hit that
# only the translated copy carries quotes the original line.

@pytest.mark.parametrize("text", [
    "closes https://github.com/o/r/issues/7",
    "WIP (agentops): closes GH-7",
    "fixes owner/repo#7",
])
def test_require_clean_built_refuses_each_reference_form_in_the_original(text):
    with pytest.raises(ck.ClosingKeywordError):
        ck.require_clean_built(text)


def test_require_clean_built_quotes_the_original_line_when_only_the_copy_holds_the_hit():
    message = "Subject line\n\nMerge branch 'fix-#7'\n"
    with pytest.raises(ck.ClosingKeywordError) as exc:
        ck.require_clean_built(message, where="this merge")
    text = str(exc.value)
    assert "line 3" in text
    assert "Merge branch 'fix-#7'" in text
    assert "'fix #7'" not in text


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("git", 60), OSError("git cannot start")])
def test_check_range_turns_a_git_timeout_or_a_start_failure_into_value_error(monkeypatch, tmp_path, error):
    def broken(rng, cwd=None):
        raise error
    monkeypatch.setattr(ck, "_commits", broken)
    with pytest.raises(ValueError):
        ck.check_range(tmp_path, "a", "b")
