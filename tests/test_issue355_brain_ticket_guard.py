"""#355: past the context line the brain cannot open, reopen, rewrite or close a GitHub issue.

Seam: ``tools/hooks/brain_ticket_guard.run(payload)`` -- the PreToolUse payload Claude Code hands the hook; the answer is
the deny JSON or None. The context size comes from the transcript's last assistant usage, the line from the settings
(``AGENTOPS_HOOK_SETTINGS``), the log from ``AGENTOPS_HOOK_LOG``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from tools.hooks import brain_ticket_guard  # noqa: E402

TITLE = _load_settings()["brain_chat_title"]
OVER, UNDER = 2000, 500  # the test settings' context line is 1000

REFUSED = [
    "gh issue create --title T --body B",
    "gh issue create -t T -F body.md",
    "gh issue close 5",
    "gh issue close 5 --comment done",
    "gh  issue   close   5",
    "gh issue -R o/r close 5",
    "gh -R o/r issue close 5",
    "gh issue reopen 5",
    "gh issue edit 5 --title New",
    "gh issue edit 5 -t New",
    "gh issue edit 5 --title=New",
    "gh issue edit 5 --body New",
    "gh issue edit 5 -b New",
    "gh issue edit 5 --body-file b.md",
    "gh issue edit 5 -F b.md",
    "gh issue edit 5 --add-label x --body B",
    "cd /x && gh issue close 5",
    "cd /x; gh issue create -t T",
    "echo hi && gh issue close 5 && echo done",
    "echo hi\ngh issue reopen 5\necho done",
    "# purpose: close the ticket\ngh issue close 5",
    "gh.exe issue close 5",
    "gh api -X POST repos/o/r/issues -f title=T",
    "gh api repos/o/r/issues -f title=T -f body=B",
    "gh api --method POST repos/o/r/issues",
    "gh api repos/o/r/issues --input body.json",
    "gh api -X PATCH repos/o/r/issues/5 -f state=closed",
    "cd /x && gh api -X PATCH repos/o/r/issues/5 -f state=closed",
    "gh api --method PATCH repos/o/r/issues/5 -f title=T",
    "gh api repos/o/r/issues/5 -X PATCH -f body=B",
    "gh api -X PATCH repos/o/r/issues/5 -F body=@b.md",
    "gh api -X PATCH repos/o/r/issues/5 --raw-field state=closed",
    "gh api -X PATCH /repos/o/r/issues/5 -f state=open",
    # a gh call at any command position (wrappers, loops, groups, prefixes, substitutions)
    'bash -c "gh issue close 5"',
    "sh -c 'gh issue close 5'",
    'pwsh -Command "gh issue close 5"',
    "powershell -Command gh issue close 5",
    "cmd /c gh issue close 5",
    'cmd /c "gh issue close 5"',
    'bash -c "sh -c \'gh issue close 5\'"',
    'bash -c "cd /x && gh api -X PATCH repos/o/r/issues/5 -f state=closed"',
    "eval 'gh issue close 5'",
    "for n in 1 2; do gh issue close $n; done",
    "while true; do gh issue close 5; done",
    "if true; then gh issue close 5; else gh issue reopen 6; fi",
    "foreach ($n in 1..3) { gh issue close $n }",
    "1,2 | % { gh issue close $_ }",
    "1,2 | %{ gh issue close $_ }",
    "1,2 | ForEach-Object { gh issue close $_ }",
    "(gh issue close 5)",
    "{ gh issue close 5; }",
    "x=$(gh issue close 5)",
    'echo "$(gh issue close 5)"',
    "echo `gh issue close 5`",
    "timeout 9 gh issue close 5",
    "env GH_TOKEN=x gh issue close 5",
    "GH_TOKEN=x gh issue close 5",
    "sudo gh issue close 5",
    "nohup gh issue close 5",
    "time gh issue close 5",
    "echo 5 | xargs gh issue close",
    "echo 5 | xargs -n 1 gh issue close",
    "echo 5 | xargs -I{} gh issue close {}",
    '"gh" issue close 5',
    '& "C:\\Program Files\\GitHub CLI\\gh.exe" issue close 5',
    # the rest of the class
    "gh issue delete 5",
    "gh issue delete 5 --yes",
    "gh issue transfer 5 o/other",
    "gh api -X PATCH repos/o/r/issues/5 --input=body.json",
    "gh api repos/o/r/issues --input=body.json",
    'gh api -X PATCH "repos/o/r/issues/5?x=1" -f state=closed',
    'gh api -X POST "repos/o/r/issues?x=1" -f title=T',
]

PASSED = [
    'gh issue comment 5 --body "x"',
    "gh issue comment 5 -F note.md",
    "gh issue view 5",
    "gh issue list --label ready-for-agent",
    "gh issue edit 5 --add-label parked",
    "gh issue edit 5 --remove-label parked --add-label ready-for-agent",
    "gh api repos/o/r/issues/5/comments -f body=x",
    "gh api -X POST repos/o/r/issues/5/comments -f body=x",
    "gh api repos/o/r/issues/5",
    "gh api repos/o/r/issues",
    "gh api -X PATCH repos/o/r/issues/5 -f assignee=me",
    "gh api repos/o/r/issues/comments/9 -X PATCH -f body=x",
    "gh pr create --title T",
    "git status",
    'echo "gh issue close 5"',
    "cat <<'EOF'\ngh issue close 5\nEOF",
    'gh issue comment 5 --body "run gh issue close 5 later"',
    "gh issue comment 5 --body 'it said \"gh issue close 5\"'",
    "gh issue comment 5 --body-file - <<'EOF'\nrun gh issue close 5 later\nEOF",
    'git commit -m "gh issue close 5 later"',
    'echo "bash -c \'gh issue close 5\'"',
    "echo 5 | xargs echo gh issue close",
    'bash -c "gh issue view 5"',
    "for n in 1 2; do gh issue view $n; done",
    "1,2 | % { gh issue comment $_ -b x }",
    "gh api graphql -f query='query { repository(owner:\"o\",name:\"r\") { issue(number:5) { title } } }'",
    "gh api repos/o/r/issues/5?x=1",
    'gh api "repos/o/r/issues/5/comments?per_page=2" -f body=x',
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    settings = {"repo": "o/n", "brain_chat_title": TITLE, "context_line": 1000, "context_ceiling": 5000,
                "claude_projects_dir": "x", "bridge_dir": str(tmp_path / "b"), "bridge_lock": "var/locks/bridge.lock",
                "local_modules": []}
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_HOOK_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setenv("AGENTOPS_HOOK_LOG", str(tmp_path / "hook.log"))


def _transcript(tmp_path: Path, kind: str, size: int) -> Path:
    first = {"type": "user", "message": {"content": "hi"}}
    if kind == "bg":
        first["sessionKind"] = "bg"
    records = [first]
    if kind in ("brain", "bg"):
        records.append({"type": "custom-title", "customTitle": TITLE, "sessionId": "s1"})
    records.append({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": size}}})
    path = tmp_path / f"{kind}-{size}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _payload(tmp_path: Path, command: str, *, kind="brain", size=OVER, tool="Bash", **extra) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command},
               "transcript_path": str(_transcript(tmp_path, kind, size)), "session_id": "s-1"}
    payload.update(extra)
    return payload


@pytest.mark.parametrize("command", REFUSED)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_brain_over_the_line_is_refused(tmp_path, command, tool):
    out = brain_ticket_guard.run(_payload(tmp_path, command, tool=tool))
    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    reason = spec["permissionDecisionReason"]
    assert "Context is 2k, over the context line (1k)." in reason
    assert "opens, rewrites and closes no ticket" in reason
    assert "docs/agents/brain-reference.md, The clear and the hand-off" in reason


@pytest.mark.parametrize("command", REFUSED)
def test_brain_under_the_line_passes(tmp_path, command):
    assert brain_ticket_guard.run(_payload(tmp_path, command, size=UNDER)) is None


@pytest.mark.parametrize("command", PASSED)
def test_comments_reads_and_labels_pass_over_the_line(tmp_path, command):
    assert brain_ticket_guard.run(_payload(tmp_path, command)) is None


@pytest.mark.parametrize("command", ["gh issue close 5",
                                     "cd /x && gh api -X PATCH repos/o/r/issues/5 -f state=closed"])
def test_subagent_and_background_session_pass_over_the_line(tmp_path, command):
    assert brain_ticket_guard.run(_payload(tmp_path, command, agent_type="worker")) is None
    assert brain_ticket_guard.run(_payload(tmp_path, command, kind="bg")) is None


def test_a_session_with_no_brain_title_passes(tmp_path):
    assert brain_ticket_guard.run(_payload(tmp_path, "gh issue close 5", kind="cloud")) is None


def test_other_tools_and_missing_usage_pass(tmp_path):
    assert brain_ticket_guard.run(_payload(tmp_path, "gh issue close 5", tool="Read")) is None
    payload = _payload(tmp_path, "gh issue close 5")
    Path(payload["transcript_path"]).write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")
    assert brain_ticket_guard.run(payload) is None


def test_every_refusal_is_logged_with_action_refuse(tmp_path):
    brain_ticket_guard.run(_payload(tmp_path, "gh issue close 5"))
    brain_ticket_guard.run(_payload(tmp_path, "gh issue close 6", size=UNDER))
    rows = [json.loads(line) for line in (tmp_path / "hook.log").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["action"] == "refuse" and rows[0]["event"] == "PreToolUse"
    assert rows[0]["context_k"] == 2 and rows[0]["session"] == "s-1"


def test_main_prints_the_deny_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload(tmp_path, "gh issue reopen 5"))))
    assert brain_ticket_guard.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert brain_ticket_guard.main() == 0


@pytest.mark.parametrize("mutation", ["createIssue", "updateIssue", "closeIssue", "reopenIssue", "deleteIssue",
                                      "transferIssue"])
def test_graphql_issue_mutations_are_refused(tmp_path, mutation):
    command = f"gh api graphql -f query='mutation {{ {mutation}(input: {{id: \"X\"}}) {{ clientMutationId }} }}'"
    assert brain_ticket_guard.run(_payload(tmp_path, command)) is not None
    assert brain_ticket_guard.run(_payload(tmp_path, command, size=UNDER)) is None


def test_line_nudge_text_names_the_new_rule():
    from tools.hooks.brain_reset_nudge import _nudge_text
    kind, text = _nudge_text(2000, 1000, 5000, {"subagent": 0, "shell": 0, "monitor": 0})
    assert kind == "line"
    assert ("(answer the owner in full and write his words as a comment on their ticket; open, rewrite and close no "
            "ticket, the next chat does that)") in text
    assert "anything longer goes into a ticket" not in text
