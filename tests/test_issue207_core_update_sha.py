"""#207 unit 1: a core update's sha is known once the home head is read, and an unknown one is not drawn as "?".

Seams (from the issue): `merge-status` (`merge_status.collect` / `render_text`), the merge lock's holder and queue
ticket (`MergeLock`), `_target_lock` and `_take_under_lock` under a real merge lock, and `run_core_update` with a
counting fake home reader. Fixtures are built in tmp_path; no heavy tool, no gh, no git.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tools.agentops import core_update
from tools.agentops import merge_status as ms
from tools.agentops.merge import MergeLock, MergeLockTimeout

SUITE_MIN = 20
HEAD = "abcd1234" + "0" * 32  # the fake home head: its sha8 is "abcd1234"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "var" / "locks" / "merge.lock.queue").mkdir(parents=True)
    (tmp_path / "var" / "locks" / "merge.lock.batch").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "results").mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "orchestration.json").write_text(
        json.dumps({"core_version": "0" * 40, "home": "owner/home"}), encoding="utf-8")
    _log(tmp_path)
    return tmp_path


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _log(root):
    lines = [f"2026-10-17 10:0{i} | orch-{i} | pushed | suite {SUITE_MIN} min, 6 workers | 0 failed | output x"
             for i in range(6)]
    (root / "var" / "agentops" / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _core_ticket(root, info: dict, ns: int = 0) -> None:
    name = f"{ns:020d}-{os.getpid()}-1"
    (root / "var" / "locks" / "merge.lock.queue" / name).write_text(json.dumps(info), encoding="utf-8")


def _holder(root) -> dict:
    return json.loads((root / "var" / "locks" / "merge.lock").read_text(encoding="utf-8"))


class _Stop(Exception):
    """Raised by a fake `_read_and_plan` to end the update right after the observation point."""


def test_running_row_names_the_home_sha_once_the_head_is_read(root, monkeypatch):
    """Row 1: after `_take_under_lock` has read the home head, the holder names its sha8 and `merge-status --json`
    gives `running.kind == "core-update"` with `running.sha == "abcd1234"`."""
    seen = {}

    class FakeReader:
        def head_sha(self):
            return HEAD

    def observe(root_, settings, reader, log, head=None):
        seen["running"] = ms.collect(root_, clock=time.time())["running"]
        seen["holder"] = _holder(root_)
        raise _Stop()

    monkeypatch.setattr(core_update, "_read_and_plan", observe)
    settings = core_update.load_settings(root)
    log = core_update._UpdateLog(root)
    with pytest.raises(_Stop):
        with core_update._target_lock(root, None) as lock:
            core_update._take_under_lock(root, settings, FakeReader(), log, None, lock=lock)

    assert seen["running"]["kind"] == "core-update"
    assert seen["running"]["sha"] == "abcd1234"
    assert seen["running"]["branches"] == []
    assert seen["holder"]["sha"] == "abcd1234"  # on disk while the lock is held


def test_waiting_ticket_with_unknown_sha_has_no_sha_key(root):
    """Row 2: a waiting core-update ticket with no sha has a `waiting` row with no `sha` field, and the text names
    `core update` with no `?`."""
    _core_ticket(root, {"kind": "core-update"}, ns=int((time.time() - 120) * 1e9))
    snap = ms.collect(root, clock=time.time())
    (row,) = snap["waiting"]
    assert row["kind"] == "core-update"
    assert "sha" not in row
    text = ms.render_text(snap)
    assert "waiting 1: core update, queued" in text
    assert "core update ?" not in text


def test_running_holder_without_sha_has_no_sha_key_and_no_question_mark(root):
    """Row 2 (holder side): a core-update holder with no sha gives a running row with no `sha` key; the text is
    `core update` with no `?`."""
    t0 = time.time() - 60
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(t0), "token": "t", "kind": "core-update"}),
        encoding="utf-8")
    snap = ms.collect(root, clock=t0 + 60)
    assert snap["running"]["kind"] == "core-update"
    assert "sha" not in snap["running"]
    assert "core update ?" not in ms.render_text(snap)
    assert "running: core update " in ms.render_text(snap)


def test_target_lock_ticket_has_no_sha_key_until_the_head_is_read(tmp_path, monkeypatch):
    """Row 2 (ticket side): `_target_lock` without a sha hands ``{"kind": "core-update"}`` (no ``?``) to the lock."""
    recorded = []

    from contextlib import contextmanager

    @contextmanager
    def fake_lock(path, *args, **kwargs):
        recorded.append(kwargs)
        yield None, None

    monkeypatch.setattr(core_update, "merge_then_checkout_locked", fake_lock)
    with core_update._target_lock(tmp_path, None):
        pass
    assert recorded[-1]["merge_ticket_info"] == {"kind": "core-update"}


def test_holder_record_omits_sha_when_unknown():
    """Row 2: the holder record names the sha only when it is known."""
    from tools.agentops import merge

    assert merge._holder_record({"kind": "core-update"}) == {"kind": "core-update"}
    assert merge._holder_record({"kind": "core-update", "sha": None}) == {"kind": "core-update"}
    assert merge._holder_record({"kind": "core-update", "sha": "abcd1234"}) == {"kind": "core-update", "sha": "abcd1234"}


def test_set_holder_fields_rewrites_in_place_and_survives_later_rewrites(root):
    """The holder's sha is written under the held lock without a release: pid, started and token stay, and the
    lock object keeps the field so `set_progress` and `heartbeat` keep it on disk."""
    lock = MergeLock(root / "var" / "locks" / "merge.lock")
    assert lock.acquire(ticket_info={"kind": "core-update"}) is None
    try:
        before = _holder(root)
        assert "sha" not in before
        lock.set_holder_fields({"sha": "abcd1234"})
        after = _holder(root)
        assert after["sha"] == "abcd1234"
        assert (after["pid"], after["started"], after["token"]) == (before["pid"], before["started"], before["token"])
        assert lock._holder_fields == {"kind": "core-update", "sha": "abcd1234"}

        progress = root / "var" / "agentops" / "merge_suite" / "out.txt"
        progress.write_text("", encoding="utf-8")
        lock.set_progress(progress)
        assert _holder(root)["sha"] == "abcd1234"
        lock.heartbeat()
        assert _holder(root)["sha"] == "abcd1234"
        assert _holder(root)["kind"] == "core-update"
    finally:
        lock.release()


def test_home_head_is_read_once_per_update_under_the_lock(root, monkeypatch):
    """Row 3 (#76): one update reads the home head once, and that read happens with the merge lock held."""
    reads = []

    class CountingReader:
        def __init__(self, *args, **kwargs):
            pass

        def head_sha(self):
            reads.append(json.loads((root / "var" / "locks" / "merge.lock").read_text(encoding="utf-8")))
            return HEAD

    class Plan:
        touched = frozenset()

    monkeypatch.setattr(core_update, "HomeReader", CountingReader)
    monkeypatch.setattr(core_update, "_is_committable_repo", lambda r: True)
    monkeypatch.setattr(core_update, "_mid_operation", lambda r: None)
    monkeypatch.setattr(core_update, "_read_and_plan", lambda r, s, rd, log, head=None: (head or HEAD, Plan()))
    monkeypatch.setattr(core_update, "_apply_logged",
                        lambda *a, **k: core_update.UpdateResult(message="fake: taken"))

    result = core_update.run_core_update(root, live_check=lambda: False)

    assert result.message == "fake: taken"
    assert len(reads) == 1  # #76: the home is read once
    assert reads[0]["kind"] == "core-update"  # under the lock: the holder file was there
    assert "sha" not in reads[0]  # not yet known when the head is read: the holder is rewritten after it


@pytest.mark.parametrize("exc", [MergeLockTimeout("stayed taken"), OSError("disk busy")])
def test_failed_holder_rewrite_does_not_abort_the_update(root, monkeypatch, exc):
    """The sha rewrite is display only: a MergeLockTimeout or an OSError from it is swallowed and the update
    still reaches its plan (#207: no wait or merge change)."""
    reached = {}

    class FailingLock:
        def set_holder_fields(self, fields):
            raise exc

    class FakeReader:
        def head_sha(self):
            return HEAD

    def observe(root_, settings, reader, log, head=None):
        reached["plan"] = head
        raise _Stop()

    monkeypatch.setattr(core_update, "_read_and_plan", observe)
    settings = core_update.load_settings(root)
    with pytest.raises(_Stop):
        core_update._take_under_lock(root, settings, FakeReader(), core_update._UpdateLog(root), None,
                                     lock=FailingLock())
    assert reached["plan"] == HEAD  # the update went on to its plan
