"""#343 unit 4: the test map built on GitHub and installed on the PC.

GitHub is faked (gh, send, download); the install check uses a real tiny git repo and a real testmon database."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.agentops import map_github as mg
from tools.agentops import test_select
from tools.agentops.test_copy_send import SendResult
from tools.agentops.util import AgentOpsError

REMOTE = "https://github.com/owner/copy-repo.git"
SETTINGS = {"test_copy_remote": REMOTE}
SHA = "abc123def456"  # the working repo's commit: what build_on_github is given
COPY_SHA = "copy0sha9999"  # the test copy's commit the send pushed (one parentless commit, never SHA)
WALL = datetime(2026, 10, 23, 10, 0, 0, tzinfo=timezone.utc)
GREEN_VIEW = {"status": "completed", "conclusion": "success", "jobs": [
    {"name": "map-job", "status": "completed", "conclusion": "success",
     "startedAt": "2026-10-23T10:00:00Z", "completedAt": "2026-10-23T10:07:00Z"}]}
RED_VIEW = {"status": "completed", "conclusion": "failure", "jobs": GREEN_VIEW["jobs"]}


def map_zip(*, built_at="2026-10-23T10:00:00Z", green=True, failed_ids=None, sha=COPY_SHA,
            testmon=b"sqlite-bytes-new", tests=3) -> bytes:
    """An artifact zip with the files the unit-3 contract names. ``sha`` and ``map_commit`` are the copy's commit;
    ``green=None`` leaves the ``green`` key out of meta.json."""
    meta = {"built_at": built_at, "sha": sha, "tests": tests, "map_commit": sha, "total": tests}
    if green is not None:
        meta["green"] = green
    if green is True:
        meta["last_green_commit"] = sha
    if failed_ids is not None:
        meta["failed_ids"] = failed_ids
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("testmondata", testmon)
        zf.writestr("reads/reads-1.json", json.dumps({"files": {}, "dirs": {}}))
        zf.writestr("meta.json", json.dumps(meta))
    return buf.getvalue()


def serve(payload: bytes):
    """A ``download`` fake that writes ``payload`` as the archive."""
    def download(repo, artifact_id, archive):
        Path(archive).write_bytes(payload)
    return download


def old_map(map_dir: Path) -> None:
    map_dir.mkdir(parents=True)
    (map_dir / "testmondata").write_bytes(b"old")
    (map_dir / "meta.json").write_text(json.dumps({"built_at": "2026-10-20T01:08:00Z"}), encoding="utf-8")


def assert_old_map_intact(map_dir: Path) -> None:
    assert (map_dir / "testmondata").read_bytes() == b"old"
    assert json.loads((map_dir / "meta.json").read_text(encoding="utf-8"))["built_at"] == "2026-10-20T01:08:00Z"


class FakeGh:
    """The ``gh`` callable: dispatch, run list, run view and the artifact list, answered from fields."""

    def __init__(self, *, artifacts=None, run_view=None, dispatch_exc=None, view_exc=None):
        self.calls: list[tuple] = []
        self.artifacts = [{"id": 9, "name": "test-map", "expired": False}] if artifacts is None else artifacts
        self.run_view = GREEN_VIEW if run_view is None else run_view
        self.dispatch_exc, self.view_exc = dispatch_exc, view_exc

    def __call__(self, args, input=None):
        self.calls.append((list(args), input))
        if args[:2] == ["workflow", "run"]:
            if self.dispatch_exc:
                raise self.dispatch_exc
            return ""
        if args[:2] == ["run", "list"]:
            return json.dumps([{"databaseId": 42, "status": "queued", "createdAt": "2026-10-23T10:00:10Z",
                                "event": "workflow_dispatch"}])
        if args[:2] == ["run", "view"]:
            if self.view_exc:
                raise self.view_exc
            return json.dumps(self.run_view)
        if args[0] == "api":
            return json.dumps({"artifacts": self.artifacts})
        raise AssertionError(f"unexpected gh call {args}")


def make_deps(gh: FakeGh, *, sent=True, download=None, records=None, record=None) -> mg.Deps:
    if record is None:
        record = records.append if records is not None else (lambda entry: None)
    return mg.Deps(
        send=lambda root, branch, remote: SendResult(sent, COPY_SHA if sent else None,
                                                     None if sent else "1 hit: x.py:1 kind"),
        gh=gh, clock=lambda: 0.0, wall=lambda: WALL, sleep=lambda seconds: None,
        download=download or serve(map_zip()),
        record=record,
    )


def build(tmp_path: Path, deps: mg.Deps, map_dir: Path | None = None, settings: dict | None = None):
    settings = SETTINGS if settings is None else settings  # an empty dict is given as it is: no copy, no own runners
    return mg.build_on_github(root=tmp_path / "repo", branch="merge-x", sha=SHA, settings=settings,
                              map_dir=map_dir or (tmp_path / "var" / "agentops" / "test_map"), deps=deps)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo: Path, rel: str, text: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_testmon_db(path: Path, deps_by_test: dict[str, list[str]]) -> bytes:
    """A real testmon database (the three tables ``load_testmon`` reads): each test with the files it depends on."""
    con = sqlite3.connect(path)
    con.executescript("""
        create table test_execution (id integer primary key, test_name text);
        create table file_fp (id integer primary key, filename text);
        create table test_execution_file_fp (test_execution_id integer, fingerprint_id integer);
    """)
    fp_ids: dict[str, int] = {}
    for i, (test, files) in enumerate(deps_by_test.items(), start=1):
        con.execute("insert into test_execution values (?, ?)", (i, test))
        for fn in files:
            if fn not in fp_ids:
                fp_ids[fn] = len(fp_ids) + 1
                con.execute("insert into file_fp values (?, ?)", (fp_ids[fn], fn))
            con.execute("insert into test_execution_file_fp values (?, ?)", (i, fp_ids[fn]))
    con.commit()
    con.close()
    return path.read_bytes()


def test_green_installs_the_map_and_built_at_is_readable(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    gh = FakeGh()
    result = build(tmp_path, make_deps(gh, download=serve(map_zip())), map_dir)

    assert result.status == "green"
    assert result.green is True
    assert result.run_id == 42
    assert result.built_at == "2026-10-23T10:00:00Z"
    assert json.loads((map_dir / "meta.json").read_text(encoding="utf-8"))["built_at"] == "2026-10-23T10:00:00Z"
    assert (map_dir / "testmondata").read_bytes() == b"sqlite-bytes-new"
    assert (map_dir / "reads" / "reads-1.json").is_file()
    assert sorted(p.name for p in map_dir.parent.iterdir()) == ["test_map"]  # no temp or old folder left


def test_install_stamps_the_working_sha_and_the_map_selects_from_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    base = commit_file(repo, "pkg/mod.py", "x = 1\n", "base")
    head = commit_file(repo, "pkg/mod.py", "x = 2\n", "change")
    db = make_testmon_db(tmp_path / "testmon.sqlite", {"tests/test_mod.py::test_a": ["pkg/mod.py"],
                                                  "tests/test_other.py::test_b": ["pkg/other.py"]})
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    result = mg.build_on_github(root=repo, branch="merge-x", sha=base, settings=SETTINGS, map_dir=map_dir,
                               deps=make_deps(FakeGh(), download=serve(map_zip(testmon=db, tests=2))))

    assert result.status == "green"
    meta = test_select.read_meta(map_dir)
    assert meta is not None
    assert meta["map_commit"] == base  # the working repo's sha, not the copy's
    assert meta["last_green_commit"] == base
    assert meta["total"] == 2
    sel = test_select.select_for_diff(repo, base, head, map_dir)
    assert sel.full is False, sel.reason
    assert "tests/test_mod.py::test_a" in sel.ids
    assert "tests/test_other.py::test_b" not in sel.ids


def test_the_branch_goes_to_the_test_copy_and_dispatch_sends_only_the_ref(tmp_path):
    gh = FakeGh()
    seen = []
    deps = make_deps(gh)
    deps.send = lambda root, branch, remote: (seen.append((branch, remote)), SendResult(True, COPY_SHA))[1]
    build(tmp_path, deps)

    assert seen == [("merge-x", REMOTE)]
    workflow_calls = [(args, body) for args, body in gh.calls if args[:2] == ["workflow", "run"]]
    assert len(workflow_calls) == 1
    args, body = workflow_calls[0]
    assert "map.yml" in args and "--ref" in args and "merge-x" in args
    assert json.loads(body) == {"ref": "merge-x"}


def test_red_build_leaves_the_old_map_and_names_failed_ids(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    gh = FakeGh(run_view=RED_VIEW)
    result = build(tmp_path, make_deps(gh, download=serve(
        map_zip(green=False, failed_ids=["tests/test_a.py::test_b"]))), map_dir)

    assert result.status == "red"
    assert result.green is False
    assert result.failed_ids == ["tests/test_a.py::test_b"]
    assert_old_map_intact(map_dir)


@pytest.mark.parametrize("failed_ids", [None, []])
def test_a_red_run_with_no_failed_ids_is_a_fallback_not_a_proven_code_red(tmp_path, failed_ids):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    gh = FakeGh(run_view=RED_VIEW)
    result = build(tmp_path, make_deps(gh, download=serve(map_zip(green=False, failed_ids=failed_ids))), map_dir)

    assert result.status == "fallback" and result.reason == "red without ids"
    assert result.failed_ids == []
    assert_old_map_intact(map_dir)


def test_a_failed_run_without_an_artifact_is_a_no_artifact_fallback_never_red(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    result = build(tmp_path, make_deps(FakeGh(run_view=RED_VIEW, artifacts=[])), map_dir)

    assert result.status == "fallback"
    assert result.reason == "no-artifact"
    assert_old_map_intact(map_dir)


@pytest.mark.parametrize("green", [True, None], ids=["green-true", "green-absent"])
def test_a_failed_run_whose_artifact_does_not_say_green_false_is_run_failed_no_map(tmp_path, green):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    result = build(tmp_path, make_deps(FakeGh(run_view=RED_VIEW), download=serve(map_zip(green=green))), map_dir)

    assert result.status == "fallback"
    assert result.reason == "run-failed-no-map"
    assert_old_map_intact(map_dir)


def test_broken_zip_leaves_the_old_map_and_is_a_fallback_never_red(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    truncated = map_zip()[:40]  # a download cut short: the zip directory is gone
    result = build(tmp_path, make_deps(FakeGh(), download=serve(truncated)), map_dir)

    assert result.status == "fallback"
    assert result.reason.startswith("download-failed")
    assert_old_map_intact(map_dir)
    assert sorted(p.name for p in map_dir.parent.iterdir()) == ["test_map"]


def test_dispatch_error_is_a_fallback_and_the_old_map_stays(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    records = []
    gh = FakeGh(dispatch_exc=AgentOpsError("workflow map.yml not found"))
    result = build(tmp_path, make_deps(gh, records=records), map_dir)

    assert result.status == "fallback"
    assert result.reason == "dispatch-failed: workflow map.yml not found"
    assert_old_map_intact(map_dir)
    assert records[0]["outcome"] == "dispatch-failed"
    assert records[0]["minutes"] == 0


def test_a_refused_send_is_a_fallback_and_nothing_is_dispatched(tmp_path):
    gh = FakeGh()
    result = build(tmp_path, make_deps(gh, sent=False))

    assert result.status == "fallback"
    assert result.reason.startswith("refused: ")
    assert not any(args[:2] == ["workflow", "run"] for args, _ in gh.calls)


@pytest.mark.parametrize("settings", [{}, {"test_copy_remote": "https://example.com/"}],
                         ids=["no-remote", "remote-without-owner-name"])
def test_no_test_copy_and_no_own_runners_is_a_no_places_fallback(tmp_path, settings):
    gh = FakeGh()
    result = build(tmp_path, make_deps(gh), settings=settings)

    assert result.status == "fallback"
    assert result.reason == "no-places"
    assert not gh.calls


def test_the_artifact_built_from_the_working_sha_is_a_wrong_sha_fallback(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    # the send pushed COPY_SHA; an artifact that names the working sha was not built from the merged copy
    result = build(tmp_path, make_deps(FakeGh(), download=serve(map_zip(sha=SHA))), map_dir)

    assert result.status == "fallback"
    assert result.reason == "wrong-sha"
    assert_old_map_intact(map_dir)


def test_no_artifact_is_a_fallback(tmp_path):
    map_dir = tmp_path / "var" / "agentops" / "test_map"
    old_map(map_dir)
    result = build(tmp_path, make_deps(FakeGh(artifacts=[])), map_dir)

    assert result.status == "fallback"
    assert result.reason == "no-artifact"
    assert_old_map_intact(map_dir)


def test_a_cancelled_run_is_lost_a_fallback_never_red(tmp_path):
    cancelled = {"status": "completed", "conclusion": "cancelled", "jobs": GREEN_VIEW["jobs"]}
    result = build(tmp_path, make_deps(FakeGh(run_view=cancelled)))

    assert result.status == "fallback"
    assert result.reason == "lost"


def test_a_run_whose_view_keeps_failing_is_a_poll_failed_fallback(tmp_path):
    result = build(tmp_path, make_deps(FakeGh(view_exc=RuntimeError("gh timed out"))))

    assert result.status == "fallback"
    assert result.reason == "poll-failed"


def test_minutes_of_the_run_are_recorded_as_a_map_build(tmp_path):
    records = []
    settings = {**SETTINGS, "repo": "owner/orchestration-layer"}
    result = build(tmp_path, make_deps(FakeGh(), records=records), settings=settings)

    assert result.minutes_used == 7
    assert result.record_error == ""
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "map-build"
    assert record["minutes"] == 7
    assert record["outcome"] == "green"
    assert record["run_id"] == 42
    assert record["repo"] == "orchestration-layer"  # the basename of settings repo
    assert record["slug"] == "owner/orchestration-layer"  # settings repo, as the merge suite passes it
    assert record["own_runner"] is False


def test_a_failed_record_is_named_in_the_result_and_never_changes_it(tmp_path):
    def broken(entry):
        raise OSError("disk full")

    result = build(tmp_path, make_deps(FakeGh(), record=broken))

    assert result.status == "green"
    assert "disk full" in result.record_error


def test_poll_gives_lost_when_the_run_outruns_its_bound():
    view = {"status": "in_progress", "conclusion": None, "jobs": [
        {"name": "map-job", "status": "in_progress", "startedAt": "2026-10-23T10:00:00Z"}]}
    ticks = iter(range(0, 10**9, 4000))  # each clock read is 4000 s later than the last
    outcome = mg.poll_map("owner/copy-repo", 42, FakeGh(run_view=view), since=0.0,
                          clock=lambda: float(next(ticks)), sleep=lambda seconds: None, wall=lambda: WALL)

    assert outcome["outcome"] == "lost"
    assert outcome["run_id"] == 42
