"""#757: a suite with no progress mark yet prints 0% once, never None."""
from __future__ import annotations

import time

from tests.test_agentops_merge_status import LINE, _log, _running_lock, root  # noqa: F401
from tools.agentops import merge_status as ms


def _text(root, out_text):
    t0 = int(time.time()) - 30
    _running_lock(root, t0, out_text)
    snap = ms.collect(root, clock=t0 + 30)
    return snap, ms.render_text(snap)


def test_row1_head_line_only_prints_zero_percent_once(root):
    _log(root)
    snap, text = _text(root, "bringing up nodes...\n")
    assert snap["running"]["percent"] is None
    assert "0%" in text and "None" not in text
    assert text.count("0%") == 1


def test_row2_forty_percent_sixteen_of_forty_unchanged(root):
    _log(root)
    _, text = _text(root, f"selected 40 of 4382 (map x)\n{LINE} [ 40%]\n")
    assert ", 40%, 16/40 tests, 0 failed," in text
