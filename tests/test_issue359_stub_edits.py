"""tests/test_issue359_stub_edits.py (issue #359, unit 6 of #357): the gh stub serves a body as it stood at the moment.

A body edited after the moment is replaced by the text of the newest version made at or before it; GitHub keeps the
versions in GraphQL ``userContentEdits`` (``diff`` is the full text of the version). No network: a fake ``real_gh``
answers the CLI reads and the GraphQL history read from a table and records every argv.
The moment is 2026-10-23T13:10:30Z.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.replays import gh_stub

REPO = "acme/repo"
MOMENT = "2026-10-23T13:10:30Z"
EARLY = "2026-10-23T12:00:00Z"
BEFORE = "2026-10-23T13:02:31Z"
AFTER = "2026-10-23T13:52:36Z"


def _edits(*versions):
    """userContentEdits nodes, newest first, from (createdAt, text) pairs."""
    return {"nodes": [{"createdAt": at, "diff": text} for at, text in versions]}


def _comment(number, comment_id, body, created):
    return {"id": f"IC_{comment_id}", "body": body, "createdAt": created,
            "url": f"https://github.com/{REPO}/issues/{number}#issuecomment-{comment_id}"}


#: What the CLI answers (current texts).
CLI = {
    289: {"number": 289, "title": "map", "createdAt": EARLY, "updatedAt": AFTER, "body": "NEW BODY", "comments": []},
    290: {"number": 290, "title": "lost", "createdAt": EARLY, "updatedAt": AFTER, "body": "NEW ONLY", "comments": []},
    291: {"number": 291, "title": "calm", "createdAt": EARLY, "updatedAt": EARLY, "body": "CALM", "comments": []},
    292: {"number": 292, "title": "talked", "createdAt": EARLY, "updatedAt": AFTER, "body": "UNCHANGED", "comments": []},
    293: {"number": 293, "title": "threaded", "createdAt": EARLY, "updatedAt": AFTER, "body": "T", "comments": [
        _comment(293, 111, "c1 NEW", EARLY), _comment(293, 222, "c2 CURRENT", EARLY),
        _comment(293, 333, "c3 LATE", AFTER)]},
}
#: What the GraphQL history answers, by issue number.
HISTORY = {
    289: {"lastEditedAt": AFTER, "userContentEdits": _edits((AFTER, "NEW BODY"), (BEFORE, "OLD BODY"), (EARLY, "FIRST")),
          "comments": {"nodes": []}},
    290: {"lastEditedAt": AFTER, "userContentEdits": _edits((AFTER, "NEW ONLY")), "comments": {"nodes": []}},
    292: {"lastEditedAt": EARLY, "userContentEdits": _edits((EARLY, "UNCHANGED")), "comments": {"nodes": []}},
    293: {"lastEditedAt": None, "userContentEdits": _edits(), "comments": {"nodes": [
        {"databaseId": 111, "createdAt": EARLY, "lastEditedAt": AFTER,
         "userContentEdits": _edits((AFTER, "c1 NEW"), (EARLY, "c1 OLD"))},
        {"databaseId": 222, "createdAt": EARLY, "lastEditedAt": None, "userContentEdits": _edits()},
        {"databaseId": 333, "createdAt": AFTER, "lastEditedAt": None, "userContentEdits": _edits()}]}},
}


def _pick(item: dict, fields: list[str]) -> dict:
    return {k: item[k] for k in fields if k in item}


def fake_github(argv: list[str]) -> tuple[int, str, str]:
    if argv[:2] == ["api", "graphql"]:
        query = next(a for a in argv if a.startswith("query="))
        found = re.findall(r"(\w+):issueOrPullRequest\(number:(\d+)\)", query)
        data = {alias: HISTORY.get(int(number)) for alias, number in found}
        return 0, json.dumps({"data": {"repository": data}}) + "\n", ""
    fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else None
    if argv[:2] == ["issue", "list"]:
        return 0, json.dumps([_pick(i, fields) for i in CLI.values()]) + "\n", ""
    if argv[:2] == ["issue", "view"]:
        number = int(argv[2])
        if fields is None:
            return 0, f"title:\t{CLI[number]['title']}\n\n{CLI[number]['body']}\n", ""
        return 0, json.dumps(_pick(CLI[number], fields)) + "\n", ""
    return 1, "", f"fake: unexpected argv {argv}\n"


class FakeGh:
    def __init__(self, answer=fake_github):
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.answer(argv)

    def graphql_calls(self):
        return [c for c in self.calls if c[:2] == ["api", "graphql"]]


def run(argv, tmp_path: Path, real: FakeGh):
    return gh_stub.handle(argv, tmp_path / "state", MOMENT, REPO, real)


def test_a_body_edited_after_the_moment_is_served_as_the_older_version(tmp_path):
    real = FakeGh()
    rc, out, err = run(["issue", "view", "289", "--json", "body"], tmp_path, real)
    assert (rc, err) == (0, "")
    assert json.loads(out) == {"body": "OLD BODY"}
    assert len(real.graphql_calls()) == 1
    assert "lastEditedAt" not in out and "updatedAt" not in out


def test_b_a_body_edited_after_with_no_version_before_it_is_refused_and_nothing_leaks(tmp_path):
    rc, out, err = run(["issue", "view", "290", "--json", "body,title"], tmp_path, FakeGh())
    assert (rc, out) == (1, "")
    assert "refused" in err and "#290" in err and "edited after the moment" in err and "cannot be shown" in err
    assert "NEW ONLY" not in err


def test_c_a_body_not_edited_since_the_moment_is_unchanged_and_asks_no_graphql(tmp_path):
    real = FakeGh()
    rc, out, _ = run(["issue", "view", "291", "--json", "body"], tmp_path, real)
    assert (rc, json.loads(out)) == (0, {"body": "CALM"})
    assert real.graphql_calls() == []
    # touched after the moment (a comment bumps updatedAt) but the body itself was last edited before it
    rc, out, _ = run(["issue", "view", "292", "--json", "body"], tmp_path, real)
    assert (rc, json.loads(out)) == (0, {"body": "UNCHANGED"})


def test_d_a_comment_edited_after_the_moment_shows_its_older_text_and_the_others_stay(tmp_path):
    rc, out, err = run(["issue", "view", "293", "--json", "comments"], tmp_path, FakeGh())
    assert (rc, err) == (0, "")
    comments = json.loads(out)["comments"]
    assert [c["body"] for c in comments] == ["c1 OLD", "c2 CURRENT"]
    assert comments[0]["url"].endswith("#issuecomment-111")


def without_issue(real: FakeGh, number: int) -> FakeGh:
    """The same fake without issue `number` in the listing (its body is lost, which refuses the whole listing)."""
    def answer(argv):
        rc, out, err = fake_github(argv)
        if argv[:2] == ["issue", "list"] and rc == 0:
            out = json.dumps([r for r in json.loads(out) if r.get("number") != number]) + "\n"
        return rc, out, err
    real.answer = answer
    return real


def test_e_a_listing_row_edited_after_the_moment_shows_its_older_text(tmp_path):
    real = FakeGh()
    rc, out, err = run(["issue", "list", "--json", "body", "-s", "all"], tmp_path, without_issue(real, 290))
    assert (rc, err) == (0, "")
    assert json.loads(out) == [{"body": "OLD BODY"}, {"body": "CALM"}, {"body": "UNCHANGED"}, {"body": "T"}]
    assert len(real.graphql_calls()) == 1


def test_e2_a_listing_with_a_lost_body_is_refused_whole(tmp_path):
    rc, out, err = run(["issue", "list", "--json", "number,body"], tmp_path, FakeGh())
    assert (rc, out) == (1, "")
    assert "#290" in err and "cannot be shown" in err


def test_f_a_plain_view_of_an_issue_edited_after_the_moment_is_refused_with_the_json_advice(tmp_path):
    rc, out, err = run(["issue", "view", "289"], tmp_path, FakeGh())
    assert (rc, out) == (1, "")
    assert "refused" in err and "--json body,comments" in err
    assert "NEW BODY" not in err


def test_f2_a_plain_view_of_an_issue_not_edited_since_passes_through(tmp_path):
    rc, out, _ = run(["issue", "view", "291"], tmp_path, FakeGh())
    assert (rc, out) == (0, "title:\tcalm\n\nCALM\n")


def test_g_a_write_is_still_only_recorded(tmp_path):
    real = FakeGh()
    rc, out, _ = run(["issue", "comment", "289", "--body", "hi"], tmp_path, real)
    assert rc == 0
    assert real.calls == []
    lines = (tmp_path / "state" / "writes.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["argv"] for line in lines] == [["issue", "comment", "289", "--body", "hi"]]


def test_the_history_read_is_a_graphql_query_never_a_mutation(tmp_path):
    real = FakeGh()
    run(["issue", "view", "289", "--json", "body"], tmp_path, real)
    (call,) = real.graphql_calls()
    query = next(a for a in call if a.startswith("query="))
    assert "mutation" not in query.lower() and "userContentEdits" in query
    assert not (tmp_path / "state" / "writes.jsonl").exists()


def test_a_rest_issue_object_edited_after_the_moment_shows_its_older_text(tmp_path):
    def rest(argv):
        if argv[:1] == ["api"] and argv[1] == "repos/acme/repo/issues/289":
            return 0, json.dumps({"number": 289, "created_at": EARLY, "updated_at": AFTER, "body": "NEW BODY"}) + "\n", ""
        return fake_github(argv)
    rc, out, err = run(["api", "repos/acme/repo/issues/289"], tmp_path, FakeGh(rest))
    assert (rc, err) == (0, "")
    assert json.loads(out)["body"] == "OLD BODY"


def test_a_rest_comment_edited_after_the_moment_shows_its_older_text(tmp_path):
    def rest(argv):
        if argv[:1] == ["api"] and argv[1] == "repos/acme/repo/issues/293/comments":
            url = f"https://api.github.com/repos/{REPO}/issues/293"
            return 0, json.dumps([
                {"id": 111, "issue_url": url, "created_at": EARLY, "updated_at": AFTER, "body": "c1 NEW"},
                {"id": 222, "issue_url": url, "created_at": EARLY, "updated_at": EARLY, "body": "c2 CURRENT"}]) + "\n", ""
        return fake_github(argv)
    rc, out, _ = run(["api", "repos/acme/repo/issues/293/comments"], tmp_path, FakeGh(rest))
    assert rc == 0
    assert [c["body"] for c in json.loads(out)] == ["c1 OLD", "c2 CURRENT"]


def test_a_graphql_query_from_the_run_still_passes_through_unchanged(tmp_path):
    real = FakeGh()
    args = ["api", "graphql", "-f", "query={ repository(owner:\"a\", name:\"b\") { issue(number:289) { body } } }"]
    run(args, tmp_path, real)
    assert real.calls == [args]
