"""#731: a lock waiter counts an unreadable holder by its own clock (stale after the grace,
the ceiling raises MergeLockTimeout naming the unreadable file)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import MergeLock, MergeLockTimeout

GRACE, REPORT, CEILING = 0.5, 0.3, 1.5


@pytest.fixture(autouse=True)
def _small_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_module, "UNPARSABLE_GRACE_SECONDS", GRACE)
    monkeypatch.setattr(merge_module, "REPORT_INTERVAL_SECONDS", REPORT)


def _run(lock: MergeLock, ceiling: float, limit: float) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def target() -> None:
        try:
            out["result"] = lock.acquire(max_age_seconds=ceiling)
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=limit)
    return thread, out


def test_row1_unparsable_file_with_failing_stat_is_removed_or_times_out(tmp_path: Path, monkeypatch):
    path = tmp_path / "merge.lock"
    path.write_text("", encoding="utf-8")
    lock = MergeLock(path)
    monkeypatch.setattr(lock, "_file_age", lambda: None)
    thread, out = _run(lock, CEILING, GRACE + 5.0)
    assert not thread.is_alive(), "the waiter waits for ever on an unreadable holder"
    if "exc" in out:
        assert isinstance(out["exc"], MergeLockTimeout)
    else:
        assert lock._own and json.loads(path.read_text(encoding="utf-8"))["token"] == lock.token
        lock.release()


def test_row2_name_never_frees_raises_at_the_ceiling_naming_the_unreadable_file(
        tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "merge.lock"
    path.write_text("", encoding="utf-8")
    lock = MergeLock(path)
    monkeypatch.setattr(lock, "_file_age", lambda: None)
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == path:
            raise PermissionError("pending delete")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    t0 = time.time()
    thread, out = _run(lock, CEILING, CEILING + 5.0)
    assert not thread.is_alive()
    exc = out.get("exc")
    assert isinstance(exc, MergeLockTimeout), out
    assert "unreadable" in str(exc) and "pid None" not in str(exc)
    assert exc.pid is None and exc.silent is None and exc.held >= CEILING
    assert time.time() - t0 < CEILING + 5.0
    assert capsys.readouterr().out.count("pid None, age 0 s") <= 1


def test_row3_a_young_file_written_valid_later_is_waited_on_not_removed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(merge_module, "UNPARSABLE_GRACE_SECONDS", 3.0)
    path = tmp_path / "merge.lock"
    path.write_text("", encoding="utf-8")
    lock = MergeLock(path)
    valid = json.dumps({"pid": __import__("os").getpid(), "started": merge_module.now(), "token": "other"})
    timer = threading.Timer(0.5, lambda: path.write_text(valid, encoding="utf-8"))
    timer.start()
    thread, out = _run(lock, 2.0, 1.5)
    # still waiting on the valid, live holder after the grace would have fired had it been counted
    assert thread.is_alive() or isinstance(out.get("exc"), MergeLockTimeout)
    timer.join()
    assert path.exists() and path.read_text(encoding="utf-8") == valid
    assert not lock._own
    thread.join(timeout=10)
