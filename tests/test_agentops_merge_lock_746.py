"""#746: a holder's ticket that could not be unlinked once (Windows PermissionError: a waiter has the
file open at that moment) must not keep every younger waiter out of the lock after the release."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from tools.agentops.merge import MergeLock


def test_a_holders_ticket_that_resists_one_unlink_does_not_block_the_next_waiter(tmp_path, monkeypatch):
    lock_path = tmp_path / "merge.lock"
    queue_dir = tmp_path / "merge.lock.queue"
    real_unlink = Path.unlink
    refused: list[str] = []

    def unlink_once_refused(self, *a, **kw):
        if self.parent == queue_dir and not refused:
            refused.append(self.name)
            raise PermissionError(13, "the file is open elsewhere", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink_once_refused)
    holder = MergeLock(lock_path)
    holder.acquire(max_age_seconds=60)  # one pid for both: the holder's own ticket is the one refused once
    assert len(refused) == 1

    got: dict[str, float] = {}

    def waiter():
        w = MergeLock(lock_path)
        w.acquire(max_age_seconds=60)
        got["at"] = time.monotonic()
        w.release()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.5)  # the waiter is queued and waiting on the held lock
    assert "at" not in got
    released = time.monotonic()
    holder.release()
    t.join(timeout=10)
    assert "at" in got, "the waiter never took the lock after the holder released it"
    assert got["at"] - released < 5
