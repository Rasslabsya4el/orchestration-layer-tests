"""Issue 1115 unit 2: a home merge, after its push, runs `core update` once in each target of the home's
`targets` key (folder + repo) and writes one line per target into the merge result record.

Rows (issue #1115): 1 both idle targets take the home's head; 2 (first half) a live ORCH refuses at
the merge (`refused: ORCH live`); 3 a failing home read leaves `failed:` in the result and in the
target's `artifacts/runs/dispatch.out`, the merge rc stays 0; 4 the target's update commit touches only
manifest files, `config/orchestration.json` and the starter `config/core_leak_patterns.json` (#93); 5 a home with no `targets` key: no core_targets line.

Fakes: `FakeGh` answers the two `gh api` shapes `core update` reads (commits/main, contents/<path>);
targets are real git clones with a bare origin, so the commit and push run for real. No network."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

from tests.home_tree_fake import tree_reply
from tools.agentops import core_targets
from tools.agentops import detach

HOME = "owner/home"
HEAD = "a" * 40
OLD = "0" * 40
MANIFEST_ENTRY = "tools/agentops/core_manifest.txt"
CORE_FILES = ["core/f01.txt", "core/f02.txt"]
MANIFEST_TEXT = "\n".join([MANIFEST_ENTRY, *CORE_FILES]) + "\n"


class Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeGh:
    """The home's GitHub: head sha and file bytes at that head. `fail` makes every call fail."""

    def __init__(self, head: str = HEAD, fail: bool = False) -> None:
        self.head = head
        self.fail = fail
        self.files = {MANIFEST_ENTRY: MANIFEST_TEXT.encode("utf-8"),
                      CORE_FILES[0]: b"v1\n", CORE_FILES[1]: b"v1\n"}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> Completed:
        self.calls.append(list(args))
        if self.fail:
            return Completed(1, "", "gh: connection refused")
        path = args[1].split("/", 3)[3]  # repos/<owner>/<name>/<rest>
        if path == "commits/main":
            return Completed(0, self.head + "\n")
        if path.startswith("git/trees/"):  # #148: the home's tree, one call
            return Completed(0, tree_reply(self.files).stdout)
        if path.startswith("contents/"):
            name = path[len("contents/"):].partition("?ref=")[0]
            if name not in self.files:
                return Completed(1, "", "gh: not found")
            return Completed(0, base64.b64encode(self.files[name]).decode("ascii") + "\n")
        return Completed(1, "", f"gh: unexpected {args}")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"
    return result


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _settings(update_rule: str, core_version: str = OLD, targets: list | None = None) -> str:
    data = {"update_rule": update_rule, "home": HOME, "core_version": core_version}
    if targets is not None:
        data["targets"] = targets
    return json.dumps(data, indent=2) + "\n"


def _target(tmp_path: Path, name: str, *, live: bool = False) -> Path:
    """A target folder: a clone of its own bare origin, with its settings committed and pushed."""
    bare = tmp_path / f"{name}-origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / f"{name}-seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _git(seed, "config", "user.email", "agentops-test@example.invalid")
    _git(seed, "config", "user.name", "agentops test")
    _write(seed, "config/orchestration.json", _settings("auto"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    folder = tmp_path / name
    _git(tmp_path, "clone", "-q", str(bare), str(folder))
    _git(folder, "config", "user.email", "agentops-test@example.invalid")
    _git(folder, "config", "user.name", "agentops test")
    (folder / "var" / "locks").mkdir(parents=True, exist_ok=True)
    if live:
        _write(folder, "artifacts/runs/orch_session.json", json.dumps({"sessions": [{"pid": 1}]}))
    return folder


def _settings_of(folder: Path) -> dict:
    return json.loads((folder / "config" / "orchestration.json").read_text(encoding="utf-8"))


def _core_update_commits(folder: Path) -> list[str]:
    log = _git(folder, "log", "--format=%s", "origin/main").stdout.splitlines()
    return [subject for subject in log if subject.startswith("Core update:")]


def _home(tmp_path: Path, targets: list | None) -> Path:
    """The home checkout (merge --cwd): its settings name the targets; its origin/main is pushed."""
    bare = tmp_path / "home-origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    home = tmp_path / "home"
    _git(tmp_path, "clone", "-q", str(bare), str(home))
    _git(home, "config", "user.email", "agentops-test@example.invalid")
    _git(home, "config", "user.name", "agentops test")
    _write(home, "config/orchestration.json", _settings("home", core_version=HEAD, targets=targets))
    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "home")
    _git(home, "push", "-q", "origin", "main")
    return home


# Row 1 -- home merge, two idle targets: both take the home's head, one `Core update:` commit each.
def test_row1_two_idle_targets_take_the_home_head(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1")
    t2 = _target(tmp_path, "t2")
    gh = FakeGh()
    targets = [{"folder": str(t1), "repo": "owner/t1"}, {"folder": str(t2), "repo": "owner/t2"}]

    lines = core_targets.run_core_targets(targets, home_root=tmp_path, gh_runner=gh)

    assert lines == ["owner/t1: taken " + HEAD[:8], "owner/t2: taken " + HEAD[:8]], lines
    for folder in (t1, t2):
        assert _settings_of(folder)["core_version"] == HEAD
        assert len(_core_update_commits(folder)) == 1, _core_update_commits(folder)


# Row 2, first half -- a live ORCH in the target: `refused: ORCH live` at the merge, nothing written.
def test_row2_live_orch_target_is_refused_at_the_merge(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1", live=True)
    gh = FakeGh()
    targets = [{"folder": str(t1), "repo": "owner/t1"}]

    lines = core_targets.run_core_targets(targets, home_root=tmp_path, gh_runner=gh)

    assert lines == ["owner/t1: refused: ORCH live"], lines
    assert _settings_of(t1)["core_version"] == OLD
    assert _core_update_commits(t1) == []
    assert gh.calls == [], "a refused target must not read the home"


# Row 3 -- the home read fails: `failed:` in the result lines and in the target's dispatch.out; no raise.
def test_row3_failing_home_read_is_a_failed_line_and_a_log_line(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1")
    gh = FakeGh(fail=True)
    targets = [{"folder": str(t1), "repo": "owner/t1"}]

    lines = core_targets.run_core_targets(targets, home_root=tmp_path, gh_runner=gh)

    assert len(lines) == 1 and lines[0].startswith("owner/t1: failed: "), lines
    assert "gh api" in lines[0], lines
    dispatch_out = (t1 / "artifacts" / "runs" / "dispatch.out").read_text(encoding="utf-8")
    assert "core update" in dispatch_out and "failed" in dispatch_out, dispatch_out
    assert _settings_of(t1)["core_version"] == OLD


# Row 4 -- the target's update commit touches only manifest files, config/orchestration.json and the #93 starter.
def test_row4_the_update_commit_touches_only_manifest_files_and_core_version(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1")
    gh = FakeGh()
    targets = [{"folder": str(t1), "repo": "owner/t1"}]

    core_targets.run_core_targets(targets, home_root=tmp_path, gh_runner=gh)

    changed = set(_git(t1, "diff", "--name-only", "HEAD~1", "HEAD").stdout.split())
    assert changed == {MANIFEST_ENTRY, *CORE_FILES, "config/orchestration.json",
                       "config/core_leak_patterns.json"}, changed


# Row 5 -- a home with no `targets` key: the merge result carries no core_targets line, no gh call.
def test_row5_home_without_targets_key_runs_no_update(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path, targets=None)
    gh = FakeGh()
    monkeypatch.setattr(detach, "_core_targets_gh_runner", lambda: gh)
    result_file = tmp_path / "results" / "r.json"
    rc = detach.run_and_record([sys.executable, "-c", "pass", "--cwd", str(home)], result_file,
                               "merge", "b1", merge_log=tmp_path / "merge.log")

    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert rc == 0
    assert "core_targets" not in record, record
    assert gh.calls == []


# Wiring -- a home merge with targets: the detach result record names one line per target, the rc stays 0
# whatever a target did (a failed target included).
def test_detach_record_carries_one_line_per_target_and_keeps_rc(tmp_path: Path, monkeypatch) -> None:
    t1 = _target(tmp_path, "t1")
    t2 = _target(tmp_path, "t2", live=True)
    targets = [{"folder": str(t1), "repo": "owner/t1"}, {"folder": str(t2), "repo": "owner/t2"}]
    home = _home(tmp_path, targets=targets)
    gh = FakeGh()
    monkeypatch.setattr(detach, "_core_targets_gh_runner", lambda: gh)

    result_file = tmp_path / "results" / "r.json"
    rc = detach.run_and_record([sys.executable, "-c", "pass", "--cwd", str(home)], result_file,
                               "merge", "b1", merge_log=tmp_path / "merge.log")

    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert rc == 0
    assert record["core_targets"] == ["owner/t1: taken " + HEAD[:8], "owner/t2: refused: ORCH live"]
    assert _settings_of(t1)["core_version"] == HEAD
    assert _settings_of(t2)["core_version"] == OLD
