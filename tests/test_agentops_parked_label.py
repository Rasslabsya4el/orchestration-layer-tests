"""#1074 part 1: `tools.agentops.parked_label` rewrites the parked titles of one repo through a fake gh runner.

No network: every `gh` call goes to a FakeGh that answers from a list of issues held in memory.
"""
import json
import subprocess

import pytest

from tools.agentops import parked_label as pl

REPO = "owner/name"
CONDITION = "until the day-1 prototype is accepted"


def _result(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(args, rc, out, err)


class FakeGh:
    """Answers `gh issue list`, `gh label list`, `gh label create`, `gh issue edit`; records every call."""

    def __init__(self, issues, labels=(), edit_rc=0):
        self.issues = issues
        self.labels = list(labels)
        self.edit_rc = edit_rc
        self.calls = []  # (args, stdin input or None)

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs.get("input")))
        head = args[:2]
        if head == ["issue", "list"]:
            return _result(args, out=json.dumps(self.issues))
        if head == ["label", "list"]:
            return _result(args, out=json.dumps([{"name": n} for n in self.labels]))
        if head == ["label", "create"]:
            self.labels.append(args[2])
            return _result(args)
        if head == ["issue", "edit"]:
            return _result(args, rc=self.edit_rc, err="" if not self.edit_rc else "HTTP 500 boom")
        return _result(args, rc=1, err="unexpected call")

    def writes(self):
        return [(a, i) for a, i in self.calls if a[:2] in (["issue", "edit"], ["label", "create"])]

    def edit_for(self, number):
        for args, stdin in self.writes():
            if args[:2] == ["issue", "edit"] and args[2] == str(number):
                return args, stdin
        return None


def _issue(number, title, body=""):
    return {"number": number, "title": title, "body": body}


# --- the title rewrite -------------------------------------------------------------------------------------

@pytest.mark.parametrize("old, new, condition", [
    ("X (parked until the day-1 prototype is accepted)", "X", CONDITION),
    ("Parked: code-shape findings", "code-shape findings", None),
    ("Y (parked)", "Y", None),
    ("Z (PARKED)", "Z", None),
    ("Crafting oracle (parked) - part 2", "Crafting oracle - part 2", None),
])
def test_title_loses_the_parked_phrase(old, new, condition):
    assert pl.split_title(old) == (new, condition)


def test_detection_matches_the_title_word_case_insensitively():
    assert pl.is_parked("Y (parked)")
    assert pl.is_parked("Parked: code-shape findings")
    assert pl.is_parked("x (Parked until later)")
    assert not pl.is_parked("Build a day-1 skeleton")


def test_a_title_that_is_only_the_phrase_is_refused_not_written():
    with pytest.raises(ValueError):
        pl.split_title("(parked)")


# --- the run: one row per issue, edits only when not dry ---------------------------------------------------

def test_run_rewrites_titles_labels_and_adds_the_condition_line_on_top_of_the_body():
    gh = FakeGh([
        _issue(502, "Loadout map (parked until the day-1 prototype is accepted)", "## For the human\nbody"),
        _issue(1039, "Parked: code-shape findings", "## For the human\nbody"),
        _issue(7, "Build a day-1 skeleton", "untouched"),
    ], labels=["parked"])
    rows = pl.run(REPO, dry_run=False, runner=gh)

    assert [(r.number, r.old_title, r.new_title) for r in rows] == [
        (502, "Loadout map (parked until the day-1 prototype is accepted)", "Loadout map"),
        (1039, "Parked: code-shape findings", "code-shape findings"),
    ]
    args, stdin = gh.edit_for(502)
    assert args[args.index("--title") + 1] == "Loadout map"
    assert args[args.index("--add-label") + 1] == "parked"
    assert stdin.startswith(f"Parked: {CONDITION}\n")
    assert stdin.endswith("## For the human\nbody")
    args, stdin = gh.edit_for(1039)
    assert args[args.index("--title") + 1] == "code-shape findings"
    assert stdin is None  # no condition: the body is left as it is
    assert gh.edit_for(7) is None


def test_a_body_that_already_states_the_condition_is_not_doubled():
    body = f"Parked: {CONDITION}\n\n## For the human\nbody"
    gh = FakeGh([_issue(502, f"Loadout map (parked {CONDITION})", body)], labels=["parked"])
    pl.run(REPO, dry_run=False, runner=gh)
    args, stdin = gh.edit_for(502)
    sent = stdin if stdin is not None else body
    assert sent.count("Parked:") == 1


def test_dry_run_writes_nothing_and_still_lists_the_rows(capsys):
    gh = FakeGh([_issue(1, "Y (parked)", "body")], labels=[])
    rows = pl.run(REPO, dry_run=True, runner=gh)
    assert [(r.number, r.new_title) for r in rows] == [(1, "Y")]
    assert gh.writes() == []

    assert pl.main(["--repo", REPO, "--dry-run"], runner=gh) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "Y (parked)" in out and "Y" in out
    assert gh.writes() == []


def test_the_label_is_created_only_when_missing_and_not_on_dry_run():
    gh = FakeGh([_issue(1, "Y (parked)")], labels=[])
    pl.run(REPO, dry_run=False, runner=gh)
    creates = [a for a, _ in gh.writes() if a[:2] == ["label", "create"]]
    assert len(creates) == 1
    assert creates[0][2] == "parked"
    assert creates[0][creates[0].index("--description") + 1] == "waiting on a named event; not worked until it comes"

    gh = FakeGh([_issue(1, "Y (parked)")], labels=["parked"])
    pl.run(REPO, dry_run=False, runner=gh)
    assert not [a for a, _ in gh.writes() if a[:2] == ["label", "create"]]

    gh = FakeGh([_issue(1, "Y (parked)")], labels=[])
    pl.run(REPO, dry_run=True, runner=gh)
    assert gh.writes() == []


def test_a_failed_edit_is_a_row_error_and_the_exit_code_is_one(capsys):
    gh = FakeGh([_issue(1, "Y (parked)")], labels=["parked"], edit_rc=1)
    rows = pl.run(REPO, dry_run=False, runner=gh)
    assert rows[0].error and "HTTP 500" in rows[0].error
    assert pl.main(["--repo", REPO], runner=FakeGh([_issue(1, "Y (parked)")], labels=["parked"], edit_rc=1)) == 1


def test_main_without_dry_run_edits_and_exits_zero(capsys):
    gh = FakeGh([_issue(3, "Monster and boss damage system (parked)")], labels=["parked"])
    assert pl.main(["--repo", REPO], runner=gh) == 0
    args, _ = gh.edit_for(3)
    assert args[args.index("--title") + 1] == "Monster and boss damage system"
    assert "#3" in capsys.readouterr().out


# --- send-back (#1074): condition punctuation, bare-word tail, word boundary, unclosed paren ----------------

@pytest.mark.parametrize("old, condition", [
    ("Foo (parked: waiting for X)", "waiting for X"),
    ("Foo (parked, waiting)", "waiting"),
    ("Foo (parked - x)", "x"),
    ("Foo parked - x", "x"),
])
def test_condition_loses_its_leading_punctuation(old, condition):
    assert pl.split_title(old) == ("Foo", condition)


def test_the_body_line_carries_the_cleaned_condition():
    assert pl.body_with_condition("## For the human", "waiting for X").startswith("Parked: waiting for X\n")
    gh = FakeGh([_issue(9, "Foo (parked: waiting for X)", "## For the human")], labels=["parked"])
    pl.run(REPO, dry_run=False, runner=gh)
    _, stdin = gh.edit_for(9)
    assert stdin.startswith("Parked: waiting for X\n")


def test_a_bare_word_mid_title_keeps_the_title_before_it_and_the_tail_as_condition(capsys):
    assert pl.split_title("Foo parked - bar baz") == ("Foo", "bar baz")
    gh = FakeGh([_issue(4, "Foo parked - bar baz", "body")], labels=["parked"])
    rows = pl.run(REPO, dry_run=True, runner=gh)
    assert rows[0].condition == "bar baz"
    assert pl.main(["--repo", REPO, "--dry-run"], runner=gh) == 0
    assert "bar baz" in capsys.readouterr().out


def test_the_word_boundary_keeps_unparked_and_parkedness_out():
    assert not pl.is_parked("Unparked build")
    assert not pl.is_parked("Parkedness notes")
    assert pl.is_parked("Foo (parked)")
    gh = FakeGh([_issue(1, "Unparked build"), _issue(2, "Parkedness notes")], labels=["parked"])
    assert pl.run(REPO, dry_run=False, runner=gh) == []
    assert gh.writes() == []


@pytest.mark.parametrize("old, new, condition", [
    ("Foo (parked until X", "Foo", "until X"),
    ("Foo (parked", "Foo", None),
    ("Foo (x (parked)", "Foo x", None),
])
def test_an_unclosed_paren_before_parked_leaves_no_stray_paren(old, new, condition):
    assert pl.split_title(old) == (new, condition)


# --- addendum (#1074): malformed gh replies exit 2 with one line; one regex for detection and split ---------

def _reply_runner(replies):
    """A runner that answers each gh subcommand (the first two args) with the given text."""
    def runner(args, **kwargs):
        out = replies.get(tuple(args[:2]))
        if out is None:
            return _result(args, rc=1, err="unexpected call")
        return _result(args, out=out)
    return runner


@pytest.mark.parametrize("replies", [
    {("issue", "list"): "not json"},
    {("issue", "list"): json.dumps([_issue(1, "Y (parked)")]), ("label", "list"): "not json"},
    {("issue", "list"): '{"number": 1}', ("label", "list"): "[]"},
    {("issue", "list"): json.dumps(["not an issue"]), ("label", "list"): "[]"},
])
def test_a_malformed_gh_reply_exits_two_with_one_line_and_no_traceback(replies, capsys):
    rc = pl.main(["--repo", REPO], runner=_reply_runner(replies))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("error: ")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("old, new, condition", [
    ("X (parked until the day-1 prototype is accepted)", "X", CONDITION),
    ("Parked: code-shape findings", "code-shape findings", None),
    ("Y (parked)", "Y", None),
    ("Foo parked - bar baz", "Foo", "bar baz"),
    ("Foo (parked until X", "Foo", "until X"),
])
def test_one_detector_accepts_a_title_and_its_split_changes_it(old, new, condition):
    assert pl.is_parked(old)
    assert pl.split_title(old) == (new, condition)
    assert pl.split_title(old)[0] != old
    assert not pl.is_parked(pl.split_title(old)[0])


def test_unparked_build_is_neither_matched_nor_split():
    assert not pl.is_parked("Unparked build")
    with pytest.raises(ValueError):
        pl.split_title("Unparked build")


def test_a_skipped_number_is_left_alone_even_when_its_title_carries_the_word(capsys):
    gh = FakeGh([
        _issue(1074, "Parked tickets carry a label, not the word in the title", "b"),
        _issue(28, "Crafting oracle (parked)", "b"),
    ], labels=["parked"])
    assert pl.main(["--repo", REPO, "--skip", "1074"], runner=gh) == 0
    assert gh.edit_for(1074) is None
    assert gh.edit_for(28) is not None
    assert "1 parked issue(s)" in capsys.readouterr().out
