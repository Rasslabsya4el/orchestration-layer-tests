"""#251: a refused GitHub send carries its reason to the merge's place field and to the brain line.

Real ``test_copy_send.send`` in temp git repos (the scan refuses before any push, so no remote is needed for a hit);
the product name is a made-up one (``Zorblax``) and lives in the machine file, as in ``test_issue231_send``.
The place field is checked through ``merge_github.pc_place_text(merge_github.pc_reason(result))``, the value merge.py
puts into ``run.place``; the brain line through ``github_suite.run_github_suite``'s own ``brain_line``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops import github_suite as gs
from tools.agentops import merge_github
from tools.agentops import test_copy_send as tcs
from tools.agentops.test_copy_send import SendResult, send
from tools.agentops.util import no_window_flags

PRODUCT = "Zorblax"
OWNER = "Ivan" + r"\s+Surname"
COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}


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


def _machine(tmp_path: Path, monkeypatch) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    (machine / "test_copy_patterns.json").write_text(json.dumps({"product": PRODUCT, "owner-name": OWNER}),
                                                     encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


CLEAN = {
    "pkg/a.py": "A = 1\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
}


def _run(deps_send, branch="i251-reason"):
    """run_github_suite with the real record and machine line replaced by no-ops; only the send is injected."""
    deps = gs.Deps(send=deps_send, record=lambda record: None)
    return gs.run_github_suite(root=None, branch=branch, repo_slug="owner/repo", remote=COPY, repo_name="orch",
                               priority=False, settings=SETTINGS, deps=deps)


def test_one_product_hit_names_file_line_and_kind_in_place_and_brain_line(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {"tests/test_planted.py": "X = 1\nLABEL = '" + PRODUCT + " lab'\n", **CLEAN})
    result = _run(lambda root, branch, remote: send(repo, branch, "file:///nonexistent-remote.git"))
    capsys.readouterr()
    assert result.status == "fallback"
    assert result.reason == "refused: 1 hit: tests/test_planted.py:2 product"
    assert result.brain_line == ("orch merge i251-reason: GitHub refused: 1 hit: tests/test_planted.py:2 product, "
                                 "suite on the PC")
    place = merge_github.pc_place_text(merge_github.pc_reason(result))
    assert place == "suite on the PC (GitHub refused: 1 hit: tests/test_planted.py:2 product)"
    assert PRODUCT not in place


def test_no_remote_refusal_names_that_reason_in_place_and_brain_line(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {**CLEAN})
    result = _run(lambda root, branch, remote: send(repo, branch, ""))
    capsys.readouterr()
    assert result.reason == "refused: test copy: no remote set"
    assert result.brain_line == "orch merge i251-reason: GitHub refused: test copy: no remote set, suite on the PC"
    assert merge_github.pc_place_text(merge_github.pc_reason(result)) == \
        "suite on the PC (GitHub refused: test copy: no remote set)"


def test_more_than_five_hits_name_five_then_the_count_of_the_rest(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    lines = "".join(f"L{i} = '{PRODUCT}'\n" for i in range(1, 8))
    repo = _repo(tmp_path, {"tests/test_many.py": lines, **CLEAN})
    refused = send(repo, "i251-many", "file:///nonexistent-remote.git")
    capsys.readouterr()
    assert not refused.sent and refused.sha is None
    assert refused.reason == ("7 hits: tests/test_many.py:1 product, tests/test_many.py:2 product, "
                              "tests/test_many.py:3 product, tests/test_many.py:4 product, "
                              "tests/test_many.py:5 product +2 more")


def test_refusal_without_reason_keeps_the_plain_word(monkeypatch):
    result = _run(lambda root, branch, remote: SendResult(False, None))
    assert result.reason == "refused"
    assert result.brain_line == "orch merge i251-reason: GitHub refused, suite on the PC"


def test_long_reason_keeps_the_brain_line_within_160_chars():
    reason = "refused: 5 hits: " + ", ".join(f"tests/{'d' * 60}/test_{i}.py:{i} product" for i in range(5))
    line = gs.brain_line("orch", "b" * 100, reason)
    assert len(line) <= 160
    assert line.startswith("orch merge ") and line.endswith(", suite on the PC")
    assert "tests/" in line


def test_a_long_reason_gives_way_before_a_short_ticket_name():
    reason = "refused: 5 hits: " + ", ".join(f"tests/{'d' * 60}/test_{i}.py:{i} product" for i in range(5))
    line = gs.brain_line("orch", "#211", reason)
    assert len(line) <= 160
    assert "merge #211: GitHub refused: 5 hits" in line
