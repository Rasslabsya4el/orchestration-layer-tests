"""#232 unit 0 (D119): the test copy carries a sanitized ``config/orchestration.json``. Real git in temp dirs; the
remote is a local bare repo; the machine file sits in a temp ``AGENTOPS_MACHINE_DIR``. The product name is a made-up
one (``Zorblax``). Tested: ``snapshot_bytes`` drops the ``targets`` entries whose JSON text matches the product
pattern, the push carries no product text, the check reports 0 hits while the working file is unchanged on disk, and
unparseable json, a file without ``targets``, no product pattern and any other file pass their bytes through."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops import test_copy_check as tc
from tools.agentops.test_copy_check import snapshot_bytes
from tools.agentops.test_copy_send import send
from tools.agentops.util import no_window_flags

PRODUCT = "Zorblax"
OWNER = "Ivan" + r"\s+Surname"
SETTINGS = {
    "repo": "me/home",
    "targets": [
        {"repo": "me/" + PRODUCT, "folder": "C:/Coding/" + PRODUCT.lower()},
        {"repo": "me/Other", "folder": "C:/Coding/other"},
    ],
    "areas": ["orchestration"],
}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _repo(tmp_path: Path, settings_text: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in {"config/orchestration.json": settings_text, "pkg/a.py": "A = 1\n"}.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _machine(tmp_path: Path, monkeypatch, patterns: dict) -> None:
    machine = tmp_path / "machine"
    machine.mkdir(exist_ok=True)
    (machine / "test_copy_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


def _planted(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT, "owner-name": OWNER})
    return _repo(tmp_path, json.dumps(SETTINGS, indent=2) + "\n")


def test_snapshot_bytes_keeps_only_the_other_target_in_key_order(tmp_path, monkeypatch):
    repo = _planted(tmp_path, monkeypatch)
    out = json.loads(snapshot_bytes(repo, "config/orchestration.json", tc.load_patterns(repo)))
    assert out["targets"] == [SETTINGS["targets"][1]]
    assert list(out) == list(SETTINGS)
    assert PRODUCT.encode() not in snapshot_bytes(repo, "config/orchestration.json", tc.load_patterns(repo))


def test_push_carries_no_product_text_and_check_is_clean_working_file_untouched(tmp_path, monkeypatch, capsys):
    repo = _planted(tmp_path, monkeypatch)
    before = (repo / "config/orchestration.json").read_bytes()
    bare = tmp_path / "copy.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    result = send(repo, "m1", str(bare))
    assert result.sent, capsys.readouterr().out
    pushed = json.loads(_git(bare, "show", "m1:config/orchestration.json"))
    assert pushed["targets"] == [SETTINGS["targets"][1]]
    found = subprocess.run(["git", "-C", str(bare), "grep", "-c", PRODUCT, "m1"], capture_output=True, text=True,
                           creationflags=no_window_flags())
    assert found.stdout == "" and found.returncode == 1
    assert [h for h in tc.scan(repo) if h.kind == "product"] == []
    assert (repo / "config/orchestration.json").read_bytes() == before
    assert PRODUCT.encode() in before


def test_unparseable_and_no_targets_and_other_files_pass_through(tmp_path, monkeypatch):
    repo = _planted(tmp_path, monkeypatch)
    patterns = tc.load_patterns(repo)
    assert snapshot_bytes(repo, "pkg/a.py", patterns) == (repo / "pkg/a.py").read_bytes()
    settings = repo / "config/orchestration.json"
    settings.write_text("{ not json " + PRODUCT, encoding="utf-8")
    assert snapshot_bytes(repo, "config/orchestration.json", patterns) == settings.read_bytes()
    settings.write_text(json.dumps({"repo": PRODUCT}), encoding="utf-8")
    assert snapshot_bytes(repo, "config/orchestration.json", patterns) == settings.read_bytes()


def test_no_product_pattern_drops_nothing(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, {"owner-name": OWNER})
    repo = _repo(tmp_path, json.dumps(SETTINGS, indent=2) + "\n")
    got = snapshot_bytes(repo, "config/orchestration.json", tc.load_patterns(repo))
    assert json.loads(got)["targets"] == SETTINGS["targets"]
