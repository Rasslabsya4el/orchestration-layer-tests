"""#809: a commit --state-entry / --review-entry that leaves other changes of the file unsaved says so."""
import argparse
import json
import subprocess

from tools.agentops.merge import run_commit, run_commit_command

BASE = "# State\n\nUpdated: 2026-10-01 00:00 old\n\nPadding: old rule\n\nLast review: r0\n"


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          check=True).stdout.strip()


def _world(tmp_path):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    co = tmp_path / "co"
    _git(tmp_path, "clone", "-q", str(bare), str(co))
    _git(co, "config", "user.email", "t@t.invalid")
    _git(co, "config", "user.name", "t")
    (co / "docs").mkdir()
    (co / "docs" / "STATE.md").write_text(BASE, encoding="utf-8")
    _git(co, "add", ".")
    _git(co, "commit", "-q", "-m", "init")
    _git(co, "push", "-q", "origin", "HEAD:main")
    _git(co, "fetch", "-q", "origin")
    _git(co, "branch", "-q", "--set-upstream-to=origin/main")
    return co


def _run(co, tmp_path, capsys, **kw):
    args = argparse.Namespace(paths=["docs/STATE.md"], message="State: entry", cwd=co,
                              lock=tmp_path / "lock", timeout=None, state_entry=None,
                              state_as="w", review_entry=None)
    for k, v in kw.items():
        setattr(args, k, v)
    rc = run_commit_command(args)
    out = capsys.readouterr().out
    return rc, [l for l in out.splitlines() if l.startswith("warning:")]


def test_row1_body_edit_with_entry_warns_once(tmp_path, capsys):
    co = _world(tmp_path)
    p = co / "docs" / "STATE.md"
    p.write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    rc, warns = _run(co, tmp_path, capsys, state_entry="2026-10-11 02:00 mine")
    assert rc == 0
    assert len(warns) == 1 and "docs/STATE.md" in warns[0] and "commit -m" in warns[0]
    committed = _git(co, "show", "HEAD:docs/STATE.md")
    assert "old rule" in committed and "new rule" not in committed and "mine" in committed
    assert "new rule" in p.read_text(encoding="utf-8")


def test_row1_result_json_has_the_field(tmp_path):
    co = _world(tmp_path)
    (co / "docs" / "STATE.md").write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    res = run_commit(["docs/STATE.md"], "m", cwd=co, lock_path=tmp_path / "lock",
                     state_entry="2026-10-11 02:00 mine", state_as="w")
    assert res.ok
    u = json.loads(res.to_json())["unsaved"]
    assert u["path"] == "docs/STATE.md" and u["changed_lines"] == 1 and "commit -m" in u["save_with"]


def test_row2_no_body_edit_no_warning(tmp_path, capsys):
    co = _world(tmp_path)
    rc, warns = _run(co, tmp_path, capsys, state_entry="2026-10-11 02:00 mine")
    assert rc == 0 and warns == []


def test_row3_review_entry_alone_warns(tmp_path, capsys):
    co = _world(tmp_path)
    (co / "docs" / "STATE.md").write_text(BASE.replace("old rule", "new rule"), encoding="utf-8")
    rc, warns = _run(co, tmp_path, capsys, review_entry="2026-10-11 02:00 r1")
    assert rc == 0 and len(warns) == 1
