"""#285 unit 2 (rule 4, size): a copy file over 50 MiB (52,428,800 B) refuses the send, one line names the file and its
size in MiB, nothing is pushed; a file of exactly 52,428,800 B is sent. Real git in temp dirs; the remote is a local
bare repo; the machine file (``test_copy_patterns.json``) sits in a temp ``AGENTOPS_MACHINE_DIR``, so ``product`` and
``owner-name`` are checked as in #231. The big file is zero bytes: no pattern hits, only the size decides.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops.test_copy_send import send
from tools.agentops.util import no_window_flags

PRODUCT = "Zorblax"
OWNER = "Ivan" + r"\s+Surname"
LIMIT = 52_428_800
CLEAN = {
    "pkg/a.py": "A = 1\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _repo(tmp_path: Path, files: dict, blobs: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for rel, size in blobs.items():
        (repo / rel).write_bytes(b"\0" * size)
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


def test_file_over_50_mib_is_refused_and_nothing_pushed(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN, {"big.bin": LIMIT + 1})
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    out = capsys.readouterr().out.splitlines()
    assert not result.sent and result.sha is None
    assert "test copy refused: big.bin is 52428801 B (50.0 MiB), over the 50 MiB limit, suite on the PC" in out
    assert result.reason == "test copy refused: big.bin is 52428801 B (50.0 MiB), over the 50 MiB limit"
    assert _git(bare, "for-each-ref") == ""


def test_file_of_exactly_50_mib_is_sent(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    repo = _repo(tmp_path, CLEAN, {"big.bin": LIMIT})
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    assert result.sent and result.sha is not None
    assert _git(bare, "rev-parse", "refs/heads/m1") == result.sha
    assert _git(bare, "ls-tree", "-r", "--name-only", "m1").splitlines() == ["big.bin", "pkg/a.py", "tests/test_a.py"]


def test_standin_goes_into_the_sent_copy_and_the_size_is_judged_after_it(tmp_path, monkeypatch):
    """Rules 1 and 4 together through ``send``: a 50 MiB + 1 B file mapped to a small stand-in is sent with the
    stand-in's bytes (the size is judged on them) and the stand-in's own path is not in the copy."""
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    files = {**CLEAN, "standin.bin": "STANDIN\n",
             "config/orchestration.json": json.dumps({"test_copy_standins": {"big.bin": "standin.bin"}})}
    repo = _repo(tmp_path, files, {"big.bin": LIMIT + 1})
    bare = _bare(tmp_path)
    result = send(repo, "m1", bare)
    assert result.sent and _git(bare, "rev-parse", "refs/heads/m1") == result.sha
    names = _git(bare, "ls-tree", "-r", "--name-only", "m1").splitlines()
    assert "big.bin" in names and "standin.bin" not in names
    assert _git(bare, "show", "m1:big.bin") == "STANDIN"
