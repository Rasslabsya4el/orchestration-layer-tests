"""#764: a failed merge is `did not land` with its reason; the full run's time counts from its own commit's start."""
from __future__ import annotations

import json

from tools.agentops import merge_status as ms

T0 = 1_800_000_000.0


def _result(root, name, rc, reason):
    d = root / "var" / "agentops" / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"x-merge-{name}.json").write_text(json.dumps({
        "started": "2026-10-10T13:00:00", "ended": "2026-10-10T13:30:00", "rc": rc, "reason": reason,
        "argv": ["a", "b", "c", "d", name]}), encoding="utf-8")


def _rows(root):
    return ms.render_text({"running": None, "full_run": None, "waiting": [],
                           "landed": ms.collect(root, clock=T0)["landed"]}).splitlines()[1:]


def test_row1_red_suite_is_did_not_land(tmp_path):
    _result(tmp_path, "orch-758", 1, "red-suite")
    rows = _rows(tmp_path)
    assert rows[0].startswith("did not land:") and "red-suite" in rows[0]
    assert not [r for r in rows if r.startswith("landed:") and "rc 1" in r]
    assert ms.collect(tmp_path, clock=T0)["landed"][0]["reason"] == "red-suite"


def test_row2_rc0_stays_landed(tmp_path):
    _result(tmp_path, "orch-1", 0, None)
    rows = _rows(tmp_path)
    assert rows[0].startswith("landed: orch-1") and rows[0].endswith("rc 0")


def test_row3_running_counts_from_the_commit_run(tmp_path, monkeypatch):
    base = tmp_path / "var" / "agentops"
    (base / "full_run").mkdir(parents=True)
    out = base / "full_run" / "94432a65.out"
    out.write_text("........ [ 50%]\n", encoding="utf-8")
    (base / "full_run.lock").write_text(json.dumps({"pid": 1, "started": ms._iso(T0 - 80 * 60)}), encoding="utf-8")
    monkeypatch.setattr(ms, "pid_alive", lambda pid: True)
    monkeypatch.setattr(ms, "_birth", lambda p: T0 - 16 * 60)
    import os
    os.utime(out, (T0, T0))
    f = ms.collect(tmp_path, clock=T0)["full_run"]
    text = ms.render_text({"running": None, "full_run": f, "waiting": [], "landed": []})
    assert "running 16 min" in text
