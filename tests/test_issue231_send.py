"""#231 unit 2 (D119): the test copy's send. Real git in temp dirs; the remote is a local bare repo; the machine file
(``test_copy_patterns.json``) sits in a temp ``AGENTOPS_MACHINE_DIR``. The product name is a made-up one (``Zorblax``)
and the email is spelled in parts, so this file itself never carries a shape the check would hit.

Tested here: row 1 of the issue (a planted product name and an email: refused, both file:line named, the remote has
no ref, the refusal line with the count), row 2 (a clean repo: the remote gets one parentless commit whose tree is
the snapshot, none of the working repo's commits, the working repo untouched), a machine file without ``owner-name``
(refused, nothing pushed), an empty remote (refused), the remote from the repo setting, a second send of the same
branch (overwrites that branch only), and the refusal to overwrite ``main``."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from tools.agentops import test_copy_check as tc
from tools.agentops import test_copy_send as tcs
from tools.agentops.test_copy_cmd import run_test_copy_command
from tools.agentops.test_copy_send import send
from tools.agentops.util import no_window_flags

PRODUCT = "Zorblax"
OWNER = "Ivan" + r"\s+Surname"
EMAIL = "ivan" + "@" + "mailhost" + ".io"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _bare(tmp_path: Path) -> str:
    bare = tmp_path / "copy.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    return str(bare)


def _machine(tmp_path: Path, monkeypatch, patterns: dict) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    (machine / "test_copy_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


CLEAN = {
    "pkg/a.py": "A = 1\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
    "docs/research/x.md": "private notes\n",
}


def test_planted_name_and_email_refused_and_nothing_pushed(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, {
        "tests/test_planted.py": "X = 1\nLABEL = '" + PRODUCT + " lab'\n# contact " + EMAIL + "\n",
        **CLEAN,
    })
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    out = capsys.readouterr().out
    assert not result.sent and result.sha is None
    assert "tests/test_planted.py:2 product" in out
    assert "tests/test_planted.py:3 email" in out
    assert "test copy refused: 2 hits, suite on the PC" in out.splitlines()
    assert PRODUCT not in out and EMAIL not in out
    assert _git(bare, "for-each-ref") == ""


def test_clean_repo_sends_one_parentless_commit_of_the_snapshot(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    head = _git(repo, "rev-parse", "HEAD")
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    out = capsys.readouterr().out
    assert result.sent
    assert result.sha in out
    assert _git(bare, "rev-parse", "refs/heads/m1") == result.sha
    assert _git(bare, "for-each-ref", "--format=%(refname)") == "refs/heads/m1"
    assert _git(bare, "rev-list", "--count", "m1") == "1"
    assert _git(bare, "rev-list", "--parents", "-n", "1", "m1") == result.sha  # no parent
    assert _git(bare, "log", "-1", "--format=%s", "m1") == "test copy m1"
    names = _git(bare, "ls-tree", "-r", "--name-only", "m1").splitlines()
    assert names == tc.snapshot_files(repo) == ["pkg/a.py", "tests/test_a.py"]
    assert head not in _git(bare, "rev-list", "--all").splitlines()
    assert _git(bare, "show", "m1:pkg/a.py") == "A = 1"
    # the working repo: same HEAD, same branches, clean index
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "branch", "--format=%(refname)").count("\n") == 0
    assert _git(repo, "status", "--porcelain") == ""


def test_machine_file_without_owner_name_is_refused(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT})
    repo = _repo(tmp_path, CLEAN)
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    out = capsys.readouterr().out.splitlines()
    assert not result.sent
    assert "test copy refused: 0 hits, suite on the PC" in out
    assert "not checked: owner-name" in out
    assert _git(bare, "for-each-ref") == ""


def test_empty_remote_is_refused(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    result = send(repo, "m1", "")
    assert not result.sent
    assert "test copy: no remote set, suite on the PC" in capsys.readouterr().out.splitlines()


def test_remote_comes_from_the_repo_setting_and_cli_exit_codes(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    bare = _bare(tmp_path)
    repo = _repo(tmp_path, CLEAN)
    # untracked: a tracked setting holding a local path would itself be a user-path hit
    (repo / "config").mkdir()
    (repo / "config" / "orchestration.json").write_text(json.dumps({"test_copy_remote": bare}), encoding="utf-8")
    args = argparse.Namespace(test_copy_command="send", root=repo, branch="m2", remote=None)
    assert run_test_copy_command(args) == 0
    assert _git(bare, "rev-list", "--count", "m2") == "1"
    capsys.readouterr()
    empty = argparse.Namespace(test_copy_command="send", root=repo, branch="m3", remote="")
    assert run_test_copy_command(empty) == 0  # an empty --remote falls back to the setting
    (tmp_path / "other").mkdir()
    unset = _repo(tmp_path / "other", CLEAN)  # no setting, no --remote: refused
    refused = argparse.Namespace(test_copy_command="send", root=unset, branch="m4", remote=None)
    assert run_test_copy_command(refused) == 1


def test_same_branch_again_overwrites_only_that_branch(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    bare = _bare(tmp_path)
    first = send(repo, "m1", bare)
    other = send(repo, "m9", bare)
    (repo / "pkg" / "a.py").write_text("A = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "change")
    second = send(repo, "m1", bare)
    assert first.sent and other.sent and second.sent and first.sha != second.sha
    assert _git(bare, "rev-list", "--count", "m1") == "1"
    assert _git(bare, "show", "m1:pkg/a.py") == "A = 2"
    assert _git(bare, "rev-parse", "m9") == other.sha


def test_main_is_never_overwritten(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    bare = _bare(tmp_path)
    result = send(repo, "main", bare)
    assert not result.sent
    assert "test copy: branch main is never overwritten, suite on the PC" in capsys.readouterr().out.splitlines()
    assert _git(bare, "for-each-ref") == ""


def _refused_with_nothing_pushed(repo, bare, capsys, line):
    result = send(repo, "m1", bare)
    out = capsys.readouterr().out.splitlines()
    assert not result.sent and result.sha is None
    assert line in out
    assert _git(bare, "for-each-ref") == ""
    return out


def test_binary_and_utf16_files_with_an_email_are_refused(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    for name, data in (("blob.bin", bytes([0, 1]) + EMAIL.encode()), ("u16.txt", ("a\n" + EMAIL + "\n").encode("utf-16"))):
        (repo / "tests" / name).write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "planted")
    out = _refused_with_nothing_pushed(repo, _bare(tmp_path), capsys, "test copy refused: 2 hits, suite on the PC")
    assert "tests/blob.bin:1 email" in out and "tests/u16.txt:2 email" in out


def test_a_tracked_path_holding_the_product_name_is_refused(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, {**CLEAN, "tests/test_" + PRODUCT.lower() + ".py": "X = 1\n"})
    out = _refused_with_nothing_pushed(repo, _bare(tmp_path), capsys, "test copy refused: 1 hits, suite on the PC")
    assert "tests/test_zorblax.py:0 product" in out


def test_the_scan_judges_the_copied_bytes_not_the_working_tree(tmp_path, monkeypatch, capsys):
    """The file is clean when the send reads it for the check and planted when it is copied: the scan runs on the copy."""
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    real_bytes = tcs.snapshot_bytes

    def plant_in_copy(root, rel, patterns):
        if rel == "pkg/a.py":
            return ("A = '" + EMAIL + "'" + chr(10)).encode("utf-8")
        return real_bytes(root, rel, patterns)

    monkeypatch.setattr(tcs, "snapshot_bytes", plant_in_copy)
    _refused_with_nothing_pushed(repo, _bare(tmp_path), capsys, "test copy refused: 1 hits, suite on the PC")


def test_no_git_env_and_no_real_author_or_date_reach_the_copy(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    bare = _bare(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "owner" + "@" + "real.test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "owner" + "@" + "real.test")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Real Owner")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Real Owner")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-10-19T12:00:00+0300")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-19T12:00:00+0300")
    stray_index = tmp_path / "stray.index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(stray_index))
    result = send(repo, "m1", bare)
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME",
                 "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name)
    assert result.sent and not stray_index.exists()
    shown = _git(bare, "log", "-1", "--format=%an|%ae|%cn|%ce|%at|%ct", "m1")
    assert shown == "test-copy|test-copy@localhost|test-copy|test-copy@localhost|946684800|946684800"  # 2000-01-01Z


def test_a_tracked_symlink_is_refused_not_followed(tmp_path, monkeypatch, capsys):
    """The symlink is made in the index directly (mode 120000), so no platform support for symlinks is needed."""
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input="pkg/a.py", text=True,
                          capture_output=True, check=True, creationflags=no_window_flags()).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},tests/link")
    _git(repo, "commit", "-q", "-m", "link")
    _refused_with_nothing_pushed(repo, _bare(tmp_path), capsys,
                                 "test copy refused: symlink tests/link, suite on the PC")


def test_line_ends_are_kept_whatever_a_tracked_gitattributes_says(tmp_path, monkeypatch, capsys):
    """A CRLF file under ``* text eol=lf`` reaches the copy byte for byte (the temp repo ignores the attributes)."""
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    (repo / "tests" / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (repo / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8")
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--no-filters", "tests/crlf.txt"],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},tests/crlf.txt")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "crlf")
    bare = _bare(tmp_path)
    assert send(repo, "m1", bare).sent
    assert _git(bare, "rev-parse", "m1:tests/crlf.txt") == blob


def test_content_changed_between_add_and_commit_is_refused(tmp_path, monkeypatch, capsys):
    """The guard itself: a blob that differs from the file's bytes refuses the send."""
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    real_git = tcs._git

    def change_after_add(cwd, *args, **kw):
        out = real_git(cwd, *args, **kw)
        if args[:1] == ("add",):
            (Path(cwd) / "pkg" / "a.py").write_text("A = 2\n", encoding="utf-8")
        return out

    monkeypatch.setattr(tcs, "_git", change_after_add)
    _refused_with_nothing_pushed(repo, _bare(tmp_path), capsys, "test copy refused: content changed on add")


def test_a_timed_out_git_call_refuses(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN)
    real_run = subprocess.run

    def hang_on_push(argv, **kw):
        if "push" in argv:
            assert kw["timeout"] == 300
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return real_run(argv, **kw)

    monkeypatch.setattr(tcs.subprocess, "run", hang_on_push)
    result = send(repo, "m1", _bare(tmp_path))
    assert not result.sent
    assert "test copy: git timed out, suite on the PC" in capsys.readouterr().out.splitlines()
