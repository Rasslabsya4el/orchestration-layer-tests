"""tests/test_issue349_gh_stub.py (issue #349): tools/replays/gh_stub.py, the recording `gh` stand-in.

No network: every read goes to a fake `real_gh` that records its argv and answers from a fixed table.
The moment of the replay is 2026-10-23T15:15:00Z: issue 340 was created before it, issue 350 after it.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

from tools.replays import gh_stub

REPO = "acme/repo"
MOMENT = "2026-10-23T15:15:00Z"
OLD = "2026-10-23T10:00:00Z"
NEW = "2026-10-23T16:00:00Z"

ISSUES = {
    340: {"number": 340, "title": "old", "createdAt": OLD, "state": "OPEN", "comments": [
        {"body": "early", "createdAt": OLD}, {"body": "late", "createdAt": NEW}]},
    350: {"number": 350, "title": "new", "createdAt": NEW, "state": "OPEN", "comments": []},
}
REST_ISSUES = {340: {"number": 340, "created_at": OLD}, 350: {"number": 350, "created_at": NEW}}
REST_COMMENTS_340 = [{"body": "early", "created_at": OLD}, {"body": "late", "created_at": NEW}]


def _pick(item: dict, fields: list[str]) -> dict:
    return {k: item[k] for k in fields if k in item}


def fake_github(argv: list[str]) -> tuple[int, str, str]:
    """The real gh of the fixture repo: two issues, one of them with two comments."""
    if argv[:2] in (["issue", "list"], ["search", "issues"]):
        if "--json" in argv:
            fields = argv[argv.index("--json") + 1].split(",")
            return 0, json.dumps([_pick(i, fields) for i in ISSUES.values()]) + "\n", ""
        return 0, "".join(f"{i['number']}\t{i['title']}\tOPEN\n" for i in ISSUES.values()), ""
    if argv[:2] == ["issue", "view"]:
        number = int(argv[2])
        if number not in ISSUES:
            return 1, "", f"GraphQL: Could not resolve to an Issue with the number {number}.\n"
        if "--json" in argv:
            fields = argv[argv.index("--json") + 1].split(",")
            return 0, json.dumps(_pick(ISSUES[number], fields)) + "\n", ""
        return 0, f"title: {ISSUES[number]['title']}\n", ""
    if argv[:1] == ["api"]:
        path = argv[1]
        if path == "repos/acme/repo/issues/340/comments":
            return 0, json.dumps(REST_COMMENTS_340) + "\n", ""
        if path.startswith("repos/acme/repo/issues/"):
            return 0, json.dumps(REST_ISSUES[int(path.rsplit("/", 1)[1])]) + "\n", ""
    if argv[:2] == ["repo", "view"]:
        return 0, "acme/repo\n", ""
    if argv[:2] == ["auth", "status"]:
        return 0, "Logged in to github.com\n", ""
    return 1, "", f"fake: unexpected argv {argv}\n"


class FakeGh:
    """Stands in for the real gh: records every argv it is given and answers with `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.answer(argv)


def run(argv, tmp_path: Path, real: FakeGh, moment: str = MOMENT):
    return gh_stub.handle(argv, tmp_path / "state", moment, REPO, real)


def writes(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "writes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_create_returns_stub_urls_numbered_from_9001_and_never_reaches_real_gh(tmp_path):
    real = FakeGh(fake_github)
    rc, out, err = run(["issue", "create", "--title", "t", "--body", "b"], tmp_path, real)
    assert (rc, out, err) == (0, "https://github.com/acme/repo/issues/9001\n", "")
    rc, out, _ = run(["issue", "create", "--title", "u", "--body", "c"], tmp_path, real)
    assert (rc, out) == (0, "https://github.com/acme/repo/issues/9002\n")
    assert real.calls == []
    assert (tmp_path / "state" / "counter").read_text(encoding="utf-8").strip() == "9002"
    assert [w["argv"] for w in writes(tmp_path)] == [
        ["issue", "create", "--title", "t", "--body", "b"],
        ["issue", "create", "--title", "u", "--body", "c"],
    ]


def test_pr_create_shares_the_numbering_and_answers_with_a_pull_url(tmp_path):
    real = FakeGh(fake_github)
    run(["issue", "create", "--title", "t", "--body", "b"], tmp_path, real)
    rc, out, _ = run(["pr", "create", "--title", "p", "--body", "q"], tmp_path, real)
    assert (rc, out) == (0, "https://github.com/acme/repo/pull/9002\n")


def test_body_file_is_recorded_in_full_read_at_call_time(tmp_path):
    body = "Первая строка\n\nВторая: 340 «кавычки»\n" * 3
    body_file = tmp_path / "body.md"
    body_file.write_bytes(body.encode("utf-8"))
    real = FakeGh(fake_github)
    rc, _, _ = run(["issue", "comment", "340", "--body-file", str(body_file)], tmp_path, real)
    assert rc == 0
    run(["issue", "create", "--title", "t", "-F", str(body_file)], tmp_path, real)
    recorded = writes(tmp_path)
    assert [w["body"] for w in recorded] == [body, body]
    assert all(w["at"].endswith("Z") for w in recorded)


def test_body_file_dash_is_read_from_stdin_as_bytes_with_crlf_kept(tmp_path, monkeypatch):
    data = "из stdin\r\nвторая строка\r\n".encode("utf-8")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))
    rc, _, _ = run(["issue", "edit", "340", "--body-file", "-"], tmp_path, FakeGh(fake_github))
    assert rc == 0
    assert writes(tmp_path)[-1]["body"] == "из stdin\r\nвторая строка\r\n"


def test_api_post_is_recorded_and_never_reaches_real_gh(tmp_path):
    real = FakeGh(fake_github)
    rc, out, _ = run(["api", "-X", "POST", "repos/acme/repo/issues/340/comments", "-f", "body=hi there"],
                     tmp_path, real)
    assert (rc, out) == (0, "{}\n")
    rc, _, _ = run(["api", "repos/acme/repo/issues/340/comments", "-f", "body=no method"], tmp_path, real)
    assert rc == 0
    assert real.calls == []
    recorded = writes(tmp_path)
    assert [w["body"] for w in recorded] == ["hi there", "no method"]
    assert recorded[0]["argv"] == ["api", "-X", "POST", "repos/acme/repo/issues/340/comments", "-f", "body=hi there"]


def test_api_field_at_file_is_read_and_api_issue_create_gets_a_number(tmp_path):
    body_file = tmp_path / "b.md"
    body_file.write_bytes("из файла\n".encode("utf-8"))
    real = FakeGh(fake_github)
    rc, out, _ = run(["api", "-X", "POST", "repos/acme/repo/issues/340/comments", "-F", f"body=@{body_file}"],
                     tmp_path, real)
    assert rc == 0 and real.calls == []
    assert writes(tmp_path)[-1]["body"] == "из файла\n"
    rc, out, _ = run(["api", "-X", "POST", "repos/acme/repo/issues", "-f", "title=t", "-f", "body=b"], tmp_path, real)
    assert json.loads(out) == {"number": 9001, "html_url": "https://github.com/acme/repo/issues/9001"}
    assert real.calls == []


def test_issue_list_json_drops_issue_created_after_moment_and_keeps_older(tmp_path):
    real = FakeGh(fake_github)
    rc, out, err = run(["issue", "list", "--json", "number,title"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out) == [{"number": 340, "title": "old"}]
    assert real.calls == [["issue", "list", "--json", "number,title,createdAt"]]


def test_issue_list_plain_is_filtered_by_number_through_a_json_probe(tmp_path):
    real = FakeGh(fake_github)
    rc, out, _ = run(["issue", "list", "--state", "all"], tmp_path, real)
    assert rc == 0
    assert "340\told\tOPEN\n" == out
    assert ["issue", "list", "--state", "all", "--json", "number,createdAt"] in real.calls


def test_issue_view_of_hidden_issue_is_rc1_with_gh_message(tmp_path):
    real = FakeGh(fake_github)
    for argv in (["issue", "view", "350"], ["issue", "view", "350", "--json", "number,title"]):
        rc, out, err = run(argv, tmp_path, real)
        assert rc == 1 and out == ""
        assert err == "GraphQL: Could not resolve to an Issue with the number 350.\n"


def test_issue_view_of_visible_issue_passes_through_unchanged(tmp_path):
    real = FakeGh(fake_github)
    assert run(["issue", "view", "340"], tmp_path, real) == (0, "title: old\n", "")
    assert real.calls == [["issue", "view", "340", "--json", "createdAt,updatedAt"], ["issue", "view", "340"]]


def test_issue_view_json_without_createdAt_gets_it_added_and_removed(tmp_path):
    real = FakeGh(fake_github)
    rc, out, _ = run(["issue", "view", "340", "--json", "number,title"], tmp_path, real)
    assert rc == 0
    assert json.loads(out) == {"number": 340, "title": "old"}
    assert real.calls == [["issue", "view", "340", "--json", "number,title,createdAt"]]


def test_comments_created_after_moment_are_hidden_on_a_visible_issue(tmp_path):
    real = FakeGh(fake_github)
    rc, out, _ = run(["issue", "view", "340", "--json", "number,comments"], tmp_path, real)
    assert rc == 0
    assert json.loads(out) == {"number": 340, "comments": [{"body": "early", "createdAt": OLD}]}
    rc, out, _ = run(["api", "repos/acme/repo/issues/340/comments"], tmp_path, real)
    assert rc == 0
    assert json.loads(out) == [{"body": "early", "created_at": OLD}]


def test_api_get_of_hidden_single_issue_is_not_found(tmp_path):
    rc, out, err = run(["api", "repos/acme/repo/issues/350"], tmp_path, FakeGh(fake_github))
    assert (rc, out) == (1, "")
    assert "Not Found" in err


def test_reads_the_stub_cannot_filter_exactly_are_refused_not_leaked(tmp_path):
    real = FakeGh(fake_github)
    for argv in (["issue", "list", "--json", "number", "--jq", ".[].number"],
                 ["issue", "view", "340", "--comments"],
                 ["api", "repos/acme/repo/issues/340/comments", "--jq", ".[].body"]):
        rc, out, err = run(argv, tmp_path, real)
        assert (rc, out) == (1, "")
        assert "refused" in err
    assert real.calls == []


def test_paginated_api_list_is_filtered_page_by_page(tmp_path):
    def pages(argv):
        if argv[1] == "repos/acme/repo/issues":
            return 0, (json.dumps([{"number": 340, "created_at": OLD}])
                       + json.dumps([{"number": 350, "created_at": NEW}])) + "\n", ""
        return fake_github(argv)
    rc, out, _ = run(["api", "repos/acme/repo/issues", "--paginate"], tmp_path, FakeGh(pages))
    assert rc == 0
    assert [json.loads(line) for line in out.splitlines()] == [[{"number": 340, "created_at": OLD}], []]


@pytest.mark.parametrize("argv", [["repo", "view"], ["auth", "status"]])
def test_other_reads_pass_through_unchanged(tmp_path, argv):
    real = FakeGh(fake_github)
    rc, out, err = run(argv, tmp_path, real)
    assert real.calls == [argv]
    assert (rc, out, err) == fake_github(argv)


def test_main_reads_the_environment_and_records_a_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPLAY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPLAY_MOMENT", MOMENT)
    monkeypatch.setenv("REPLAY_REPO", REPO)
    monkeypatch.setenv("REPLAY_REAL_GH", str(tmp_path / "no-such-gh.exe"))
    monkeypatch.setattr(sys, "argv", ["gh_stub", "issue", "create", "--title", "t", "--body", "b"])
    assert gh_stub.main() == 0
    assert capsys.readouterr().out == "https://github.com/acme/repo/issues/9001\n"
    assert writes(tmp_path)[-1]["body"] == "b"


def test_repo_flag_before_the_verb_is_a_recorded_write_never_real(tmp_path):
    real = FakeGh(fake_github)
    rc, out, err = run(["issue", "-R", "o/r", "create", "--title", "t", "--body", "b"], tmp_path, real)
    assert (rc, out, err) == (0, "https://github.com/o/r/issues/9001\n", "")
    assert real.calls == []
    assert [w["argv"] for w in writes(tmp_path)] == [["issue", "-R", "o/r", "create", "--title", "t", "--body", "b"]]
    assert writes(tmp_path)[0]["created"]["number"] == 9001


def test_listing_with_other_repo_flag_is_filtered_by_the_same_moment(tmp_path):
    real = FakeGh(fake_github)
    rc, out, err = run(["issue", "list", "-R", "other/repo", "--json", "number,title"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out) == [{"number": 340, "title": "old"}]
    assert real.calls == [["issue", "list", "-R", "other/repo", "--json", "number,title,createdAt"]]


def _search_api(argv):
    if argv[:1] == ["api"] and "search/issues" in argv:
        items = [{"number": 340, "created_at": OLD}, {"number": 350, "created_at": NEW}]
        return 0, json.dumps({"total_count": 2, "items": items}) + "\n", ""
    return fake_github(argv)


def test_api_search_issues_is_filtered_by_created_at(tmp_path):
    real = FakeGh(_search_api)
    rc, out, err = run(["api", "-X", "GET", "search/issues", "-f", "q=repo:acme/repo"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out)["items"] == [{"number": 340, "created_at": OLD}]
    assert real.calls == [["api", "-X", "GET", "search/issues", "-f", "q=repo:acme/repo"]]


def _search_prs(argv):
    if argv[:2] == ["search", "prs"]:
        fields = argv[argv.index("--json") + 1].split(",")
        return 0, json.dumps([_pick(i, fields) for i in ISSUES.values()]) + "\n", ""
    return fake_github(argv)


def test_search_prs_is_filtered_by_the_moment(tmp_path):
    real = FakeGh(_search_prs)
    rc, out, err = run(["search", "prs", "fix", "--json", "number,title"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out) == [{"number": 340, "title": "old"}]
    assert real.calls == [["search", "prs", "fix", "--json", "number,title,createdAt"]]


def test_issue_view_of_a_stub_created_number_is_answered_from_the_record(tmp_path):
    real = FakeGh(fake_github)
    run(["issue", "create", "--title", "t", "--body", "b"], tmp_path, real)
    rc, out, err = run(["issue", "view", "9001"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert out.startswith("title:\tt\nstate:\tOPEN\nurl:\thttps://github.com/acme/repo/issues/9001\n")
    assert out.endswith("\n\nb\n")
    rc, out, _ = run(["issue", "view", "9001", "--json", "number,title,body"], tmp_path, real)
    assert (rc, json.loads(out)) == (0, {"number": 9001, "title": "t", "body": "b"})
    assert real.calls == []


def test_jq_and_template_refusals_name_the_json_workaround(tmp_path):
    real = FakeGh(fake_github)
    listing = run(["issue", "list", "--json", "number", "--jq", ".[].number"], tmp_path, real)
    view = run(["issue", "view", "340", "--json", "number", "--jq", ".number"], tmp_path, real)
    templ = run(["search", "issues", "x", "--json", "number", "--template", "{{.number}}"], tmp_path, real)
    for rc, out, err in (listing, view, templ):
        assert (rc, out) == (1, "")
        assert "refused" in err and "--json" in err
    assert real.calls == []


def test_graphql_query_passes_through_to_real_gh(tmp_path):
    def graphql(argv):
        if argv[:2] == ["api", "graphql"]:
            return 0, json.dumps({"data": {"viewer": {"login": "x"}}}) + "\n", ""
        return fake_github(argv)
    real = FakeGh(graphql)
    args = ["api", "graphql", "-f", "query={ viewer { login } }"]
    rc, out, err = run(args, tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out) == {"data": {"viewer": {"login": "x"}}}
    assert real.calls == [args]
    assert writes(tmp_path) == []


def test_graphql_mutation_is_recorded_and_never_passed_to_real_gh(tmp_path):
    real = FakeGh(fake_github)
    args = ["api", "graphql", "-f", "query=mutation { addComment(input: {}) { clientMutationId } }", "-f", "body=hi"]
    rc, out, _ = run(args, tmp_path, real)
    assert (rc, out) == (0, "{}\n")
    assert real.calls == []
    recorded = writes(tmp_path)
    assert recorded[-1]["argv"] == args
    assert recorded[-1]["body"] == "hi"


@pytest.mark.parametrize("argv", [
    ["workflow", "run", "ci.yml"],
    ["release", "create", "v1", "--title", "t", "--notes", "n"],
    ["repo", "create", "acme/new", "--public"],
    ["gist", "create", "--desc", "d"],
    ["secret", "set", "TOKEN", "--body", "x"],
    ["run", "rerun", "123"],
    ["pr", "ready", "7"],
    ["issue", "pin", "340"],
])
def test_unlisted_verbs_are_recorded_writes_never_real(tmp_path, argv):
    real = FakeGh(fake_github)
    rc, _, err = run(argv, tmp_path, real)
    assert rc == 0 and err == ""
    assert real.calls == []
    assert [w["argv"] for w in writes(tmp_path)] == [argv]


def test_body_file_changed_after_the_call_still_records_the_content_at_call_time(tmp_path):
    body_file = tmp_path / "late.md"
    body_file.write_bytes("как было\n".encode("utf-8"))
    run(["issue", "comment", "340", "--body-file", str(body_file)], tmp_path, FakeGh(fake_github))
    body_file.write_bytes("как стало\n".encode("utf-8"))
    assert writes(tmp_path)[-1]["body"] == "как было\n"


def test_issue_edit_echoes_the_number_after_a_value_flag(tmp_path):
    real = FakeGh(fake_github)
    rc, out, err = run(["issue", "edit", "--add-label", "5", "340"], tmp_path, real)
    assert (rc, out, err) == (0, "https://github.com/acme/repo/issues/340\n", "")
    assert real.calls == []
