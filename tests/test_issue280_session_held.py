"""#280 unit 3 (session-retry), real files on Windows: a reader holds `orch_session.json` open with a plain
Python `open` (no delete sharing, so the replace is refused with "Access is denied", as the dispatcher's
log showed).

Row 4: a reader that lets go after 120 ms: the write succeeds, the file holds the new content, and no
"could not write" line is logged. Held through all retries (the sleep is a no-op, so the reader holds the
file for the whole call): exactly one line naming `replace`.

Windows only; listed in tests/pc_only.txt. The retry arithmetic is in tests/test_issue280_session_write.py.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from tools.agentops import dispatch
from tools.agentops.dispatch import Dispatcher

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tests._area_rule import _no_area_rule  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="a plain open refuses a replace on Windows only (#280)")


def _dispatcher(tmp_path: Path, lines: list[str]) -> Dispatcher:
    return Dispatcher(claude=FakeClaude(), gh=FakeGh(), events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      finishing_dir=tmp_path / "finishing", clock=FakeClock(), log=lines.append)


def test_reader_lets_go_after_120_ms_write_succeeds_no_line(tmp_path):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    state = tmp_path / "orch_session.json"
    state.write_text(json.dumps({"old": True}), encoding="utf-8")
    reader = open(state, "rb")  # a plain open: no delete sharing, so the replace is refused
    release = threading.Timer(0.12, reader.close)
    release.start()
    try:
        d._write_state()
    finally:
        release.join(timeout=2)
        if not reader.closed:
            reader.close()
    assert lines == []
    data = json.loads(state.read_text(encoding="utf-8"))
    assert "old" not in data
    assert data["sessions"] == []


def test_reader_holds_through_all_retries_one_replace_line(tmp_path):
    lines: list[str] = []
    d = _dispatcher(tmp_path, lines)
    state = tmp_path / "orch_session.json"
    state.write_text(json.dumps({"old": True}), encoding="utf-8")
    reader = open(state, "rb")
    waits: list[float] = []
    try:
        d._write_state(sleep=lambda s: waits.append(s))
    finally:
        reader.close()
    assert len(waits) == dispatch.STATE_REPLACE_RETRIES
    assert len(lines) == 1
    assert "step replace" in lines[0]
    assert "could not write orch_session.json" in lines[0]
    assert json.loads(state.read_text(encoding="utf-8")) == {"old": True}
