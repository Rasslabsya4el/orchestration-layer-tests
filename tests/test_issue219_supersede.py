"""#219 unit 3 (supersede): a merge queued for a branch whose older head is queued or running ends the older entry.

Rule 2 and row 3 of the issue. Seams: ``MergeLock.acquire(ticket_info=...)`` (what ``merge <branch>`` does at queue
time; ``MergeAlreadyQueued`` is what the CLI turns into ``already-queued`` exit 5), ``detach.run_and_record`` (the
result record and its reason word) and ``loop_stop.loop_status`` (#209's counter).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools.agentops import detach, loop_stop
from tools.agentops import merge_withdraw as mw
from tools.agentops.merge import MergeAlreadyQueued, MergeLock, MergeWithdrawn
from tools.agentops.util import no_window_flags, pid_alive

H1, H2 = "a" * 40, "b" * 40


def _lock(tmp_path):
    return tmp_path / "var" / "locks" / "merge.lock"


def _info(branch, head, result="", issues=(219,), cwd="C:/repo"):
    info = {"kind": "merge", "branch": branch, "suite": "s", "remote": "origin", "main": "main",
            "cwd": cwd, "issues": list(issues), "result": str(result)}
    if head is not None:
        info["head"] = head
    return info


def _queued(lock, info, pid=None, ns=None):
    queue = lock.with_name(lock.name + ".queue")
    queue.mkdir(parents=True, exist_ok=True)
    ns = ns or time.time_ns()
    ticket = queue / f"{ns:020d}-{pid or os.getpid()}-0"
    ticket.write_text(json.dumps(info), encoding="utf-8")
    return ticket


def _spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], creationflags=no_window_flags())


def _hold_by(lock, pid, branch="holder"):
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "token": "t",
                                "kind": "merge", "branch": branch}), encoding="utf-8")


def _tickets(lock):
    queue = lock.with_name(lock.name + ".queue")
    return sorted(queue.iterdir(), key=lambda t: t.name) if queue.exists() else []


def _wait_for(predicate, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _wait_file(path, seconds=30):
    assert _wait_for(lambda: path.exists() and path.read_text(encoding="utf-8").strip(), seconds), f"{path} never written"
    return path.read_text(encoding="utf-8").strip()


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, creationflags=no_window_flags()).stdout.strip()


def _commit(cwd, name):
    (Path(cwd) / name).write_text(name, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-m", name)
    return _git(cwd, "rev-parse", "HEAD")


def _repo(tmp_path):
    """A repo whose `origin/main` is c1; returns (cwd, c1 sha, c2 sha), c2 only on the branch `u219-b`."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _git(cwd, "init", "-q", "-b", "main")
    c1 = _commit(cwd, "a")
    _git(cwd, "update-ref", "refs/remotes/origin/main", c1)
    _git(cwd, "checkout", "-q", "-b", "u219-b")
    return cwd, c1, _commit(cwd, "b")


def _waiter(lock, info):
    """A started daemon thread that queues `info` behind the live holder; MergeWithdrawn (its end) is swallowed."""
    def run():
        try:
            MergeLock(lock).acquire(max_age_seconds=600, ticket_info=info)
        except MergeWithdrawn:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _end_waiters(lock, threads, locks=()):
    """Test cleanup: a waiter whose ticket is gone ends (MergeWithdrawn) at its next turn; the thread is joined."""
    for t in _tickets(lock):
        t.unlink()
    for th in threads:
        th.join(30)
    for lk in locks:
        try:
            lk.release()
        except Exception:  # noqa: BLE001 -- cleanup only
            pass


def test_a_queued_older_head_is_ended_and_the_new_head_takes_its_place_in_line(tmp_path):
    lock = _lock(tmp_path)
    bystander = _spawn("import time; time.sleep(300)")  # the live holder both waiters queue behind
    threads = []
    try:
        _hold_by(lock, bystander.pid)
        old_result = tmp_path / "results" / "old.json"
        old_result.parent.mkdir(parents=True)
        outcome = {}

        def wait_older():
            try:
                MergeLock(lock).acquire(max_age_seconds=600, ticket_info=_info("u219-b", H1, old_result))
                outcome["took"] = True
            except MergeWithdrawn as exc:
                outcome["ended"] = str(exc)

        older = threading.Thread(target=wait_older, daemon=True)
        older.start()
        threads.append(older)
        assert _wait_for(lambda: len(_tickets(lock)) == 1)
        other = _queued(lock, _info("u219-other", H1), ns=time.time_ns() + 10**12)  # a later entry of another branch
        old_ns = int(_tickets(lock)[0].name.split("-")[0])

        threads.append(_waiter(lock, _info("u219-b", H2, tmp_path / "results" / "new.json")))
        older.join(30)

        assert not older.is_alive() and "took" not in outcome, "the older entry kept waiting"
        assert _wait_for(lambda: len([t for t in _tickets(lock) if t != other]) == 1)
        mine = [t for t in _tickets(lock) if t != other][0]
        assert json.loads(mine.read_text(encoding="utf-8"))["head"] == H2
        assert int(mine.name.split("-")[0]) == old_ns, "the new head must keep the older entry's place in line"
        assert other.exists(), "an entry of another branch was touched"
        assert Path(str(old_result) + mw.MARKER_SUFFIX).read_text(encoding="utf-8") == f"superseded: replaced by {H2}"
        assert mw.marker_reason(old_result) == "superseded"
    finally:
        bystander.kill()
        _end_waiters(lock, threads)


def test_a_running_older_head_has_only_its_own_tree_ended_and_the_new_head_runs(tmp_path):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    old_result = tmp_path / "results" / "old.json"
    old_result.parent.mkdir(parents=True)
    child_file = tmp_path / "child.pid"
    holder = _spawn("import subprocess, sys, time\n"
                    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                    f"open(r'{child_file}', 'w').write(str(c.pid))\n"
                    "time.sleep(300)\n")
    bystander = _spawn("import time; time.sleep(300)")
    child_pid = 0
    try:
        child_pid = int(_wait_file(child_file))
        _hold_by(lock, holder.pid, "u219-b")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": _info("u219-b", H1, old_result)}), encoding="utf-8")

        new = MergeLock(lock)
        started = time.time()
        assert new.acquire(max_age_seconds=60, ticket_info=_info("u219-b", H2)) is None  # the new head runs
        assert time.time() - started < 60
        assert _wait_for(lambda: not pid_alive(holder.pid) and not pid_alive(child_pid)), "the old run's tree survived"
        assert pid_alive(bystander.pid), "a process outside the old run's tree was touched"
        assert mw.marker_reason(old_result) == "superseded"
        assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
        new.release()
    finally:
        for p in (holder, bystander):
            p.kill()
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass  # already ended by the supersede


def test_the_same_head_stays_already_queued_and_touches_nothing(tmp_path):
    lock = _lock(tmp_path)
    result = tmp_path / "old.json"
    ticket = _queued(lock, _info("u219-b", H1, result))
    with pytest.raises(MergeAlreadyQueued):
        MergeLock(lock).acquire(max_age_seconds=60, ticket_info=_info("u219-b", H1))
    assert ticket.exists()
    assert not Path(str(result) + mw.MARKER_SUFFIX).exists()


def test_an_older_record_without_a_head_is_never_superseded(tmp_path):
    lock = _lock(tmp_path)
    result = tmp_path / "old.json"
    ticket = _queued(lock, _info("u219-b", None, result))
    with pytest.raises(MergeAlreadyQueued):
        MergeLock(lock).acquire(max_age_seconds=60, ticket_info=_info("u219-b", H2))
    assert ticket.exists()
    assert not Path(str(result) + mw.MARKER_SUFFIX).exists()


def test_another_branch_is_never_superseded(tmp_path):
    lock = _lock(tmp_path)
    bystander = _spawn("import time; time.sleep(300)")
    threads = []
    try:
        _hold_by(lock, bystander.pid)
        result = tmp_path / "other.json"
        ticket = _queued(lock, _info("u219-other", H1, result))
        threads.append(_waiter(lock, _info("u219-b", H2)))
        assert _wait_for(lambda: len(_tickets(lock)) == 2)
        assert ticket.exists()
        assert not Path(str(result) + mw.MARKER_SUFFIX).exists()
    finally:
        bystander.kill()
        _end_waiters(lock, threads)


def test_a_superseded_run_records_reason_superseded_naming_the_new_head_and_posts_nothing(tmp_path, monkeypatch):
    calls = []

    class Gh:
        def __getattr__(self, name):
            def call(*a, **k):
                calls.append((name, a))
                return None
            return call

    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "killed\n", ""))
    result = tmp_path / "res.json"
    Path(str(result) + mw.MARKER_SUFFIX).write_text(f"superseded: replaced by {H2}", encoding="utf-8")
    argv = ["python", "-m", "tools.agentops", "merge", "u219-b", "--issue", "219"]
    rc = detach.run_and_record(argv, result, "merge", "u219-b", merge_log=tmp_path / "m.log", gh=Gh())
    record = json.loads(result.read_text(encoding="utf-8"))
    assert rc == 1 and record["reason"] == "superseded"
    assert f"did not land (superseded: replaced by {H2})" in record["output"]
    assert [c for c in calls if c[0] != "issue_status"] == [], calls


def test_the_withdrawn_marker_still_reads_withdrawn(tmp_path):
    result = tmp_path / "res.json"
    Path(str(result) + mw.MARKER_SUFFIX).write_text("withdrawn: by the owner", encoding="utf-8")
    assert mw.marker_reason(result) == "withdrawn"
    assert mw.marker_reason(tmp_path / "none.json") == ""


def test_row3_a_superseded_result_counts_as_no_attempt_in_the_failed_streak():
    def result(text):
        return {"body": f"<!-- merge result -->\nMerge of u219-b did not land ({text}) after 1 min.",
                "createdAt": "2026-10-19T10:00:00Z"}

    assert loop_stop.loop_status([result("superseded")] * 3).count == 0
    assert loop_stop.loop_status([result("red-on-branch"), result("superseded"), result("superseded")]).count == 1


def test_a_superseded_waiter_prints_did_not_land_superseded_naming_the_new_head(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from tools.agentops import merge

    result = tmp_path / "res.json"
    Path(str(result) + mw.MARKER_SUFFIX).write_text(f"superseded: replaced by {H2}", encoding="utf-8")
    monkeypatch.setenv(merge.ENV_MERGE_RESULT, str(result))
    monkeypatch.setattr(merge, "worktree_refusal", lambda cwd: "")
    monkeypatch.setattr(merge, "resolve_suite_cmd", lambda suite: "s")
    monkeypatch.setattr(merge, "resolve_suite_env", lambda: {})
    monkeypatch.setattr(merge, "resolve_suite_shared", lambda: ())

    def gone(*a, **k):
        raise MergeWithdrawn("the queued merge of u219-b was withdrawn")

    monkeypatch.setattr(merge, "run_merge", gone)
    args = SimpleNamespace(branch="u219-b", cwd=tmp_path, lock=_lock(tmp_path), timeout=None, suite=None,
                           issue=[219], queue_checked=True)
    assert merge.run_merge_command(args) == 1
    assert f"did not land (superseded: replaced by {H2})" in capsys.readouterr().out


# -- send-back items 1 to 3 ------------------------------------------------------------------------------------------

def test_the_new_entry_inherits_the_issues_of_the_entry_it_replaces(tmp_path, capsys):
    lock = _lock(tmp_path)
    bystander = _spawn("import time; time.sleep(300)")
    threads = []
    try:
        _hold_by(lock, bystander.pid)
        _queued(lock, _info("u219-b", H1, issues=(7,)))
        threads.append(_waiter(lock, _info("u219-b", H2, issues=())))  # the new call names no --issue
        assert _wait_for(lambda: any(json.loads(t.read_text(encoding="utf-8")).get("head") == H2
                                     for t in _tickets(lock)))
        mine = [json.loads(t.read_text(encoding="utf-8")) for t in _tickets(lock)]
        assert [m["issues"] for m in mine if m["head"] == H2] == [[7]]
        assert "merge-inherited: 7" in capsys.readouterr().err.splitlines()  # the line the result record reads
    finally:
        bystander.kill()
        _end_waiters(lock, threads)


def test_the_result_record_of_the_new_run_names_the_inherited_issues_too(tmp_path, monkeypatch):
    out = "merge-inherited: 7,9\nsome output\n"
    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, out, ""))
    result = tmp_path / "res.json"
    argv = ["python", "-m", "tools.agentops", "merge", "u219-b", "--issue", "5"]
    detach.run_and_record(argv, result, "merge", "u219-b", merge_log=tmp_path / "m.log", gh=_NoGh())
    assert json.loads(result.read_text(encoding="utf-8"))["issues"] == [5, 7, 9]


class _NoGh:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_a_running_older_head_already_in_origin_main_is_not_killed(tmp_path):
    cwd, c1, c2 = _repo(tmp_path)
    lock = _lock(tmp_path)
    holder = _spawn("import time; time.sleep(300)")
    try:
        _hold_by(lock, holder.pid, "u219-b")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": _info("u219-b", c1, cwd=str(cwd))}), encoding="utf-8")  # c1 landed
        with pytest.raises(MergeAlreadyQueued):
            MergeLock(lock).acquire(max_age_seconds=60, ticket_info=_info("u219-b", c2, cwd=str(cwd)))
        assert pid_alive(holder.pid), "a run that already landed was killed"
    finally:
        holder.kill()


def test_a_running_batch_leader_is_not_superseded(tmp_path):
    cwd, c1, c2 = _repo(tmp_path)
    lock = _lock(tmp_path)
    holder = _spawn("import time; time.sleep(300)")
    try:
        _hold_by(lock, holder.pid, "u219-b")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": _info("u219-b", c2, cwd=str(cwd))}), encoding="utf-8")
        batch = lock.with_name(lock.name + ".batch")
        batch.mkdir()
        member = _spawn("import time; time.sleep(300)")  # a live member claimed by the leader
        try:
            (batch / "00000000000000000002-1-0.claim").write_text(json.dumps(
                {"pid": member.pid, "token": "t", "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
            with pytest.raises(MergeAlreadyQueued):
                MergeLock(lock).acquire(max_age_seconds=60, ticket_info=_info("u219-b", "c" * 40, cwd=str(cwd)))
        finally:
            member.kill()
        assert pid_alive(holder.pid), "a batch leader was killed"
    finally:
        holder.kill()


def test_the_recorded_head_is_the_remote_sha_when_the_remote_is_ahead_else_the_local_one(tmp_path):
    from tools.agentops.merge import _queue_head

    cwd, c1, c2 = _repo(tmp_path)
    assert _queue_head(cwd, "u219-b", "origin") == c2  # no remote branch: the local sha
    _git(cwd, "update-ref", "refs/remotes/origin/u219-b", c1)
    assert _queue_head(cwd, "u219-b", "origin") == c2  # the remote is behind: the local sha
    _git(cwd, "checkout", "-q", "main")
    c3 = _commit(cwd, "c")  # the remote branch moves ahead of the (stale) local one
    _git(cwd, "update-ref", "refs/remotes/origin/u219-b", c3)
    _git(cwd, "branch", "-f", "u219-b", c1)
    assert _queue_head(cwd, "u219-b", "origin") == c3  # the run fast-forwards to the remote sha
    assert _queue_head(cwd, "nope", "origin") == ""
