"""tools/agentops/core_issue.py (issue #263): `core issue`, run in a repo set up as a target of
the shared orchestration core. No network: `gh` is a recorded stand-in (the pattern of
tests/test_agentops_core_update.py), so no test writes to a real tracker."""

from __future__ import annotations

import argparse
import json
import re
import subprocess

import pytest

from tools.agentops import core_check, core_issue
from tools.agentops.util import SESSION_ID_ENV_VARS

HOME = "someone/home-repo"
REPO = "someone/target-repo"

MANIFEST = [
    "docs/agents/orch.md",
    "docs/agents/workflow.md",
    "tools/agentops/dispatch.py",
    "tools/agentops/core_issue.py",
]

SETTINGS = {
    "repo": REPO,
    "home": HOME,
    "core_version": "abc123abc123",
    "update_rule": "word",
}


def _manifest_path(tmp_path):
    path = tmp_path / "core_manifest.txt"
    path.write_text("\n".join(MANIFEST) + "\n", encoding="utf-8")
    return path


def _result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class RecordingGh:
    """Serves canned stdout for exact-match calls, records every call, and lets a test register
    a handler for calls whose arguments vary (the DELETE calls of the cap case)."""

    def __init__(self, table: dict[tuple, str], *, handlers=None):
        self.table = table
        self.handlers = handlers or []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        key = tuple(args)
        if key in self.table:
            return _result(stdout=self.table[key])
        for predicate, respond in self.handlers:
            if predicate(args):
                return respond(args)
        raise AssertionError(f"unexpected gh call: {args}")


def _base_kwargs(tmp_path, same=None):
    return dict(
        role="worker",
        kind="broke",
        what="tools/agentops/dispatch.py: the dispatcher log format changed",
        error="tools/agentops/dispatch.py raised KeyError on 'sessions'",
        log="docs/agents/orch.md: the checkpoint step never ran",
        did="worked around",
        session="sess-1",
        same=same,
        caught_by="suite",
        settings=SETTINGS,
        cwd=tmp_path,
        manifest_path=_manifest_path(tmp_path),
    )


def _issue_url(number: int) -> str:
    return f"https://github.com/{HOME}/issues/{number}\n"


# -- row 1: a call with every field, repo set up as a target, a parent already exists -----------

def test_full_call_writes_one_issue_with_nine_fields_and_attaches_to_the_parent(tmp_path) -> None:
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": 600, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/600/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([{"id": 90001, "number": 501, "state": "open", "closed_at": None}]),
    }

    def is_issue_create(args):
        return args[:2] == ["issue", "create"]

    def is_get_id(args):
        return args[:2] == ["api", f"repos/{HOME}/issues/501"]

    def is_attach(args):
        return args[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/600/sub_issues"]

    handlers = [
        (is_issue_create, lambda args: _result(stdout=_issue_url(501))),
        (is_get_id, lambda args: _result(stdout="90001\n")),
        (is_attach, lambda args: _result(stdout="")),
    ]
    gh = RecordingGh(table, handlers=handlers)

    result = core_issue.write_core_issue(gh_runner=gh, **{**_base_kwargs(tmp_path), "kind": "change"})

    assert result.created_issue == 501
    assert result.parent_issue == 600
    assert result.comment_added is False
    assert result.removed == []

    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    title = create_calls[0][create_calls[0].index("--title") + 1]
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert title.startswith(REPO + ":")
    assert len(re.findall(r"\*\*[a-z_ ]+:\*\*", body)) == 9

    attach_calls = [c for c in gh.calls if c[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/600/sub_issues"]]
    assert len(attach_calls) == 1
    assert "-F" in attach_calls[0] and "sub_issue_id=90001" in attach_calls[0]


# -- row 2: no parent exists ---------------------------------------------------------------------

def test_no_parent_exists_creates_one_then_attaches(tmp_path) -> None:
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): "[]",
        ("label", "create", "core-epic", "-R", HOME, "--color", core_issue.CORE_EPIC_LABEL_META["color"],
         "--description", core_issue.CORE_EPIC_LABEL_META["description"], "--force"): "",
        ("api", f"repos/{HOME}/issues/700/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([{"id": 90002, "number": 502, "state": "open", "closed_at": None}]),
    }

    def is_issue_create(args):
        return args[:2] == ["issue", "create"] and "--label" in args and args[args.index("--label") + 1] != "core-epic"

    def is_parent_create(args):
        return args[:2] == ["issue", "create"] and "core-epic" in args

    def is_get_id(args):
        return args[:2] == ["api", f"repos/{HOME}/issues/502"]

    def is_attach(args):
        return args[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/700/sub_issues"]

    handlers = [
        (is_parent_create, lambda args: _result(stdout=_issue_url(700))),
        (is_issue_create, lambda args: _result(stdout=_issue_url(502))),
        (is_get_id, lambda args: _result(stdout="90002\n")),
        (is_attach, lambda args: _result(stdout="")),
    ]
    gh = RecordingGh(table, handlers=handlers)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    assert result.created_issue == 502
    assert result.parent_issue == 700
    parent_creates = [c for c in gh.calls if is_parent_create(c)]
    assert len(parent_creates) == 1


# -- row 3: a field holds a path outside the manifest --------------------------------------------

def test_a_path_outside_the_manifest_refuses_with_nothing_written(tmp_path) -> None:
    gh = RecordingGh({})
    kwargs = _base_kwargs(tmp_path)
    kwargs["what"] = "src/not_in_manifest.py: blew up"

    with pytest.raises(core_issue.CoreIssueError) as excinfo:
        core_issue.write_core_issue(gh_runner=gh, **kwargs)

    assert "src/not_in_manifest.py" in str(excinfo.value)
    assert gh.calls == []


def test_error_and_log_are_free_text_and_never_refused(tmp_path) -> None:
    error_text = "veto/approve and a lone / and tools/agentops/provisional.py"
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": 600, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/600/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([{"id": 90001, "number": 503, "state": "open", "closed_at": None}]),
    }
    handlers = [
        (lambda a: a[:2] == ["issue", "create"], lambda a: _result(stdout=_issue_url(503))),
        (lambda a: a[:2] == ["api", f"repos/{HOME}/issues/503"], lambda a: _result(stdout="90001\n")),
        (lambda a: a[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/600/sub_issues"],
         lambda a: _result(stdout="")),
    ]
    gh = RecordingGh(table, handlers=handlers)
    kwargs = _base_kwargs(tmp_path)
    kwargs["error"] = error_text
    kwargs["log"] = "src/not_in_manifest.py / other/thing.md"

    result = core_issue.write_core_issue(gh_runner=gh, **kwargs)

    assert result.created_issue == 503
    creates = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(creates) == 1
    assert error_text in creates[0][creates[0].index("--body") + 1]


# -- row 4: --same N of an open core issue --------------------------------------------------------

def test_same_defect_writes_one_comment_and_no_new_issue(tmp_path) -> None:
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): json.dumps(
            [{"number": 501, "title": "x", "body": "**what failed:** tools/agentops/dispatch.py: ..."}]
        ),
        ("issue", "comment", "501", "-R", HOME, "--body", core_issue._comment_body(
            REPO, SETTINGS["core_version"], "sess-1")): "",
    }
    gh = RecordingGh(table)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path, same=501))

    assert result.created_issue is None
    assert result.comment_added is True
    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert create_calls == []
    comment_calls = [c for c in gh.calls if c[:2] == ["issue", "comment"]]
    assert len(comment_calls) == 1


# -- row 5: the cap ---------------------------------------------------------------------------

def test_cap_removes_exactly_the_ten_oldest_closed_and_keeps_every_open_one(tmp_path) -> None:
    old_open = [{"id": 1000 + i, "number": i, "state": "open", "closed_at": None} for i in range(60)]
    old_closed = [
        {"id": 2000 + i, "number": 1000 + i, "state": "closed", "closed_at": f"2026-01-{i + 1:02d}T00:00:00Z"}
        for i in range(30)
    ]
    new_id = 90003
    all_subs = old_open + old_closed + [{"id": new_id, "number": 503, "state": "open", "closed_at": None}]
    assert len(all_subs) == 91
    pre_attach = old_open + old_closed  # the parent as it stands before the new issue is attached (#1112)

    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": 800, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/800/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([pre_attach[:50], pre_attach[50:]]),
    }

    def is_issue_create(args):
        return args[:2] == ["issue", "create"]

    def is_get_id(args):
        return args[:2] == ["api", f"repos/{HOME}/issues/503"]

    def is_attach(args):
        return args[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/800/sub_issues"]

    def is_detach(args):
        return args[:4] == ["api", "-X", "DELETE", f"repos/{HOME}/issues/800/sub_issue"]

    handlers = [
        (is_issue_create, lambda args: _result(stdout=_issue_url(503))),
        (is_get_id, lambda args: _result(stdout=f"{new_id}\n")),
        (is_attach, lambda args: _result(stdout="")),
        (is_detach, lambda args: _result(stdout="")),
    ]
    gh = RecordingGh(table, handlers=handlers)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    detach_calls = [c for c in gh.calls if is_detach(c)]
    assert len(detach_calls) == 10
    removed_ids = {int(c[c.index("-F") + 1].split("=", 1)[1]) for c in detach_calls}
    expected_removed_ids = {2000 + i for i in range(10)}  # the ten oldest closed_at values
    assert removed_ids == expected_removed_ids
    assert result.removed and set(result.removed) == expected_removed_ids

    removed_states = {sub["id"]: sub["state"] for sub in all_subs}
    assert all(removed_states[i] == "closed" for i in removed_ids)

    assert len(pre_attach) == 90
    assert len(pre_attach) - len(detach_calls) + 1 == 81  # the attach adds the new one back


# -- #1112: make room before the attach ----------------------------------------------------------

PARENT_1112 = 900
NEW_1112 = 510
NEW_ID_1112 = 91000


def _children_of(open_count: int, closed_count: int) -> list[dict]:
    open_subs = [{"id": 1000 + i, "number": 1000 + i, "state": "open", "closed_at": None}
                 for i in range(open_count)]
    closed_subs = [
        {"id": 2000 + i, "number": 2000 + i, "state": "closed",
         "closed_at": f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z"}
        for i in range(closed_count)
    ]
    return open_subs + closed_subs


def _parent_1112_table(children: list[dict]) -> dict:
    return {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": PARENT_1112, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/{PARENT_1112}/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([children]),
    }


def _attach_args_1112() -> list[str]:
    return ["api", "-X", "POST", f"repos/{HOME}/issues/{PARENT_1112}/sub_issues"]


def _detach_args_1112() -> list[str]:
    return ["api", "-X", "DELETE", f"repos/{HOME}/issues/{PARENT_1112}/sub_issue"]


def _gh_for_parent_1112(children: list[dict]) -> RecordingGh:
    def is_issue_create(args):
        return args[:2] == ["issue", "create"]

    def is_get_id(args):
        return args[:2] == ["api", f"repos/{HOME}/issues/{NEW_1112}"]

    def is_attach(args):
        return args[:4] == _attach_args_1112()

    def is_detach(args):
        return args[:4] == _detach_args_1112()

    handlers = [
        (is_issue_create, lambda args: _result(stdout=_issue_url(NEW_1112))),
        (is_get_id, lambda args: _result(stdout=f"{NEW_ID_1112}\n")),
        (is_attach, lambda args: _result(stdout="")),
        (is_detach, lambda args: _result(stdout="")),
    ]
    return RecordingGh(_parent_1112_table(children), handlers=handlers)


def _indexes(gh: RecordingGh, prefix: list[str]) -> list[int]:
    return [i for i, call in enumerate(gh.calls) if call[:len(prefix)] == prefix]


def _detached_ids(gh: RecordingGh) -> set[int]:
    return {int(gh.calls[i][gh.calls[i].index("-F") + 1].split("=", 1)[1])
            for i in _indexes(gh, _detach_args_1112())}


def test_1112_row1_full_parent_loses_its_twenty_oldest_closed_before_the_attach(tmp_path) -> None:
    children = list(reversed(_children_of(open_count=5, closed_count=95)))
    gh = _gh_for_parent_1112(children)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    detach_at = _indexes(gh, _detach_args_1112())
    attach_at = _indexes(gh, _attach_args_1112())
    assert len(detach_at) == 20
    assert len(attach_at) == 1
    assert max(detach_at) < attach_at[0]
    assert _detached_ids(gh) == {2000 + i for i in range(20)}  # the twenty oldest closed_at values
    assert set(result.removed) == {2000 + i for i in range(20)}
    assert result.parent_issue == PARENT_1112
    assert result.created_issue == NEW_1112


def test_1112_row2_parent_below_the_threshold_gets_no_detach_and_one_attach(tmp_path) -> None:
    children = _children_of(open_count=60, closed_count=25)  # 85 children
    gh = _gh_for_parent_1112(children)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    assert _indexes(gh, _detach_args_1112()) == []
    assert len(_indexes(gh, _attach_args_1112())) == 1
    assert result.removed == []
    assert result.parent_issue == PARENT_1112


def test_1112_row3_parent_full_of_open_children_leaves_the_issue_unattached(tmp_path) -> None:
    children = _children_of(open_count=100, closed_count=0)
    gh = _gh_for_parent_1112(children)

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    assert result.created_issue == NEW_1112
    assert result.parent_issue is None
    assert result.removed == []
    assert f"not attached: parent #{PARENT_1112} holds 100 open sub-issues" in result.message
    assert _indexes(gh, _attach_args_1112()) == []
    assert _indexes(gh, _detach_args_1112()) == []


def test_1112_boundary_89_children_gets_no_trim_and_one_attach(tmp_path) -> None:
    gh = _gh_for_parent_1112(_children_of(open_count=89, closed_count=0))

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    assert _indexes(gh, _detach_args_1112()) == []
    assert len(_indexes(gh, _attach_args_1112())) == 1
    assert result.parent_issue == PARENT_1112


def test_1112_boundary_90_children_trims_to_80_then_attaches(tmp_path) -> None:
    gh = _gh_for_parent_1112(_children_of(open_count=80, closed_count=10))

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    detach_at = _indexes(gh, _detach_args_1112())
    attach_at = _indexes(gh, _attach_args_1112())
    assert _detached_ids(gh) == {2000 + i for i in range(10)}
    assert len(attach_at) == 1 and max(detach_at) < attach_at[0]
    assert result.parent_issue == PARENT_1112


def test_1112_boundary_99_open_children_still_attaches(tmp_path) -> None:
    gh = _gh_for_parent_1112(_children_of(open_count=99, closed_count=0))

    result = core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))

    assert _indexes(gh, _detach_args_1112()) == []
    assert len(_indexes(gh, _attach_args_1112())) == 1
    assert result.parent_issue == PARENT_1112


def test_1112_make_room_runs_alone_on_a_parent_number(tmp_path) -> None:
    children = list(reversed(_children_of(open_count=5, closed_count=95)))
    gh = _gh_for_parent_1112(children)

    room = core_issue.make_room(HOME, PARENT_1112, gh_runner=gh)

    assert set(room.removed) == {2000 + i for i in range(20)}
    assert room.remaining == 80
    assert _detached_ids(gh) == {2000 + i for i in range(20)}
    assert _indexes(gh, _attach_args_1112()) == []


# -- --session defaulting (#286) ------------------------------------------------------------------
#
# `run_issue_command` fills `session` itself when `--session` is not given: the first non-empty
# of `SESSION_ID_ENV_VARS`, `"none"` when none is set, and a given `--session` always wins.


def _clear_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SESSION_ID_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _parse_issue_args(argv: list[str]) -> argparse.Namespace:
    """Parses `argv` through the real CLI parser (`core_check.add_subparser`'s subtree), so the
    no-argument case is proven at the CLI, not only in `_resolve_session`."""
    ap = argparse.ArgumentParser(prog="core_check")
    sub = ap.add_subparsers(dest="command", required=True)
    core_check.add_subparser(sub)
    return ap.parse_args(argv)


def _issue_argv(**overrides) -> list[str]:
    base = dict(
        role="worker", kind="broke",
        what="tools/agentops/dispatch.py: the dispatcher log format changed",
        error="tools/agentops/dispatch.py raised KeyError on 'sessions'",
        log="docs/agents/orch.md: the checkpoint step never ran",
        did="worked around", **{"caught-by": "suite"},
    )
    base.update(overrides)
    argv = ["core", "issue"]
    for key, value in base.items():
        argv += [f"--{key}", value]
    return argv


def _table_for_full_issue_flow(number: int, parent: int, sub_id: int) -> tuple[dict, list]:
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": parent, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/{parent}/sub_issues?per_page=100", "--paginate", "--slurp"):
            json.dumps([{"id": sub_id, "number": number, "state": "open", "closed_at": None}]),
    }

    def is_issue_create(args):
        return args[:2] == ["issue", "create"]

    def is_get_id(args):
        return args[:2] == ["api", f"repos/{HOME}/issues/{number}"]

    def is_attach(args):
        return args[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/{parent}/sub_issues"]

    handlers = [
        (is_issue_create, lambda args: _result(stdout=_issue_url(number))),
        (is_get_id, lambda args: _result(stdout=f"{sub_id}\n")),
        (is_attach, lambda args: _result(stdout="")),
    ]
    return table, handlers


def _session_field(body: str) -> str:
    match = re.search(r"\*\*session:\*\* (.*)", body)
    assert match, body
    return match.group(1)


def _run_via_cli(monkeypatch, argv, gh, *, cwd, manifest_path) -> None:
    """Runs one `core issue` call through the real argparse path and `run_issue_command`, with
    `gh_runner` fixed to `gh` and settings/cwd/manifest fixed, so no real tracker is touched
    however `session` resolves."""
    real_write_core_issue = core_issue.write_core_issue

    def fixed_write_core_issue(*, session, **kwargs):
        kwargs["settings"] = SETTINGS
        kwargs["cwd"] = cwd
        kwargs["manifest_path"] = manifest_path
        return real_write_core_issue(gh_runner=gh, session=session, **kwargs)

    monkeypatch.setattr(core_issue, "write_core_issue", fixed_write_core_issue)
    args = _parse_issue_args(argv)
    rc = core_issue.run_issue_command(args)
    assert rc == 0


# row 2: the variable set, no argument -> the body's `session` = the id
def test_session_env_var_used_when_no_argument(tmp_path, monkeypatch) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sess-42")

    real_subprocess_run = subprocess.run

    def guarded_run(*args, **kwargs):
        raise AssertionError(f"a real subprocess was spawned: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", guarded_run)

    table, handlers = _table_for_full_issue_flow(number=901, parent=900, sub_id=91001)
    gh = RecordingGh(table, handlers=handlers)

    _run_via_cli(monkeypatch, _issue_argv(), gh,
                cwd=tmp_path, manifest_path=_manifest_path(tmp_path))

    monkeypatch.setattr(subprocess, "run", real_subprocess_run)

    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert _session_field(body) == "env-sess-42"


# row 3: both variables absent, no argument -> `none`
def test_session_none_when_env_absent(tmp_path, monkeypatch) -> None:
    _clear_session_env(monkeypatch)

    table, handlers = _table_for_full_issue_flow(number=902, parent=900, sub_id=91002)
    gh = RecordingGh(table, handlers=handlers)

    _run_via_cli(monkeypatch, _issue_argv(), gh,
                cwd=tmp_path, manifest_path=_manifest_path(tmp_path))

    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert _session_field(body) == "none"


# row 4: variable set AND the argument given -> the argument
def test_session_argument_wins_over_env(tmp_path, monkeypatch) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sess-should-not-be-used")

    table, handlers = _table_for_full_issue_flow(number=903, parent=900, sub_id=91003)
    gh = RecordingGh(table, handlers=handlers)

    _run_via_cli(monkeypatch, _issue_argv(session="given-sess"), gh,
                cwd=tmp_path, manifest_path=_manifest_path(tmp_path))

    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert _session_field(body) == "given-sess"


# row 5: every test's gh calls go only to the fake -- proven above by guarding subprocess.run in
# row 2, and here by asserting the fake alone ever gets a call across a full CLI-parsed run.
def test_gh_calls_go_only_to_the_fake(tmp_path, monkeypatch) -> None:
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess-fallback")

    real_subprocess_run = subprocess.run
    spawned: list = []

    def guarded_run(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError(f"a real subprocess was spawned: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", guarded_run)

    table, handlers = _table_for_full_issue_flow(number=904, parent=900, sub_id=91004)
    gh = RecordingGh(table, handlers=handlers)

    _run_via_cli(monkeypatch, _issue_argv(), gh,
                cwd=tmp_path, manifest_path=_manifest_path(tmp_path))

    monkeypatch.setattr(subprocess, "run", real_subprocess_run)

    assert spawned == []
    create_calls = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    body = create_calls[0][create_calls[0].index("--body") + 1]
    assert _session_field(body) == "env-sess-fallback"
    assert gh.calls  # the fake is the only thing that ever recorded a call


# -- row 6 (core half): the area label of the code's own issues ---------------------------------

def _area_gh(open_parents):
    """Stateful fake for the code's own repo (repo == home): creates get numbers 700, 701."""
    created = iter([700, 701])

    def gh(args):
        gh.calls.append(list(args))
        if args[:2] == ["repo", "view"]:
            return _result(stdout=HOME + "\n")
        if args[:2] == ["issue", "list"] and "core-epic" in args:
            return _result(stdout=json.dumps([{"number": n, "title": "p"} for n in open_parents]))
        if args[:2] == ["issue", "list"]:
            return _result(stdout="[]")
        if args[:2] == ["issue", "create"]:
            return _result(stdout=_issue_url(next(created)))
        if args[0] == "api" and args[1].startswith(f"repos/{HOME}/issues/7") and "sub_issues" not in args[1]:
            return _result(stdout="90001\n")
        if args[:2] == ["api", "-X"]:
            return _result(stdout="")
        if args[0] == "api" and "sub_issues" in args[1]:
            return _result(stdout="[]")
        if args[:2] == ["label", "create"]:
            return _result(stdout="")
        raise AssertionError(f"unexpected gh call: {args}")

    gh.calls = []
    return gh


def _area_run(tmp_path, open_parents, area):
    gh = _area_gh(open_parents)
    kwargs = _base_kwargs(tmp_path)
    kwargs["settings"] = {"repo": HOME, "home": HOME, "update_rule": "home", "code_issue_area": area}
    core_issue.write_core_issue(gh_runner=gh, git_runner=lambda a: _result(stdout="deadbeef\n"), **kwargs)
    creates = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    counts = [sum(1 for i, a in enumerate(c) if a == "--label" and c[i + 1].startswith("area:")) for c in creates]
    label_creates = [c for c in gh.calls if c[:2] == ["label", "create"]]
    assert not any(x.startswith("area:") for c in label_creates for x in c)
    return creates, counts


def test_area_label_no_parent_open_two_issues_each_one_area(tmp_path) -> None:
    creates, counts = _area_run(tmp_path, [], "area:orchestration")
    assert len(creates) == 2 and counts == [1, 1]
    assert all("area:orchestration" in c for c in creates)


def test_area_label_one_parent_open_one_issue(tmp_path) -> None:
    creates, counts = _area_run(tmp_path, [600], "area:orchestration")
    assert len(creates) == 1 and counts == [1]


def test_area_label_empty_setting_no_area(tmp_path) -> None:
    creates, counts = _area_run(tmp_path, [], "")
    assert len(creates) == 2 and counts == [0, 0]


def test_area_label_not_added_when_home_differs() -> None:
    settings = dict(SETTINGS, code_issue_area="area:orchestration")
    assert core_issue._area_for(settings, HOME) == ""


# -- caused by / caught by (#355) ------------------------------------------------------------------

def _create_gh() -> RecordingGh:
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): json.dumps([{"number": 600, "title": "Core epic: core issues"}]),
        ("api", f"repos/{HOME}/issues/600/sub_issues?per_page=100", "--paginate", "--slurp"): "[]",
    }
    handlers = [
        (lambda a: a[:2] == ["issue", "create"], lambda a: _result(stdout=_issue_url(501))),
        (lambda a: a[:2] == ["api", f"repos/{HOME}/issues/501"], lambda a: _result(stdout="90001\n")),
        (lambda a: a[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/600/sub_issues"],
         lambda a: _result(stdout="")),
    ]
    return RecordingGh(table, handlers=handlers)


def _created_body(gh: RecordingGh) -> str:
    call = [c for c in gh.calls if c[:2] == ["issue", "create"]][0]
    return call[call.index("--body") + 1]


def test_broke_without_caught_by_is_refused_naming_the_field_and_no_gh_call(tmp_path, capsys) -> None:
    gh = _create_gh()
    kwargs = _base_kwargs(tmp_path)
    kwargs["caught_by"] = None
    with pytest.raises(core_issue.CoreIssueError, match="caught by"):
        core_issue.write_core_issue(gh_runner=gh, **kwargs)
    assert gh.calls == []


def test_broke_without_caught_by_exits_2_through_the_cli(tmp_path, capsys) -> None:
    args = _parse_issue_args(["core", "issue", "--role", "worker", "--kind", "broke", "--what", "x.md", "--error", "e",
                              "--log", "l", "--did", "parked"])
    assert core_issue.run_issue_command(args) == 2
    assert "caught by" in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [("caught_by", "nobody"), ("caused_by", "237"), ("caused_by", "#x")])
def test_invalid_caught_by_or_caused_by_is_refused(tmp_path, field, value) -> None:
    gh = _create_gh()
    kwargs = {**_base_kwargs(tmp_path), field: value}
    with pytest.raises(core_issue.CoreIssueError):
        core_issue.write_core_issue(gh_runner=gh, **kwargs)
    assert gh.calls == []


def test_broke_with_caught_by_and_caused_by_has_eleven_fields(tmp_path) -> None:
    gh = _create_gh()
    core_issue.write_core_issue(gh_runner=gh, **{**_base_kwargs(tmp_path), "caused_by": "#237"})
    body = _created_body(gh)
    assert len(re.findall(r"\*\*[a-z_ ]+:\*\*", body)) == 11
    assert "**caused by:** #237" in body
    assert "**caught by:** suite" in body


def test_looks_off_with_none_gives_both_unknown(tmp_path) -> None:
    gh = _create_gh()
    kwargs = {**_base_kwargs(tmp_path), "kind": "looks off", "caught_by": None}
    core_issue.write_core_issue(gh_runner=gh, **kwargs)
    body = _created_body(gh)
    assert "**caused by:** unknown" in body
    assert "**caught by:** unknown" in body


def test_change_keeps_nine_fields(tmp_path) -> None:
    gh = _create_gh()
    core_issue.write_core_issue(gh_runner=gh, **{**_base_kwargs(tmp_path), "kind": "change"})
    body = _created_body(gh)
    assert len(re.findall(r"\*\*[a-z_ ]+:\*\*", body)) == 9
    assert "caught by" not in body


# -- every issue the generator opens passes the ticket shape check -------------------------------

def _creates(gh):
    out = []
    for c in gh.calls:
        if c[:2] == ["issue", "create"]:
            out.append((c[c.index("--title") + 1], c[c.index("--body") + 1]))
    return out


def _shape_table():
    table = {
        ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"): REPO + "\n",
        ("issue", "list", "-R", HOME, "--label", "core-issue", "--state", "open",
         "--json", "number,title,body"): "[]",
        ("label", "create", "core-issue", "-R", HOME, "--color", core_issue.CORE_ISSUE_LABEL_META["color"],
         "--description", core_issue.CORE_ISSUE_LABEL_META["description"], "--force"): "",
        ("issue", "list", "-R", HOME, "--label", "core-epic", "--state", "open",
         "--json", "number,title"): "[]",
        ("label", "create", "core-epic", "-R", HOME, "--color", core_issue.CORE_EPIC_LABEL_META["color"],
         "--description", core_issue.CORE_EPIC_LABEL_META["description"], "--force"): "",
        ("api", f"repos/{HOME}/issues/700/sub_issues?per_page=100", "--paginate", "--slurp"): "[]",
    }
    handlers = [
        (lambda a: a[:2] == ["issue", "create"] and "core-epic" in a, lambda a: _result(stdout=_issue_url(700))),
        (lambda a: a[:2] == ["issue", "create"], lambda a: _result(stdout=_issue_url(502))),
        (lambda a: a[:2] == ["api", f"repos/{HOME}/issues/502"], lambda a: _result(stdout="90002\n")),
        (lambda a: a[:4] == ["api", "-X", "POST", f"repos/{HOME}/issues/700/sub_issues"], lambda a: _result()),
    ]
    return RecordingGh(table, handlers=handlers)


def test_parent_and_finding_issue_pass_the_ticket_shape_check(tmp_path) -> None:
    from tools.agentops.ticket_shape import check

    gh = _shape_table()
    core_issue.write_core_issue(gh_runner=gh, **_base_kwargs(tmp_path))
    created = _creates(gh)
    assert len(created) == 2  # the finding, then the parent
    for title, body in created:
        assert check(title, body) == []
    finding_title, finding_body = created[0]
    assert finding_title.startswith(f"{REPO}: broke: ")
    assert "**what failed:**" in finding_body and "dispatch.py" in finding_body.split("## For the agent")[1]


def test_long_what_title_is_cut_to_80_and_keeps_the_kind(tmp_path) -> None:
    from tools.agentops.rework import title_kinds
    from tools.agentops.ticket_shape import check

    gh = _shape_table()
    what = "tools/agentops/dispatch.py: " + "the dispatcher log format changed again and again " * 4 + "#123"
    kwargs = {**_base_kwargs(tmp_path), "what": what}
    core_issue.write_core_issue(gh_runner=gh, **kwargs)
    title, body = _creates(gh)[0]
    assert len(title) <= 80 and "#" not in title
    assert title_kinds(title) == ["broke"]
    assert check(title, body) == []
