"""#299: at most 3 messages to the brain's chat per start in any 30 min, whatever the texts; a wake
that raises is a wake that failed (one "could not tell" line, the tick goes on)."""

from __future__ import annotations

from tests.test_agentops_dispatch_refused_start import _run, _setup
from tests._area_rule import _no_area_rule  # noqa: F401


def _changing(claude, n):
    claude.texts = [f"claude --bg failed: refused, attempt {i}" for i in range(1, n + 1)]


def test_row1_11_changing_texts_three_messages(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    _changing(claude, 11)
    _run(d, clock, 11)
    assert len(claude.waked) == 3
    assert len([l for l in lines if "orch start refused (" in l]) == 11
    assert len([l for l in lines if "kept in this log only" in l]) == 1
    assert claude.attempts == 11


def test_row2_119_changing_texts_six_messages(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    _changing(claude, 119)
    _run(d, clock, 119)
    assert len(claude.waked) == 6
    assert len([l for l in lines if "orch start refused (" in l]) == 119
    assert claude.attempts == 119


def test_row3_wake_raises_is_a_failed_wake(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)

    def raising_wake(target, message):
        claude.waked.append((target, message))
        raise ValueError("wake blew up")

    claude.wake = raising_wake
    writes = []
    real_write = d._write_state

    def counting_write():
        writes.append(1)
        real_write()

    d._write_state = counting_write
    _run(d, clock, 5)
    assert len([l for l in lines if "could not tell" in l and "wake blew up" in l]) == 1
    assert [l for l in lines if "tick failed" in l] == []
    assert claude.attempts == 5
    assert len(writes) >= 5
    assert (tmp_path / "orch_session.json").is_file()
