"""tools/hooks/tracker_guard.py (issue #319, unit 1). The hook runs as a subprocess with a JSON
PreToolUse payload on stdin; no PoB, no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "tracker_guard.py"
AREAS = ["area:orchestration", "area:builds", "area:runs", "area:crafting"]

LABEL_CMDS = [
    "gh label create x",
    "gh label edit x --name y",
    "gh label delete x",
    "gh api -X POST repos/o/r/labels",
    "gh api -X PATCH repos/o/r/labels/x",
    "gh api -X DELETE repos/o/r/labels/x",
]


def _settings(areas):
    data = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    return {**data, "areas": areas}


@pytest.fixture
def env(tmp_path):
    settings = tmp_path / "orch.json"
    settings.write_text(json.dumps(_settings(AREAS)), encoding="utf-8")
    return {"settings": settings, "log": tmp_path / "log.jsonl", "tmp": tmp_path}


def _sessions(tmp_path):
    bg = tmp_path / "bg.jsonl"
    bg.write_text(json.dumps({"sessionKind": "bg"}) + "\n", encoding="utf-8")
    orch = tmp_path / "orch.jsonl"
    orch.write_text(json.dumps({"agentSetting": "orch"}) + "\n", encoding="utf-8")
    return {
        "worker": {"agent_type": "worker"},
        "bg": {"transcript_path": str(bg)},
        "orch": {"transcript_path": str(orch)},
    }


def _run(command, env, session=None, tool="Bash", areas=True, extra_env=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command}, "cwd": str(env["tmp"]), **(session or {})}
    settings = env["settings"]
    if not areas:
        settings = env["tmp"] / "empty.json"
        settings.write_text(json.dumps(_settings([])), encoding="utf-8")
    e = {**os.environ, "TRACKER_GUARD_SETTINGS": str(settings),
         "TRACKER_GUARD_LOG": str(env["log"]), **(extra_env or {})}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                       text=True, timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None


HUMAN = chr(10).join([
    "## For the human",
    "- **What:** in day-1 builds every flask is switched on.",
    "- **Goal:** a flask is active only at 100 % uptime, in every build the owner runs.",
    "- **Plan:** the flag comes from the same calculation PoB shows.",
    "- **Result:** all 5 flasks switch off; shown by a test.",
    "- **Undo:** two-way door (one revert); touches every build that wears flasks.",
    "",
    "## For the agent",
    "Spec line.",
    "Class: none (test fixture)",
    ""])
GOOD_TITLE = "Flasks are active only at 100 % Flask uptime in PoB"


def _good_file(env, name="good.md", body=HUMAN):
    (env["tmp"] / name).write_text(body, encoding="utf-8")
    return name


def _rows(env):
    if not env["log"].exists():
        return []
    return [json.loads(x) for x in env["log"].read_text(encoding="utf-8").splitlines()]


def test_row1_label_guard(env):
    sessions = _sessions(env["tmp"])
    refused = 0
    for s in sessions.values():
        for c in LABEL_CMDS:
            reason = _run(c, env, s)
            assert reason and "brain alone" in reason and "ticket" in reason, (c, reason)
            refused += 1
    assert refused == 18  # 6 commands x 3 sessions (the 5 of the spec + the extra PATCH/DELETE case)
    assert len(_rows(env)) == 18
    assert all(r["kind"] == "label" for r in _rows(env))
    passed = sum(_run(c, env, None) is None for c in LABEL_CMDS)
    assert passed == len(LABEL_CMDS)
    passed = 0
    for s in [None, *sessions.values()]:
        for c in ("gh label list", "gh issue edit 5 --add-label bug"):
            passed += _run(c, env, s) is None
    assert passed == 8
    assert len(_rows(env)) == 18


def test_label_variants(env):
    w = {"agent_type": "worker"}
    for c in ["gh api --method=DELETE /repos/o/r/labels/x", "gh api -XPOST repos/o/r/labels -f name=x",
              "gh api repos/o/r/labels -f name=x", "echo hi && gh label create x",
              "cd x; gh label delete y | cat",
              "gh api graphql -f query='mutation { createLabel(input:{}) { label { id } } }'"]:
        assert _run(c, env, w), c
        assert _run(c, env, w, tool="PowerShell"), c
    for c in ["gh api repos/o/r/labels", "gh api -X GET repos/o/r/labels", "gh api repos/o/r/issues -f a=b",
              "echo gh label create x"]:
        assert _run(c, env, w) is None, c


def test_row5_issue_guard(env):
    base = f'gh issue create --title "{GOOD_TITLE}" --body-file {_good_file(env)}'
    reason = _run(base, env)
    assert reason and all(a in reason for a in AREAS)
    assert _run(base + " --label area:runs", env) is None
    assert _run(base + " --label enhancement,area:runs", env) is None
    assert _run(base + " -l area:runs --label=bug", env) is None
    r1 = _run(base + " --label area:runs --label area:builds", env)
    r2 = _run(base + ' --label "area:runs,area:builds"', env)
    assert r1 and r2
    r3 = _run(base + " --label area:monitor", env)
    assert r3 and "area:runs" in r3
    assert len([1 for c in [base + " --label area:runs --label area:builds",
                            base + ' --label "area:runs,area:builds"'] if _run(c, env)]) == 2
    empty = [base, base + " --label area:runs", base + " --label enhancement,area:runs",
             base + " --label area:runs --label area:builds", base + ' --label "area:runs,area:builds"',
             base + " --label area:monitor"]
    assert all(_run(c, env, areas=False) is None for c in empty)
    assert all(r["kind"] == "issue" for r in _rows(env))


def test_row10_log(env):
    long_cmd = "gh label create " + "x" * 300
    assert _run(long_cmd, env, {"agent_type": "worker"})
    assert _run("gh issue create --title t", env)
    rows = _rows(env)
    assert len(rows) == 2
    assert rows[0]["session"] == "agent worker" and rows[0]["kind"] == "label"
    assert len(rows[0]["command"]) == 200
    assert rows[1]["session"] == "brain" and rows[1]["kind"] == "issue"
    assert all(len(r["time"]) == 19 for r in rows)


def test_log_failure_and_hook_error_do_not_change_decision(env):
    env["log"] = env["tmp"]  # a directory: the write fails
    assert _run("gh label create x", env, {"agent_type": "worker"})
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0 and not r.stdout.strip()


def test_shape_row1_create_without_block_refused(env):
    reason = _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body "plain text"', env)
    assert reason and "write-ticket" in reason
    reason = _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs', env)
    assert reason and "write-ticket" in reason
    both = _run('gh issue create --title t --body "plain"', env)
    assert both and "write-ticket" in both and "area:runs" in both
    assert _rows(env)


def test_shape_row2_flask_example_passes(env):
    f = _good_file(env)
    assert _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {f}', env) is None
    assert _run(f'gh issue create -t "{GOOD_TITLE}" -l area:runs -F {f}', env) is None


def test_area_rule_only_for_own_repo(env):
    f = _good_file(env)
    own = json.loads(env["settings"].read_text(encoding="utf-8"))["repo"]
    base = f'gh issue create -t "{GOOD_TITLE}" -F {f}'
    assert _run(base + " -R Rasslabsya4el/Zorblax-cabinet-dashboard", env) is None
    assert _run(base + " --repo=Rasslabsya4el/Zorblax-cabinet-dashboard", env) is None
    assert _run(base + f" -R {own}", env)
    assert _run(base + f" -R {own.upper()}", env)
    assert _run(base, env)


def test_shape_row3_edit_and_comment(env):
    bad = _good_file(env, "bad.md", "no block here")
    reason = _run(f"gh issue edit 5 --body-file {bad}", env)
    assert reason and "write-ticket" in reason
    assert _run("gh issue edit 5 --add-label x", env) is None
    assert _run('gh issue comment 5 --body "anything"', env) is None
    assert _run(f"gh issue edit 5 --body-file {_good_file(env)}", env) is None


def test_shape_edit_title_stdin_and_unreadable(env):
    reason = _run('gh issue edit 5 --title "#12 x"', env)
    assert reason and "write-ticket" in reason
    assert _run(f'gh issue edit 5 --title "{GOOD_TITLE}"', env) is None
    assert _run("gh issue edit 5 --body-file -", env) is None
    assert any(r["kind"] == "issue_stdin" for r in _rows(env))
    reason = _run("gh issue edit 5 --body-file missing.md", env)
    assert reason and "missing.md" in reason


# -- #612: a body file directly in the shared temp folder is refused -----------------------------

def _temp_env(env):
    root = env["tmp"] / "usertemp"
    session = root / "claude" / "proj" / "sess" / "scratchpad"
    session.mkdir(parents=True)
    body = HUMAN
    (root / "a.md").write_text(body, encoding="utf-8")
    (session / "a.md").write_text(body, encoding="utf-8")
    (env["tmp"] / "a.md").write_text(body, encoding="utf-8")
    return root, session, {"TEMP": str(root), "TMP": str(root), "TMPDIR": str(root)}


def test_612_row1_shared_temp_refused_on_create_edit_comment(env):
    root, _, extra = _temp_env(env)
    shared = root / "a.md"
    cmds = [f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {shared}',
            f"gh issue edit 5 --body-file {shared}",
            f"gh issue comment 5 --body-file {shared}"]
    reasons = [_run(c, env, extra_env=extra) for c in cmds]
    assert all(r is not None and "not the shared temp folder" in r for r in reasons), reasons
    assert len([ln for ln in env["log"].read_text(encoding="utf-8").splitlines() if ln.strip()]) == 3


def test_612_temp_words_before_expansion_refused(env):
    _, _, extra = _temp_env(env)
    for word in ("$TEMP/a.md", "%TEMP%\\a.md", "/tmp/a.md", "$env:TEMP\\a.md", "${TMP}/a.md"):
        reason = _run(f'gh issue comment 5 --body-file "{word}"', env, extra_env=extra)
        assert reason is not None and "shared temp folder" in reason, word


def test_612_row2_session_folder_passes(env):
    _, session, extra = _temp_env(env)
    f = (session / "a.md").as_posix()  # the shape check's own reader needs no backslash escapes
    assert _run(f"gh issue comment 5 --body-file {f}", env, extra_env=extra) is None
    assert _run(f"gh issue edit 5 --body-file {f}", env, extra_env=extra) is None
    assert _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {f}', env,
                extra_env=extra) is None


def test_612_row3_repo_relative_passes(env):
    _, _, extra = _temp_env(env)
    assert _run("gh issue comment 5 --body-file a.md", env, extra_env=extra) is None
    assert _run("gh issue edit 5 --body-file a.md", env, extra_env=extra) is None


def test_body_file_after_cd(env):
    d = env["tmp"] / "work"
    (d / "out").mkdir(parents=True)
    (d / "out" / "117.body.md").write_text(HUMAN, encoding="utf-8")
    base = d.as_posix()
    assert _run(f"cd {base} && gh issue edit 117 --body-file out/117.body.md", env) is None
    reason = _run(f"cd {base} && gh issue edit 117 --body-file $B/117.body.md", env)
    assert reason == "The --body-file $B/117.body.md holds a shell variable: write a literal absolute path."
    reason = _run(f"cd $B && gh issue edit 117 --body-file out/117.body.md", env)
    assert reason and "shell variable" in reason
    reason = _run(f"cd {base} && gh issue edit 117 --body-file out/missing.md", env)
    assert reason and "cannot be read" in reason


# -- #1074: a new title carrying the word "parked" is refused (label `parked` instead) ----------

def test_parked_title_refused_on_create_and_edit(env):
    f = _good_file(env)
    for title in ("X (parked)", "Crafting oracle parked until the prototype", "PARKED: loadout map"):
        reason = _run(f'gh issue create --title "{title}" --label area:runs --body-file {f}', env)
        assert reason and "`parked`" in reason and "Parked: <event>" in reason, (title, reason)
        assert "write-ticket" in reason, (title, reason)
        reason = _run(f'gh issue edit 5 --title "{title}"', env)
        assert reason and "`parked`" in reason and "Parked: <event>" in reason, (title, reason)
    assert len(_rows(env)) == 6


def test_title_without_parked_passes(env):
    f = _good_file(env)
    assert _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {f}', env) is None
    assert _run(f'gh issue edit 5 --title "{GOOD_TITLE}"', env) is None
    assert _run(f'gh issue create --title "Parkour is not a mechanic here" --label area:runs --body-file {f}', env) is None
    pf = _good_file(env, "parked.md", "Parked: waits on the day-1 prototype\n" + HUMAN)
    assert _run(f"gh issue edit 5 --body-file {pf}", env) is None  # the body is not the title


# -- #371: `Why` is the old second field: refused on create, accepted on edit ---------------------

def test_371_why_body_refused_on_create_accepted_on_edit(env):
    old = _good_file(env, "old_why.md", HUMAN.replace("- **Goal:**", "- **Why:**"))
    reason = _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {old}', env)
    assert reason and "Goal" in reason and "Why" in reason, reason
    assert _run(f"gh issue edit 5 --body-file {old}", env) is None
    assert _run(f'gh issue create --title "{GOOD_TITLE}" --label area:runs --body-file {_good_file(env)}',
                env) is None
