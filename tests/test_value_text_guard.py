"""#415 unit 3: the value check runs on every text for another reader (tools/hooks/value_text_guard.py).

Seams: `value_text_guard.run(payload)` (the catch-all PreToolUse hook: every tool but a shell), `tracker_guard.run(payload)`
(the shell: the gh writes, whose value refusal joins the ticket-shape refusal in the same call), the hook as a
subprocess (`main`'s exit codes), and the two wiring files. The check itself is orch-it's `guards check-text`; the
tests stand a stub script in its place (env `VALUE_TEXT_GUARD_CHECK`) that flags `[[<kind>]]` in the text and logs every
call, so a test also counts the calls. One real call runs when the orch-it clone has the subcommand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "value_text_guard.py"
SETTINGS = ROOT / ".claude" / "settings.json"
TEMPLATE = ROOT / "config" / "core_templates" / "settings.hooks.json"

from tools.hooks.value_text_guard import DEFAULT_RULE as RULE  # noqa: E402 - one definition, the module's
from tools.hooks.value_text_guard import PATTERN_KINDS  # noqa: E402

# the eleven kinds orch-it's check knows: 8 value kinds and 3 pattern kinds
VALUE_KINDS = ["account", "repo", "host", "home", "user", "projects_folder", "git_name", "git_email"]
ALL_KINDS = VALUE_KINDS + list(PATTERN_KINDS)

STUB = '''
import json, os, re, sys, time
args = sys.argv[1:]
text = sys.stdin.read()
leaves = "--leaves-machine" in args
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"leaves": leaves, "args": args, "text": text, "cwd": os.getcwd()}) + "\\n")
if "[[unreachable]]" in text:
    sys.stderr.write("the check cannot run\\n")
    sys.exit(2)
if "[[sleep]]" in text:
    time.sleep(30)
kinds = []
for k in re.findall(r"\\[\\[(\\w+)\\]\\]", text):
    if k == "path":  # a run-time location: a value that differs per machine, passed only in text that stays
        if leaves:
            kinds.append("projects_folder")
    else:
        kinds.append(k)
findings = [{"kind": k, "rule": "%s"} for k in dict.fromkeys(kinds)]
print(json.dumps({"ok": not findings, "findings": findings, "not_checked": []}))
sys.exit(1 if findings else 0)
''' % RULE

BRAIN = {"agent_type": "brain"}
ORCH = {"agent_type": "orch"}


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "stub_check.py"
    script.write_text(STUB, encoding="utf-8")
    log = tmp_path / "stub_calls.jsonl"
    monkeypatch.setenv("VALUE_TEXT_GUARD_CHECK", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("TRACKER_GUARD_LOG", str(tmp_path / "tracker.jsonl"))

    class Stub:
        path = tmp_path

        @staticmethod
        def calls():
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    return Stub


def _guard(payload):
    from tools.hooks import value_text_guard
    return value_text_guard.run(payload)


def _reason(output):
    assert output is not None, "the call passed"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    return output["hookSpecificOutput"]["permissionDecisionReason"]


def _shell(command, session, cwd):
    from tools.hooks import tracker_guard
    output = tracker_guard.run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                "tool_input": {"command": command}, "cwd": str(cwd), **session})
    return None if output is None else _reason(output)


def _write(file, content, session=BRAIN, tool="Write", cwd=None):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(cwd or ROOT),
            "tool_input": {"file_path": str(file), "content": content}, **session}


def _body_file(tmp_path, text):
    f = tmp_path / "body.md"
    f.write_text(text, encoding="utf-8")
    return f


# ---- (a) the tracker: a body and a comment, at create and at edit, from a brain and an ORCH session ----

@pytest.mark.parametrize("session", [BRAIN, ORCH], ids=["brain", "orch"])
@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("form", ["create", "edit", "comment"])
def test_tracker_body_and_comment_with_a_kind_are_refused(stub, kind, form, session):
    body = _body_file(stub.path, f"A line with the value [[{kind}]] in it.")
    command = {
        "create": f"gh issue create --title Title --body-file {body.name} --label area:runs",
        "edit": f"gh issue edit 7 --body-file {body.name}",
        "comment": f"gh issue comment 7 --body-file {body.name}",
    }[form]
    reason = _shell(command, session, stub.path)
    assert reason and RULE in reason and kind in reason
    call, = stub.calls()
    assert call["leaves"] is True  # the tracker is read by another machine: no per-machine value passes


@pytest.mark.parametrize("command", [
    'gh issue comment 7 --body "the value [[git_email]] here"',
    'gh issue create --title T --body "the value [[git_email]] here" --label area:runs',
    'gh issue edit 7 --title "[[git_email]]"',
    'gh pr create --title T --body "the value [[git_email]] here"',
    'gh api repos/o/r/issues/7/comments -f body="the value [[git_email]] here"',
    'gh api -X PATCH repos/o/r/issues/7 -F title="[[git_email]]"',
    'gh issue comment 7 --body "$(cat <<\'EOF\'\nthe value [[git_email]] here\nEOF\n)"',
])
def test_tracker_inline_forms_are_read(stub, command):
    reason = _shell(command, BRAIN, stub.path)
    assert reason and RULE in reason and "git_email" in reason


def test_tracker_value_refusal_joins_the_ticket_shape_refusal(stub):
    body = _body_file(stub.path, "no human block, and [[git_email]]")
    reason = _shell(f"gh issue create --title Title --body-file {body.name} --label area:runs", BRAIN, stub.path)
    assert "write-ticket" in reason and RULE in reason


def test_tracker_read_and_other_shell_calls_are_not_judged(stub):
    for command in ["gh issue list", "gh issue view 7", "gh issue comment 7", "ls -la", "echo [[git_email]]",
                    "gh api repos/o/r/issues/7", "gh api -X GET repos/o/r/issues -f q=[[git_email]]"]:
        assert _shell(command, BRAIN, stub.path) is None, command
    assert stub.calls() == []


def test_tracker_clean_body_passes_and_a_non_bound_session_is_not_judged(stub):
    clean = _body_file(stub.path, "a clean comment")
    assert _shell(f"gh issue comment 7 --body-file {clean.name}", BRAIN, stub.path) is None
    assert len(stub.calls()) == 1
    dirty = _body_file(stub.path, "[[git_email]]")
    for session in ({"agent_type": "worker"}, {}):
        assert _shell(f"gh issue comment 7 --body-file {dirty.name}", session, stub.path) is None
    assert len(stub.calls()) == 1


# ---- (b) a prompt or message to another agent stays on the machine ----

@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("tool,field", [("Agent", "prompt"), ("Task", "prompt"), ("SendMessage", "message")])
def test_helper_prompt_and_session_message_with_a_kind_are_refused(stub, kind, tool, field):
    payload = {"tool_name": tool, "cwd": str(ROOT), **BRAIN,
               "tool_input": {"subagent_type": "worker", "to": "someone", field: f"Do it. [[{kind}]] is the value."}}
    reason = _reason(_guard(payload))
    assert RULE in reason and kind in reason
    call, = stub.calls()
    assert call["leaves"] is False and "--stays-on-machine" in call["args"]


def test_a_path_the_harness_gives_the_session_passes_in_a_prompt_but_not_in_a_record(stub):
    prompt = {"tool_name": "SendMessage", "cwd": str(ROOT), **BRAIN,
              "tool_input": {"to": "x", "message": "read [[path]] first"}}
    assert _guard(prompt) is None
    record = _write(ROOT / "docs" / "STATE.md", "read [[path]] first")
    assert "projects_folder" in _reason(_guard(record))


# ---- (c) a file the brain writes into the repo leaves the machine ----

@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("target", ["docs/STATE.md", "docs/grill/2026-10-29-record.md", "docs/adr/0999-x.md", "GLOSSARY.md"])
def test_a_repo_record_with_a_kind_is_refused(stub, kind, target):
    reason = _reason(_guard(_write(ROOT / target, f"Entry. [[{kind}]]")))
    assert RULE in reason and kind in reason
    assert stub.calls()[0]["leaves"] is True


def test_the_session_id_form_passes_in_state_and_a_grill_record(stub):
    text = "Read <scratchpad of 0a1b2c3d-0000-4000-8000-123456789abc>/hand-off.md before anything."
    assert _guard(_write(ROOT / "docs" / "STATE.md", text)) is None
    assert _guard(_write(ROOT / "docs" / "grill" / "r.md", text)) is None
    assert len(stub.calls()) == 2


def test_an_edit_is_judged_by_what_it_writes_never_by_old_string(stub):
    edit = {"tool_name": "Edit", "cwd": str(ROOT), **BRAIN,
            "tool_input": {"file_path": str(ROOT / "docs" / "STATE.md"), "old_string": "[[git_email]]",
                           "new_string": "<scratchpad of 0a1b2c3d-0000-4000-8000-123456789abc>/x"}}
    assert _guard(edit) is None
    edit["tool_input"]["new_string"] = "now [[git_email]]"
    assert "git_email" in _reason(_guard(edit))
    multi = {"tool_name": "MultiEdit", "cwd": str(ROOT), **BRAIN,
             "tool_input": {"file_path": str(ROOT / "docs" / "STATE.md"),
                            "edits": [{"old_string": "a", "new_string": "fine"}, {"old_string": "b", "new_string": "[[home]]"}]}}
    assert "home" in _reason(_guard(multi))


def test_a_source_file_outside_docs_gets_the_value_kinds_only(stub):
    code = ROOT / "tools" / "agentops" / "x.py"
    assert _guard(_write(code, "x = 1  # [[bare_python]] [[one_os_call]] [[check_name_literal]]")) is None
    assert "git_email" in _reason(_guard(_write(code, "x = 1  # [[git_email]] [[bare_python]]")))
    # in docs/ the same suffix keeps every kind
    assert "bare_python" in _reason(_guard(_write(ROOT / "docs" / "x.py", "[[bare_python]]")))
    assert "bare_python" in _reason(_guard(_write(ROOT / "docs" / "x.md", "[[bare_python]]")))


def test_a_file_outside_the_repo_is_not_judged(stub, tmp_path):
    assert _guard(_write(tmp_path / "scratch.md", "[[git_email]]")) is None
    assert stub.calls() == []


def test_a_file_outside_the_repo_still_has_its_other_text_fields_judged(stub, tmp_path):
    payload = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN,
               "tool_input": {"file_path": str(tmp_path / "scratch.md"), "content": "[[account]]",
                              "description": "mail [[git_email]]"}}
    assert "git_email" in _reason(_guard(payload))
    call, = stub.calls()
    assert call["leaves"] is False and "account" not in call["text"]  # the file body is not judged outside the repo


@pytest.mark.parametrize("target", ["var/notes.md", "config/orchestration.json"])
def test_a_path_git_ignores_and_the_settings_file_are_not_judged(stub, target):
    # var/ is git-ignored; config/orchestration.json holds repo and account values as settings, by the standard
    assert _guard(_write(ROOT / target, "[[git_email]] [[repo]]")) is None
    assert stub.calls() == []


def test_a_tracked_file_the_check_may_not_skip_is_still_judged(stub):
    assert "git_email" in _reason(_guard(_write(ROOT / "docs" / "notes.md", "[[git_email]]")))
    assert "git_email" in _reason(_guard(_write(ROOT / "config" / "model_prices.json", "[[git_email]]")))


def test_a_relative_file_is_resolved_against_the_calls_cwd(stub):
    payload = _write("docs/STATE.md", "[[git_email]]", cwd=ROOT)
    assert "git_email" in _reason(_guard(payload))


# ---- the call is picked by its input, not by a tool name ----

def test_an_invented_tool_with_a_string_field_is_judged(stub):
    payload = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN,
               "tool_input": {"note": "my mail is [[git_email]]", "count": 3}}
    assert "git_email" in _reason(_guard(payload))
    nested = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN,
              "tool_input": {"items": [{"deep": {"text": "[[account]]"}}]}}
    assert "account" in _reason(_guard(nested))


def test_a_call_with_no_text_is_not_judged_and_costs_no_subprocess(stub):
    for tool, tool_input in [("Read", {"file_path": str(ROOT / "README.md"), "offset": 3, "limit": 5}),
                             ("Grep", {"pattern": "[[git_email]]", "path": str(ROOT)}),
                             ("Glob", {"pattern": "**/*.py"}),
                             ("MadeUpTool9", {"count": 3, "flag": True, "text": ""}),
                             ("MadeUpTool9", None)]:
        assert _guard({"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT), **BRAIN}) is None
    assert stub.calls() == []


def test_a_session_that_is_neither_brain_nor_orch_is_not_bound(stub):
    for session in ({"agent_type": "worker"}, {"agent_type": "reviewer-spec"}, {}):
        payload = {"tool_name": "SendMessage", "cwd": str(ROOT), **session,
                   "tool_input": {"message": "[[git_email]]"}}
        assert _guard(payload) is None
    assert stub.calls() == []


def test_one_call_is_one_subprocess_however_many_fields_it_has(stub):
    payload = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN,
               "tool_input": {"a": "one", "b": "two", "c": ["three", "four"]}}
    assert _guard(payload) is None
    call, = stub.calls()
    assert all(word in call["text"] for word in ("one", "two", "three", "four"))


# ---- a shell call is judged by the text FLAGS it carries, not by a gh verb ----

@pytest.mark.parametrize("command", [
    'gh issue close 7 --comment "the value [[git_email]] here"',
    'gh pr review 7 --approve --body "the value [[git_email]] here"',
    'gh pr merge 7 --squash --body "the value [[git_email]] here"',
    'gh pr comment 7 -b "the value [[git_email]] here"',
    'git commit -m "the value [[git_email]] here"',
    'git commit -am "the value [[git_email]] here"',
    'git commit --message="the value [[git_email]] here"',
    'python -m tools.agentops commit -m "the value [[git_email]] here" docs/STATE.md',
    'python -m tools.agentops commit docs/STATE.md -m msg --state-entry "2026-10-30 12:00 [[git_email]]"',
    'python -m tools.agentops close 7 --comment "the value [[git_email]] here"',
], ids=lambda c: c.split('"')[0].strip())
def test_a_text_flag_of_gh_git_commit_or_agentops_is_judged_as_leaving_the_machine(stub, command):
    reason = _shell(command, ORCH, stub.path)
    assert reason and RULE in reason and "git_email" in reason
    call, = stub.calls()
    assert call["leaves"] is True
    assert "tools.agentops" not in call["text"]  # python's own `-m` value is not a text


@pytest.mark.parametrize("command", [
    "python -m tools.agentops close 7 --comment-file {f}",
    "gh pr review 7 --approve --body-file {f}",
    "gh pr merge 7 --body-file {f}",
    "git commit -F {f}",
])
def test_a_text_file_flag_is_read_and_judged(stub, command):
    body = _body_file(stub.path, "the value [[git_email]] here")
    reason = _shell(command.format(f=body.name), BRAIN, stub.path)
    assert reason and "git_email" in reason
    assert stub.calls()[0]["leaves"] is True


def test_a_clean_flag_text_passes_with_one_check(stub):
    assert _shell('git commit -m "a clean message"', BRAIN, stub.path) is None
    assert len(stub.calls()) == 1


def test_a_shell_call_without_a_text_flag_costs_no_subprocess(stub):
    for command in ["git commit --amend --no-edit", "git status -sb", "git log -m", "gh issue close 7", "gh pr merge 7 --squash",
                    "python -m tools.agentops close 7 --not-planned", "python -m tools.agentops merge --help",
                    'echo --body "[[git_email]]"', "gh issue view 7 --comments", "gh pr list -B main"]:
        assert _shell(command, BRAIN, stub.path) is None, command
    assert stub.calls() == []


# ---- a text that cannot be read is never a pass ----

@pytest.mark.parametrize("command,kind", [
    ("gh issue comment 7 --body-file no-such.md", "body file"),
    ("gh issue create --title T --body-file no-such.md --label area:runs", "body file"),
    ("python -m tools.agentops close 7 --comment-file no-such.md", "comment file"),
    ("git commit -F no-such.md", "message file"),
    ("gh api repos/o/r/issues/7/comments -F body=@no-such.md", "field file"),
])
def test_a_named_text_file_that_cannot_be_read_refuses_and_names_its_kind(stub, command, kind):
    reason = _shell(command, BRAIN, stub.path)
    assert reason and kind in reason and "cannot be read" in reason
    assert "no-such" not in reason[reason.index(f"The {kind}"):]  # this check names the kind, never the path
    assert stub.calls() == []
    # a session that is not bound is not judged by this check (no file read, no refusal of its own)
    assert "refused, not passed" not in (_shell(command, {"agent_type": "worker"}, stub.path) or "")


def test_a_failing_repo_root_lookup_refuses_instead_of_passing(stub, monkeypatch):
    import tools.agentops.worktree as worktree

    def broken(root):
        raise RuntimeError("no git here")

    monkeypatch.setattr(worktree, "main_checkout_root", broken)
    reason = _reason(_guard(_write(ROOT / "docs" / "STATE.md", "[[git_email]]")))
    assert "cannot run" in reason and "refused" in reason and "RuntimeError" in reason


# ---- the check cannot run: the call is refused and the install step is named ----

def _assert_install_step(reason):
    assert "cannot run" in reason and "refused" in reason
    assert "orch_it_clone" in reason and "install" in reason


def test_check_exit_2_refuses_and_names_the_install_step(stub):
    reason = _reason(_guard(_write(ROOT / "docs" / "STATE.md", "[[unreachable]]")))
    _assert_install_step(reason)
    assert _shell(f"gh issue comment 7 --body-file {_body_file(stub.path, '[[unreachable]]').name}", BRAIN, stub.path)


def test_check_timeout_refuses(stub, monkeypatch):
    from tools.hooks import value_text_guard
    monkeypatch.setattr(value_text_guard, "TIMEOUT_SECONDS", 1)
    _assert_install_step(_reason(_guard(_write(ROOT / "docs" / "STATE.md", "[[sleep]]"))))


def test_clone_absent_refuses_and_names_the_install_step(tmp_path, monkeypatch):
    monkeypatch.delenv("VALUE_TEXT_GUARD_CHECK", raising=False)
    monkeypatch.setenv("VALUE_TEXT_GUARD_CLONE", str(tmp_path / "no-such-clone"))
    _assert_install_step(_reason(_guard(_write(ROOT / "docs" / "STATE.md", "text"))))


def test_subcommand_missing_refuses_and_names_the_install_step(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    package = clone / "src" / "orch_it"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("import sys\nsys.stderr.write('invalid choice: check-text')\nsys.exit(2)\n",
                                         encoding="utf-8")
    monkeypatch.delenv("VALUE_TEXT_GUARD_CHECK", raising=False)
    monkeypatch.setenv("VALUE_TEXT_GUARD_CLONE", str(clone))
    _assert_install_step(_reason(_guard(_write(ROOT / "docs" / "STATE.md", "text"))))


def test_the_call_runs_in_this_repo_with_the_clone_src_on_the_python_path(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    package = clone / "src" / "orch_it"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': False, 'findings': [{'kind': 'home', 'rule': 'cwd=' + os.getcwd()}], 'not_checked': []}))\n"
        "sys.exit(1)\n", encoding="utf-8")
    monkeypatch.delenv("VALUE_TEXT_GUARD_CHECK", raising=False)
    monkeypatch.setenv("VALUE_TEXT_GUARD_CLONE", str(clone))
    reason = _reason(_guard(_write(ROOT / "docs" / "STATE.md", "text")))
    assert "home" in reason  # the stub was found: the clone's src is on PYTHONPATH
    # this layer's own origin is what the repo and account kinds read, never the clone's
    assert "cwd=" + os.path.normcase(str(ROOT.resolve())) in os.path.normcase(reason)
    assert os.path.normcase(str(clone.resolve())) not in os.path.normcase(reason)


def test_a_clean_result_with_unchecked_kinds_passes(tmp_path, monkeypatch):
    script = tmp_path / "ok.py"
    script.write_text("import json,sys\nsys.stdin.read()\n"
                      "print(json.dumps({'ok': True, 'findings': [], 'not_checked': ['git_name']}))\n", encoding="utf-8")
    monkeypatch.setenv("VALUE_TEXT_GUARD_CHECK", json.dumps([sys.executable, str(script)]))
    assert _guard(_write(ROOT / "docs" / "STATE.md", "text")) is None


# ---- the hook as a process: a deny is JSON on stdout with exit 0; a crash is exit 2 ----

def _process(payload, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, str(HOOK)], input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def test_process_deny_is_json_with_exit_0(stub):
    r = _process({"tool_name": "SendMessage", "cwd": str(ROOT), **BRAIN, "tool_input": {"message": "[[git_email]]"}})
    assert r.returncode == 0, r.stderr
    assert RULE in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_process_clean_call_prints_nothing(stub):
    r = _process({"tool_name": "SendMessage", "cwd": str(ROOT), **BRAIN, "tool_input": {"message": "fine"}})
    assert (r.returncode, r.stdout.strip()) == (0, "")


def test_process_bad_stdin_is_not_judged(stub):
    assert _process("not json").returncode == 0


def test_process_internal_failure_refuses_not_passes(stub, monkeypatch):
    # a payload whose judging raises: the process answers a deny that names the install step, never a silent pass
    r = _process({"tool_name": "SendMessage", "cwd": str(ROOT), **BRAIN, "tool_input": {"message": "x"}},
                 {"VALUE_TEXT_GUARD_CHECK": "not json at all"})
    assert r.returncode == 0
    _assert_install_step(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])


# ---- wiring ----

def _value_entries(path):
    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    return [e for e in entries if any("value_text_guard.py" in h["command"] for h in e["hooks"])]


def test_the_hook_list_and_the_shipped_template_hold_the_same_entry():
    home, template = _value_entries(SETTINGS), _value_entries(TEMPLATE)
    assert len(home) == 1 and home == template


def test_the_entry_is_catch_all_and_fails_closed():
    entry, = _value_entries(SETTINGS)
    assert entry["matcher"] == ""
    command, = [h["command"] for h in entry["hooks"]]
    assert command == 'python "$CLAUDE_PROJECT_DIR/tools/hooks/value_text_guard.py" || exit 2'


def test_the_hook_is_a_core_manifest_line():
    lines = (ROOT / "tools" / "agentops" / "core_manifest.txt").read_text(encoding="utf-8").split()
    assert "tools/hooks/value_text_guard.py" in lines


# ---- one real call, when the clone has the subcommand ----

def _real_clone_has_check_text():
    try:
        from tools.hooks import value_text_guard
        clone = value_text_guard.clone_folder()
        src = clone / "src"
        done = subprocess.run([sys.executable, "-m", "orch_it", "guards", "check-text", "--help"], cwd=str(clone),
                              env={**os.environ, "PYTHONPATH": str(src if src.is_dir() else clone)},
                              capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return done.returncode == 0 and "--leaves-machine" in done.stdout


@pytest.mark.skipif(not _real_clone_has_check_text(), reason="the orch-it clone has no `guards check-text` yet")
def test_real_check_refuses_an_invented_git_email_and_passes_clean_text(monkeypatch):
    # git reads these before any config file, so the clone's own user.email cannot win
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "invented.person@example.test")
    monkeypatch.delenv("VALUE_TEXT_GUARD_CHECK", raising=False)
    payload = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN,
               "tool_input": {"note": "mail me at invented.person@example.test"}}
    assert "git_email" in _reason(_guard(payload))
    clean = {"tool_name": "MadeUpTool9", "cwd": str(ROOT), **BRAIN, "tool_input": {"note": "nothing per machine here"}}
    assert _guard(clean) is None


# ---- a link to one GitHub object is not a per-setup value (#415 unit 7, orch-it 94490e0): the real check ----
# The account and the repo are the setting `tickets_repo` (owner/name), the repo's own value, which is this checkout's
# origin; a git run here would be a live read the live-state guard refuses in a test that is not marked `live`.

REAL_CLONE = pytest.mark.skipif(not _real_clone_has_check_text(), reason="the orch-it clone has no `guards check-text` yet")
FORMS = ["issue URL", "pull URL", "commit URL", "short form"]


@pytest.fixture
def real(monkeypatch):
    from tools.agentops import core_settings
    monkeypatch.delenv("VALUE_TEXT_GUARD_CHECK", raising=False)
    monkeypatch.delenv("VALUE_TEXT_GUARD_CLONE", raising=False)
    account, repo = core_settings.tickets_repo().split("/", 1)
    return account, repo


def _forms(account, repo):
    return {
        "issue URL": f"https://github.com/{account}/{repo}/issues/415",
        "pull URL": f"https://github.com/{account}/{repo}/pull/215",
        "commit URL": f"https://github.com/{account}/{repo}/commit/cab366b2d1f7",
        "short form": f"{account}/{repo}#415",
    }


def _checkpoint(reference):
    return f"Checkpoint (ORCH 2a2d7e2e, #415): landed, see {reference}."


def _real_call(text, leaves=True):
    """(exit code, kinds found) of the real check, started as the hook starts it: no stand-in."""
    from tools.hooks import value_text_guard
    command, cwd, env = value_text_guard.check_invocation(leaves)
    done = subprocess.run(command, input=text, capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env,
                          timeout=60)
    answer = json.loads([line for line in done.stdout.splitlines() if line.strip()][-1])
    return done.returncode, [f["kind"] for f in answer["findings"]]


def _named_kinds(reason):
    """The kinds a refusal names, after `Value kinds found, in <where>: `."""
    listed = reason.split("Value kinds found, in ", 1)[1].split(": ", 1)[1].split(". Rewrite", 1)[0]
    return [k.strip() for k in listed.split(",")]


@REAL_CLONE
@pytest.mark.parametrize("form", FORMS)
def test_a_checkpoint_with_one_allowed_link_passes_the_real_check(real, form):
    text = _checkpoint(_forms(*real)[form])
    assert _real_call(text) == (0, [])
    assert _guard(_write(ROOT / "docs" / "STATE.md", text)) is None


@REAL_CLONE
def test_a_checkpoint_with_pull_issue_commit_and_short_links_passes_the_real_check(real):
    forms = _forms(*real)
    text = (f"Checkpoint (ORCH 2a2d7e2e, #415): landed, see {forms['pull URL']} and {forms['issue URL']}; "
            f"commit {forms['commit URL']}, short {forms['short form']}.")
    assert _real_call(text) == (0, [])
    assert _guard(_write(ROOT / "docs" / "STATE.md", text)) is None


@REAL_CLONE
def test_a_tracker_comment_with_a_linked_checkpoint_passes_the_real_check(real, tmp_path):
    body = _body_file(tmp_path, _checkpoint(_forms(*real)["pull URL"]))
    assert _shell(f"gh issue comment 415 --body-file {body.name}", BRAIN, tmp_path) is None


@REAL_CLONE
@pytest.mark.parametrize("form", FORMS)
def test_the_same_checkpoint_with_a_bare_account_name_in_prose_is_refused(real, form):
    account, repo = real
    text = f"Checkpoint (ORCH 2a2d7e2e, #415): landed, see {_forms(account, repo)[form]}, opened by {account}."
    code, kinds = _real_call(text)
    assert code == 1 and "account" in kinds
    assert "account" in _named_kinds(_reason(_guard(_write(ROOT / "docs" / "STATE.md", text))))


@REAL_CLONE
@pytest.mark.parametrize("link", ["repo root", "clone URL"])
def test_a_repo_root_or_a_clone_url_is_refused_by_the_real_check(real, link):
    account, repo = real
    target = {"repo root": f"https://github.com/{account}/{repo}",
              "clone URL": f"https://github.com/{account}/{repo}.git"}[link]
    text = _checkpoint(target)
    code, kinds = _real_call(text)
    assert code == 1 and "account" in kinds and "repo" in kinds
    assert {"account", "repo"} <= set(_named_kinds(_reason(_guard(_write(ROOT / "docs" / "STATE.md", text)))))
