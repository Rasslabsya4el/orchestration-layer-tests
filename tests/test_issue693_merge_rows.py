"""#693: merge-status rows are one per branch run of attempts; no heavy tool."""

from __future__ import annotations

import json

from tools.agentops import merge_status as ms

ARGV = ["python", "-m", "tools.agentops", "merge", "orch-685-fix", "--issue", "685"]


def _put(d, stamp, rc, reason, started, ended):
    rec = {"kind": "merge", "argv": ARGV, "rc": rc, "reason": reason, "started": started,
           "ended": ended, "issues": [685]}
    (d / f"{stamp}-merge-orch-685-fix.json").write_text(json.dumps(rec), encoding="utf-8")


def _three(d):
    _put(d, "20261009T230349", 1, "red-suite", "2026-10-09T23:03:49", "2026-10-09T23:12:47")
    _put(d, "20261009T231303", 0, "pushed", "2026-10-09T23:13:03", "2026-10-09T23:13:23")
    _put(d, "20261009T231312", 5, "already-queued", "2026-10-09T23:13:12", "2026-10-09T23:13:12")


def test_three_files_one_row(tmp_path):
    _three(tmp_path)
    rows = ms._landed(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert (r["branch"], r["rc"], r["attempts"], r["failed_before"]) == ("orch-685-fix", 0, 2, 1)


def test_only_failed_file(tmp_path):
    _put(tmp_path, "20261009T230349", 1, "red-suite", "2026-10-09T23:03:49", "2026-10-09T23:12:47")
    rows = ms._landed(tmp_path)
    assert len(rows) == 1
    assert (rows[0]["rc"], rows[0]["attempts"], rows[0]["failed_before"]) == (1, 1, 0)


def test_already_queued_alone_no_rows(tmp_path):
    _put(tmp_path, "20261009T231312", 5, "already-queued", "2026-10-09T23:13:12", "2026-10-09T23:13:12")
    assert ms._landed(tmp_path) == []


def test_landing_breaks_the_run(tmp_path):
    _three(tmp_path)
    _put(tmp_path, "20261010T010000", 1, "red-suite", "2026-10-10T01:00:00", "2026-10-10T01:05:00")
    rows = ms._landed(tmp_path)
    assert [(r["rc"], r["attempts"], r["failed_before"]) for r in rows] == [(1, 1, 0), (0, 2, 1)]
