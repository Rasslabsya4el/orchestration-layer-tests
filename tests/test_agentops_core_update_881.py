"""#881: `core status` reads the waiting commits' changed files in a few gh calls, not one per commit.
A counted fake `gh` serves the home's history; the result must equal the per-commit reading."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

from tools.agentops import core_update

HOME = "someone/home-repo"
MANIFEST_PATH = "tools/agentops/core_manifest.txt"
PAGE = 100


def _result(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _sha(n: int) -> str:
    return f"{n:040x}"


class FakeHome:
    """A history of ``commits`` (oldest first, each ``{sha, parents, files}``); ``files`` is what
    ``commits/<sha>`` lists (for a merge: what it brought). Counts every gh call."""

    def __init__(self, commits: list[dict], manifest: list[str], base: str) -> None:
        self.commits = commits
        self.manifest = manifest
        self.base = base
        self.head = commits[-1]["sha"]
        self.calls: list[list[str]] = []
        self.per_commit_calls = 0

    def runner(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return _result("[]")
        path = args[1] if len(args) > 1 else ""
        if args[1] == "graphql":
            return _result(self._graphql(args[3]))
        if path.endswith("/commits/main"):
            return _result(self.head + "\n")
        m = re.search(r"/commits/([0-9a-f]{40})$", path)
        if m:  # the old per-commit read
            self.per_commit_calls += 1
            row = next(c for c in self.commits if c["sha"] == m.group(1))
            return _result("".join(f + "\n" for f in row["files"]))
        if "/compare/" in path:
            return _result(self._compare(path))
        if "/contents/" in path:
            text = "\n".join(self.manifest) + "\n"
            return _result(base64.b64encode(text.encode()).decode())
        raise AssertionError(f"unexpected gh call: {args}")

    def _compare(self, path: str) -> str:
        page = int(re.search(r"&page=(\d+)", path).group(1))
        size = int(re.search(r"per_page=(\d+)", path).group(1))
        start = next(i for i, c in enumerate(self.commits) if c["sha"] == self.base) + 1 \
            if any(c["sha"] == self.base for c in self.commits) else 0
        rows = self.commits[start:][(page - 1) * size: page * size]
        return "".join(json.dumps({"sha": c["sha"], "message": f"msg {c['sha'][:8]}",
                                   "parents": c["parents"], "date": "2026-01-01T00:00:00Z"}) + "\n"
                       for c in rows)

    def _graphql(self, query: str) -> str:
        repo: dict = {}
        for alias, rest in re.findall(r'(h\d+): object\(oid: "[0-9a-f]+"\) \{(.*?)\} \} \} \}', query):
            path = json.loads(re.search(r'path: ("[^"]*")', rest).group(1))
            after = re.search(r'after: "(\d+)"', rest)
            offset = int(after.group(1)) if after else 0
            # the own change of a non-merge commit; newest first
            touching = [c["sha"] for c in reversed(self.commits)
                        if len(c["parents"]) < 2 and path in c["files"]]
            nodes = touching[offset: offset + PAGE]
            more = offset + PAGE < len(touching)
            repo[alias] = {"history": {
                "nodes": [{"oid": s, "committedDate": "2026-01-01T00:00:00Z"} for s in nodes],
                "pageInfo": {"hasNextPage": more, "endCursor": str(offset + PAGE) if more else None}}}
        return json.dumps({"data": {"repository": repo}})


def _target(tmp_path: Path, base: str) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(
        json.dumps({"home": HOME, "update_rule": "word", "core_version": base}), encoding="utf-8")
    return tmp_path


def _per_commit_truth(home: FakeHome) -> set[str]:
    manifest = set(home.manifest)
    start = next(i for i, c in enumerate(home.commits) if c["sha"] == home.base) + 1
    return {c["sha"] for c in home.commits[start:] if manifest & set(c["files"])}


def _history(n: int, manifest: list[str]) -> FakeHome:
    commits = [{"sha": _sha(0), "parents": [], "files": []}]
    for i in range(1, n + 1):
        if i % 10 == 0 and i > 20:  # a merge bringing the two commits before it
            commits.append({"sha": _sha(i), "parents": [_sha(i - 3), _sha(i - 1)],
                            "files": list(commits[-1]["files"]) + list(commits[-2]["files"])})
        else:
            file = manifest[i % len(manifest)] if i % 3 == 0 else f"docs/other{i}.md"
            commits.append({"sha": _sha(i), "parents": [commits[-1]["sha"]], "files": [file]})
    return FakeHome(commits, manifest, base=_sha(0))


def test_881_1207_waiting_commits_take_at_most_15_gh_calls(tmp_path) -> None:
    manifest = [MANIFEST_PATH] + [f"core/file{k}.md" for k in range(60)]
    home = _history(1207, manifest)
    status = core_update.run_core_status(_target(tmp_path, home.base), gh_runner=home.runner)
    assert len(home.calls) <= 15, f"{len(home.calls)} gh calls"
    assert home.per_commit_calls == 0
    assert status.changes == len(_per_commit_truth(home))
    assert status.changes > 0


def test_881_result_equals_the_per_commit_reading_on_a_small_fixture(tmp_path) -> None:
    manifest = [MANIFEST_PATH, "a.txt", "b.txt"]
    home = _history(60, manifest)
    reader = core_update.HomeReader(HOME, gh_runner=home.runner)
    commits = reader.commits_between_with_messages(home.base, home.head)
    history = reader.paths_history(home.head, manifest, "2025-01-01T00:00:00Z")
    direct = set().union(*history.values())
    got = {c["sha"] for c in commits if core_update._touches(c, commits, direct)}
    assert got == _per_commit_truth(home)
    assert any(len(c["parents"]) > 1 for c in commits)
    status = core_update.run_core_status(_target(tmp_path, home.base), gh_runner=home.runner)
    assert status.changes == len(got)
