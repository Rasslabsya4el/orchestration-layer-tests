"""#219 unit 2 (withdraw): `merge withdraw <issue|branch> --why "..."`.

Rows 4 and 5 of the issue, plus: unknown target exits 2, a merge that already ended says
"nothing to withdraw" and posts nothing, a failed brain send is logged and never raised.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import merge_status as ms
from tools.agentops import merge_withdraw as mw
from tools.agentops.merge import MergeLock
from tools.agentops.util import no_window_flags

ROOT = Path(__file__).resolve().parents[1]


class FakeGh:
    def __init__(self):
        self.calls = []

    def issue_status(self, number):
        return SimpleNamespace(open=True)

    def comment(self, number, body):
        self.calls.append(("comment", number, body))

    def add_label(self, number, label):
        self.calls.append(("label", number, label))

    def comments(self):
        return [c for c in self.calls if c[0] == "comment"]


class FakeBrain:
    def __init__(self, send_ok=True):
        self.sent = []
        self.send_ok = send_ok

    def chat(self, issue):
        return 0, "Brain: orch layer"

    def send(self, chat, message):
        self.sent.append((chat, message))
        return (self.send_ok, "ok" if self.send_ok else "boom")


def _lock(tmp_path):
    return tmp_path / "var" / "locks" / "merge.lock"


def _queued(lock, branch, issues, result, pid=None):
    queue = lock.with_name(lock.name + ".queue")
    queue.mkdir(parents=True, exist_ok=True)
    ns = time.time_ns()
    info = {"kind": "merge", "branch": branch, "suite": "s", "remote": "r", "main": "main",
            "cwd": str(lock.parent), "issues": issues, "result": str(result)}
    name = f"{ns:020d}-{pid or os.getpid()}-0"
    (queue / name).write_text(json.dumps(info), encoding="utf-8")
    return queue / name


def _spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], creationflags=no_window_flags())


def _wait_file(path, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.1)
    raise AssertionError(f"{path} never written")


def _alive(pid):
    from tools.agentops.util import pid_alive
    return pid_alive(pid)


def test_row4_withdraw_queued_entry_leaves_queue_status_and_posts_once(tmp_path):
    lock = _lock(tmp_path)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    _queued(lock, "u219-queued", [219], result)
    before = json.dumps(ms.collect(tmp_path))
    assert "u219-queued" in before

    gh, brain = FakeGh(), FakeBrain()
    rc, _ = mw.withdraw("219", why="superseded test", by="owner", lock_path=lock,
                          results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)

    assert rc == 0
    assert "u219-queued" not in json.dumps(ms.collect(tmp_path))  # merge-status no longer lists it
    comments = gh.comments()
    assert len(comments) == 1, comments
    assert comments[0][1] == 219
    assert comments[0][2].split("\n", 1)[0] == "<!-- merge withdrawn -->"
    assert "superseded test" in comments[0][2] and "owner" in comments[0][2]
    assert ("label", 219, "ready-for-agent") in gh.calls  # the ticket returns to ORCH
    assert len(brain.sent) == 1
    assert brain.sent[0] == ("Brain: orch layer", "#219: merge withdrawn by owner: superseded test")
    assert (result.parent / (result.name + ".withdrawn")).exists()  # detach writes "did not land (withdrawn)"


def test_row5_withdraw_running_entry_ends_its_tree_only_and_frees_the_line(tmp_path):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    child_file = tmp_path / "child.pid"
    holder_code = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open(r'{child_file}', 'w').write(str(c.pid))\n"
        "time.sleep(300)\n")
    holder = _spawn(holder_code)
    bystander = _spawn("import time; time.sleep(300)")
    try:
        child_pid = int(_wait_file(child_file))
        lock.write_text(json.dumps({"pid": holder.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "token": "t", "kind": "merge", "branch": "u219-running"}), encoding="utf-8")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": {"kind": "merge", "branch": "u219-running", "issues": [219],
                                                  "result": str(result)}}), encoding="utf-8")
        assert _alive(holder.pid) and _alive(child_pid)

        gh, brain = FakeGh(), FakeBrain()
        rc, _ = mw.withdraw("u219-running", why="stop it", by="owner", lock_path=lock,
                            results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)

        assert rc == 0
        deadline = time.time() + 30
        while time.time() < deadline and (_alive(holder.pid) or _alive(child_pid)):
            time.sleep(0.2)
        assert not _alive(holder.pid), "the holder's own process survived"
        assert not _alive(child_pid), "the holder's child process survived"
        assert _alive(bystander.pid), "a process outside the run's tree was touched"

        started = time.time()
        nxt = MergeLock(lock)
        assert nxt.acquire(max_age_seconds=60) is None  # the line is free for the next entry
        assert time.time() - started < 60
        nxt.release()
        assert len(gh.comments()) == 1
        assert len(brain.sent) == 1
    finally:
        for p in (holder, bystander):
            try:
                p.kill()
            except OSError:
                pass


def test_a_withdrawn_waiter_ends_and_never_takes_the_empty_line(tmp_path):
    import threading
    from tools.agentops.merge import MergeWithdrawn

    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    bystander = _spawn("import time; time.sleep(300)")  # a live holder: the waiter queues behind it
    try:
        lock.write_text(json.dumps({"pid": bystander.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "token": "t", "kind": "merge", "branch": "u219-holder"}), encoding="utf-8")
        result = tmp_path / "results" / "r.json"
        info = {"kind": "merge", "branch": "u219-waiter", "issues": [219], "result": str(result)}
        outcome = {}

        def wait_in_line():
            try:
                MergeLock(lock).acquire(max_age_seconds=600, ticket_info=info)
                outcome["took"] = True
            except MergeWithdrawn as exc:
                outcome["withdrawn"] = str(exc)

        t = threading.Thread(target=wait_in_line, daemon=True)  # a waiter that never ends must not hang pytest's exit
        t.start()
        queue = lock.with_name(lock.name + ".queue")
        deadline = time.time() + 30
        while time.time() < deadline and not any(queue.glob(f"*-{os.getpid()}-*")):
            time.sleep(0.05)
        rc, _ = mw.withdraw("u219-waiter", why="no longer needed", by="owner", lock_path=lock,
                            results_dir=result.parent, gh=FakeGh(), brain_chat=FakeBrain().chat,
                            send=FakeBrain().send)
        t.join(30)
        assert rc == 0
        assert not t.is_alive(), "the withdrawn waiter did not end"
        assert "withdrawn" in outcome and "took" not in outcome
    finally:
        bystander.kill()


def test_a_stale_holder_pid_is_gone_nothing_killed_lock_freed(tmp_path):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    bystander = _spawn("import time; time.sleep(300)")  # alive, but its creation time is not the lock's `started`
    try:
        lock.write_text(json.dumps({"pid": bystander.pid, "started": "2001-01-01 00:00:00",
                                    "token": "t", "kind": "merge", "branch": "u219-stale"}), encoding="utf-8")
        gh, brain = FakeGh(), FakeBrain()
        rc, _ = mw.withdraw("u219-stale", why="x", by="owner", lock_path=lock,
                            results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)
        assert rc == 0
        assert _alive(bystander.pid), "an unrelated process holding a reused pid was killed"
        assert not lock.exists(), "the stale lock was not freed"
        assert gh.calls == [] and brain.sent == []
    finally:
        bystander.kill()


def test_a_queued_member_of_a_batch_is_refused_exit_2_and_nothing_is_touched(tmp_path):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    holder = _spawn("import time; time.sleep(300)")
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        lock.write_text(json.dumps({"pid": holder.pid, "started": started, "token": "tok-leader", "kind": "merge",
                                    "branch": "u219-leader"}), encoding="utf-8")
        ticket = _queued(lock, "u219-member", [219], result)
        batch = lock.with_name(lock.name + ".batch")
        batch.mkdir()
        (batch / f"{ticket.name}.claim").write_text(json.dumps(
            {"pid": holder.pid, "token": "tok-leader", "started": started}), encoding="utf-8")  # the leader claimed it

        gh, brain = FakeGh(), FakeBrain()
        rc, msg = mw.withdraw("u219-member", why="x", by="owner", lock_path=lock,
                              results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)
        assert rc == 2, msg
        assert "member of a batch: withdraw the leader's batch by its branch" in msg
        assert ticket.exists(), "the member's ticket was removed"
        assert _alive(holder.pid)
        assert gh.calls == [] and brain.sent == []
        assert not (result.parent / (result.name + ".withdrawn")).exists()
    finally:
        holder.kill()


def test_a_marker_that_cannot_be_written_exits_1_before_any_kill(tmp_path, monkeypatch):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    (tmp_path / "results").mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the result's folder should be", encoding="utf-8")
    result = blocker / "r.json"  # its folder is a file: the `.withdrawn` marker cannot be written
    holder = _spawn("import time; time.sleep(300)")
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        lock.write_text(json.dumps({"pid": holder.pid, "started": started, "token": "t", "kind": "merge",
                                    "branch": "u219-marker"}), encoding="utf-8")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": {"kind": "merge", "branch": "u219-marker", "issues": [219],
                                                  "result": str(result)}}), encoding="utf-8")
        killed = []
        monkeypatch.setattr(mw, "_kill_tree", lambda pid: killed.append(pid))
        gh, brain = FakeGh(), FakeBrain()
        rc, msg = mw.withdraw("u219-marker", why="x", by="owner", lock_path=lock,
                              results_dir=tmp_path / "results", gh=gh, brain_chat=brain.chat, send=brain.send)
        assert rc == 1
        assert "could not be written" in msg
        assert killed == [], "a process was ended although the marker failed"
        assert _alive(holder.pid) and lock.exists()
        assert gh.calls == [] and brain.sent == []
    finally:
        holder.kill()


def test_an_unparsable_started_omits_the_held_minutes_phrase_never_0_s(tmp_path):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    holder = _spawn("import time; time.sleep(300)")
    try:
        lock.write_text(json.dumps({"pid": holder.pid, "started": "not a timestamp", "token": "t", "kind": "merge",
                                    "branch": "u219-unstamped"}), encoding="utf-8")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": {"kind": "merge", "branch": "u219-unstamped", "issues": [219],
                                                  "result": str(result)}}), encoding="utf-8")
        gh, brain = FakeGh(), FakeBrain()
        rc, _ = mw.withdraw("u219-unstamped", why="stop it", by="owner", lock_path=lock,
                            results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)
        assert rc == 0
        body = gh.comments()[0][2]
        assert "withdrawn by owner: stop it." in body
        assert "held the merge line" not in body
        assert " 0 s" not in body and " 0 min" not in body
    finally:
        holder.kill()


def _killers_that_do_not_end_it():
    def no_op(pid):
        return None

    def timeout(pid):
        raise subprocess.TimeoutExpired(["taskkill", "/PID", str(pid)], 30)

    def os_error(pid):
        raise OSError("taskkill is not there")

    return [no_op, timeout, os_error]


@pytest.mark.parametrize("killer", _killers_that_do_not_end_it())
def test_a_kill_that_does_not_end_the_holder_exits_1_naming_the_pid_and_keeps_the_lock(tmp_path, monkeypatch,
                                                                                    killer):
    lock = _lock(tmp_path)
    lock.parent.mkdir(parents=True)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    holder = _spawn("import time; time.sleep(300)")
    try:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        lock.write_text(json.dumps({"pid": holder.pid, "started": started, "token": "t", "kind": "merge",
                                    "branch": "u219-stuck"}), encoding="utf-8")
        lock.with_name(lock.name + ".holding").write_text(json.dumps(
            {"pid": holder.pid, "ns": 1, "info": {"kind": "merge", "branch": "u219-stuck", "issues": [219],
                                                  "result": str(result)}}), encoding="utf-8")
        killed = []
        monkeypatch.setattr(mw, "_kill_tree", lambda pid: (killed.append(pid), killer(pid)))
        monkeypatch.setattr(mw, "KILL_CONFIRM_SECONDS", 0.5)  # the poll bound, shortened for the test
        gh, brain = FakeGh(), FakeBrain()
        rc, msg = mw.withdraw("u219-stuck", why="stop it", by="owner", lock_path=lock,
                              results_dir=result.parent, gh=gh, brain_chat=brain.chat, send=brain.send)
        assert rc == 1, msg
        assert str(holder.pid) in msg
        assert killed == [holder.pid]
        assert _alive(holder.pid)
        assert lock.exists() and lock.with_name(lock.name + ".holding").exists(), "the lock was removed"
        assert gh.calls == [] and brain.sent == []
    finally:
        holder.kill()


def test_unknown_issue_or_branch_exits_2_with_a_message(tmp_path):
    lock = _lock(tmp_path)
    gh, brain = FakeGh(), FakeBrain()
    rc, msg = mw.withdraw("999", why="x", by="owner", lock_path=lock, results_dir=tmp_path,
                          gh=gh, brain_chat=brain.chat, send=brain.send)
    assert rc == 2
    assert "unknown" in msg
    assert gh.calls == [] and brain.sent == []


def test_withdraw_of_a_merge_that_already_ended_posts_nothing(tmp_path):
    lock = _lock(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    (results / "20260101T000000-merge-u219-old.json").write_text(json.dumps(
        {"kind": "merge", "rc": 0, "reason": "landed", "issues": [219],
         "argv": ["merge", "u219-old", "--issue", "219"]}), encoding="utf-8")
    gh, brain = FakeGh(), FakeBrain()
    rc, msg = mw.withdraw("219", why="x", by="owner", lock_path=lock, results_dir=results,
                          gh=gh, brain_chat=brain.chat, send=brain.send)
    assert rc == 0
    assert msg == "nothing to withdraw"
    assert gh.calls == [] and brain.sent == []


def test_a_failed_brain_send_is_logged_never_raised(tmp_path, capsys):
    lock = _lock(tmp_path)
    result = tmp_path / "results" / "r.json"
    result.parent.mkdir(parents=True)
    _queued(lock, "u219-send", [219], result)
    gh, brain = FakeGh(), FakeBrain(send_ok=False)
    rc, _ = mw.withdraw("219", why="x", by="owner", lock_path=lock, results_dir=result.parent,
                        gh=gh, brain_chat=brain.chat, send=brain.send)
    assert rc == 0
    assert len(gh.comments()) == 1
    assert "boom" in capsys.readouterr().err


def test_withdrawn_and_superseded_merges_count_for_nothing_in_the_loop_stop():
    from tools.agentops.loop_stop import loop_status

    def result(reason):
        return {"body": f"<!-- merge result -->\nMerge of u219 did not land ({reason}) after 1 min.",
                "createdAt": "2026-10-19T10:00:00Z"}

    assert loop_status([result("withdrawn")] * 3).count == 0
    assert loop_status([result("superseded")] * 3).count == 0
    assert loop_status([result("red-on-branch")] * 3).count == 3  # a real failure still counts


def test_cli_unknown_target_exits_2(tmp_path):
    env = {**os.environ, "AGENTOPS_DETACH_DIR": str(tmp_path / "results")}
    proc = subprocess.run(
        [sys.executable, "-m", "tools.agentops", "merge", "withdraw", "999", "--why", "x",
         "--lock", str(_lock(tmp_path))],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=no_window_flags())
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "unknown" in proc.stdout
