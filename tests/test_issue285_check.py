"""#285 unit 1: a test copy carries stand-in files, drops every other project's settings entry, and no longer judges
the repo's own leak words. Real git in a temp repo; the machine file sits in a temp ``AGENTOPS_MACHINE_DIR``. The
product name is a made-up one (``Zorblax``).

Rows tested: 1 (``test_copy_standins``: the copy has the stand-in's bytes, not the stand-in's own path; a missing
stand-in or an untracked mapped path is a ``CheckError`` naming both paths), 2 (every top-level list of the settings
file loses its ``product`` entries, key order kept), 3 (``config/core_leak_patterns.json`` is not read by the
test-copy check; ``core_check`` still reports its words)."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from tools.agentops import core_check
from tools.agentops import test_copy_check as tc
from tools.agentops.test_copy_cmd import run_test_copy_command

PRODUCT = "Zorblax"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _machine(tmp_path: Path, monkeypatch, patterns: dict | None) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    if patterns is not None:
        (machine / "test_copy_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


def _settings(**extra) -> str:
    return json.dumps(extra, indent=2)


def test_row1_a_stand_in_replaces_the_mapped_file_and_is_not_in_the_snapshot(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {
        "big.bin": b"B" * 100, "standin.bin": b"small", "keep.py": "X = 1\n",
        "config/orchestration.json": _settings(test_copy_standins={"big.bin": "standin.bin"}),
    })
    patterns = tc.load_patterns(repo)
    assert tc.snapshot_files(repo) == ["big.bin", "config/orchestration.json", "keep.py"]
    assert tc.snapshot_bytes(repo, "big.bin", patterns) == b"small"
    assert tc.snapshot_bytes(repo, "keep.py", patterns) == b"X = 1\n"
    # the scan runs over the stand-in's bytes
    (repo / "standin.bin").write_bytes(("mail ivan" + "@" + "mailhost.io\n").encode())
    assert tc.scan(repo) == [tc.Hit("big.bin", 1, "email")]
    # a scan of an already built copy (no stand-in file, mapping still in its settings) does not re-apply them
    copy = tmp_path / "copy"
    (copy / "config").mkdir(parents=True)
    (copy / "big.bin").write_bytes(b"small")
    (copy / "config/orchestration.json").write_text(_settings(test_copy_standins={"big.bin": "standin.bin"}))
    assert tc.scan(copy, patterns, ["big.bin", "config/orchestration.json"]) == []
    assert tc.snapshot_bytes(copy, "big.bin", patterns, standins=False) == b"small"


def test_row1_a_missing_stand_in_or_an_untracked_mapped_path_names_both_paths(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {
        "big.bin": b"B", "config/orchestration.json": _settings(test_copy_standins={"big.bin": "gone.bin"}),
    })
    with pytest.raises(tc.CheckError) as err:
        tc.snapshot_bytes(repo, "big.bin", tc.load_patterns(repo))
    assert "big.bin" in str(err.value) and "gone.bin" in str(err.value)
    with pytest.raises(tc.CheckError) as err:
        tc.snapshot_files(repo)
    assert "big.bin" in str(err.value) and "gone.bin" in str(err.value)

    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    _git(tmp_path, "init", "-q", str(repo2))
    (repo2 / "config").mkdir()
    (repo2 / "config/orchestration.json").write_text(_settings(test_copy_standins={"nope.bin": "standin.bin"}))
    (repo2 / "standin.bin").write_bytes(b"s")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-q", "-m", "fixture")
    with pytest.raises(tc.CheckError) as err:
        tc.snapshot_bytes(repo2, "nope.bin", tc.load_patterns(repo2))
    assert "nope.bin" in str(err.value) and "standin.bin" in str(err.value)
    with pytest.raises(tc.CheckError):
        tc.snapshot_files(repo2)


def test_row2_every_top_level_list_loses_its_product_entries_key_order_kept(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT})
    text = json.dumps({
        "zeta": 1,
        "targets": [{"name": "keep-t", "root": "C:/a"}, {"name": PRODUCT, "root": "C:/b"}],
        "mid": {"x": 1},
        "loss_repos": [{"name": "keep-l"}, {"name": "u", "root": "C:/" + PRODUCT.lower() + "/x"}],
        "alpha": ["clean"],
    }, indent=2) + "\n"
    repo = _repo(tmp_path, {"config/orchestration.json": text})
    out = tc.snapshot_bytes(repo, "config/orchestration.json", tc.load_patterns(repo))
    got = json.loads(out)
    assert list(got) == ["zeta", "targets", "mid", "loss_repos", "alpha"]
    assert got["targets"] == [{"name": "keep-t", "root": "C:/a"}]
    assert got["loss_repos"] == [{"name": "keep-l"}]
    assert got["alpha"] == ["clean"] and got["mid"] == {"x": 1}
    assert out.decode().endswith("\n") and out.decode().startswith('{\n  "zeta"')
    assert tc.scan(repo) == []


def test_row3_the_check_does_not_read_the_leak_file_but_core_check_does(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {
        "config/core_leak_patterns.json": json.dumps({"L1": "widgetword", "L2": "gizmoword", "L3": "doodadword"}),
        "pkg/a.py": "# widgetword gizmoword doodadword\n",
    })
    patterns = tc.load_patterns(repo)
    assert list(patterns.compiled) == ["email", "user-path", "telegram-id"]
    assert "L1-L3" not in patterns.not_checked
    assert tc.scan(repo) == []
    rc = run_test_copy_command(argparse.Namespace(test_copy_command="check", root=repo))
    out = capsys.readouterr().out
    assert rc == 0 and "L1" not in out and "L2" not in out and "L3" not in out
    # an unparseable leak file is not this check's business either
    (repo / "config/core_leak_patterns.json").write_text("{not json")
    tc.load_patterns(repo)
    # core_check on the same repo still reports the words
    (repo / "config/core_leak_patterns.json").write_text(
        json.dumps({"L1": "widgetword", "L2": "gizmoword", "L3": "doodadword"}))
    leaks = core_check.read_patterns(repo / "config/core_leak_patterns.json")
    row = core_check.check_file(repo / "pkg/a.py", "pkg/a.py", leaks)
    assert (row.counts["L1"], row.counts["L2"], row.counts["L3"]) == (1, 1, 1)
