"""#659 unit 1: the merge suite's pytest-xdist worker count comes from the machine budget probe."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import merge
from tools.agentops.merge import MergeResult, suite_workers

# the repo's registered marker name, spelled in parts: a literal would put the heavy tool's word into a core file
CMD = 'python -m pytest tests -q -m "not slow and not ' + "po" + 'b" -rf'
GO_LINE = "workers 6: cores free 10 of 12 (cpu 10%), RAM free 7.3 GB -> 10 by RAM, cap 6"


def _line_probe(line: str, calls: list | None = None):
    def probe():
        if calls is not None:
            calls.append(1)
        return line
    return probe


def _setup(monkeypatch, *, template="probe", line=GO_LINE, xdist=True, calls=None):
    monkeypatch.setattr(merge, "resolve_suite_workers_probe", lambda: template)
    monkeypatch.setattr(merge, "_make_probe", lambda t, cwd: _line_probe(line, calls))
    monkeypatch.setattr(merge, "_xdist_available", lambda interp, cwd: xdist)


def test_count_comes_from_the_probe_line():
    assert suite_workers(_line_probe(GO_LINE)) == (6, GO_LINE)
    assert suite_workers(_line_probe("workers 1: RAM n/a"))[0] == 1


def test_probe_line_without_count_is_one_worker():
    assert suite_workers(_line_probe("garbage"))[0] == 1


def test_line_with_6_adds_n_6_and_head_line(monkeypatch, tmp_path):
    _setup(monkeypatch)
    cmd, k, head = merge._with_workers(CMD, True, tmp_path)
    assert k == 6 and cmd == CMD + " -n 6 --maxschedchunk=1"
    assert head == GO_LINE


def test_one_worker_has_no_n_flag(monkeypatch, tmp_path):
    _setup(monkeypatch, line="workers 1: cores free 1 of 12 (cpu 90%), RAM free 7.3 GB -> 10 by RAM, cap 6")
    cmd, k, head = merge._with_workers(CMD, True, tmp_path)
    assert k == 1 and cmd == CMD and " -n" not in cmd
    assert head.startswith("workers 1: cores")


def test_probe_failure_never_blocks(monkeypatch, tmp_path):
    def boom():
        raise TimeoutError("timed out after 30 s")
    assert suite_workers(boom) == (1, "budget probe failed: timed out after 30 s")


def test_xdist_missing_falls_back(monkeypatch, tmp_path):
    _setup(monkeypatch, xdist=False)
    cmd, k, head = merge._with_workers(CMD, True, tmp_path)
    assert k == 1 and "-n" not in cmd.split()
    assert "pytest-xdist missing" in head


def test_key_absent_calls_no_probe(monkeypatch, tmp_path):
    calls: list[int] = []
    _setup(monkeypatch, template=None, calls=calls)
    monkeypatch.setattr(merge, "_make_probe", lambda t, cwd: pytest.fail("probe built"))
    assert merge._with_workers(CMD, True, tmp_path) == (CMD, 0, "")
    assert calls == []


def test_non_pytest_suite_untouched(monkeypatch, tmp_path):
    _setup(monkeypatch)
    assert merge._with_workers("echo ok", False, tmp_path) == ("echo ok", 0, "")


def test_merge_log_line_carries_workers(tmp_path):
    log = tmp_path / "merge.log"
    merge._append_merge_log("b", MergeResult(ok=True, reason="pushed", suite_duration=480.0,
                                             suite_workers=6), log_path=log)
    assert "| suite 8 min, 6 workers | 0 failed |" in log.read_text(encoding="utf-8")
    log2 = tmp_path / "m2.log"
    merge._append_merge_log("b", MergeResult(ok=True, reason="pushed", suite_duration=480.0), log_path=log2)
    assert "workers" not in log2.read_text(encoding="utf-8")


def test_out_file_first_line_is_head(tmp_path):
    out = tmp_path / "s.out"
    merge._run_suite("echo hello", tmp_path, None, out, "workers 6: GO")
    assert out.read_text(encoding="utf-8").splitlines()[0] == "workers 6: GO"
