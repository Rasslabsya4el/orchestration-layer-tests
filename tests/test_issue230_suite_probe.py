"""#230: the core ships a general suite-worker probe, so a repo with no ``merge_suite_workers_probe`` key
still runs its merge suite with ``-n k`` (k from measured free cores and free RAM, cap 6). A repo's own
probe string wins; an explicit empty value turns the probe off.

Seams: ``suite_probe.workers_line`` (pure), ``merge.resolve_suite_workers_probe`` (settings),
``suite_probe.main`` (one real run of the module), ``merge._with_workers`` (the count it puts in the command).
Starts no heavy tool.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.agentops import core_settings, merge, suite_probe
from tools.agentops.merge import suite_workers

WORKTREE = Path(__file__).resolve().parent.parent
CMD = 'python -m pytest tests -q -m "not slow" -rf'


def test_eight_cores_sixteen_gb_quarter_busy_gives_six_workers():
    n, line = suite_probe.workers_line(8, 25, 16)
    assert n == 6
    assert line == ("workers 6: cores free 6.0 of 8 (cpu 25%), 0.5 core a worker -> 12 by CPU, "
                    "RAM free 16.0 GB -> 32 by RAM, cap 6")
    assert 2 <= n <= 6
    assert suite_workers(lambda: line) == (6, line)
    assert merge._CAP_RE.search(line).group(1) == "6"


def test_tight_ram_gives_one_worker():
    # 3.5 GB free: (3.5 - 3.0) / 0.4 = 1.25 -> 1 by RAM (4 GB free would give 2 by the formula)
    n, line = suite_probe.workers_line(8, 25, 3.5)
    assert n == 1
    assert line == ("workers 1: cores free 6.0 of 8 (cpu 25%), 0.5 core a worker -> 12 by CPU, "
                    "RAM free 3.5 GB -> 1 by RAM, cap 6")


def test_no_cpu_reading_counts_by_ram_only():
    n, line = suite_probe.workers_line(8, None, 16)
    assert n == 6
    assert line == "workers 6: cpu n/a, RAM free 16.0 GB -> 32 by RAM, cap 6"


def test_no_ram_reading_is_one_worker():
    n, line = suite_probe.workers_line(8, 25, None)
    assert n == 1
    assert line == "workers 1: RAM n/a -> 1 (reader failed), cap 6"


def test_cpu_fully_busy_is_one_worker():
    n, line = suite_probe.workers_line(8, 100, 16)
    assert n == 1
    assert line.startswith("workers 1: cores free 0.0 of 8 (cpu 100%)")


def test_resolve_absent_key_is_the_core_probe(monkeypatch):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {})
    assert merge.resolve_suite_workers_probe() == suite_probe.CORE_PROBE_COMMAND
    assert suite_probe.CORE_PROBE_COMMAND == f'"{sys.executable}" -m tools.agentops.suite_probe'


def test_resolve_repo_probe_wins(monkeypatch):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"merge_suite_workers_probe": "my-probe"})
    assert merge.resolve_suite_workers_probe() == "my-probe"


def test_resolve_empty_string_is_off(monkeypatch):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"merge_suite_workers_probe": ""})
    assert merge.resolve_suite_workers_probe() is None


def test_main_real_run_prints_the_line():
    result = subprocess.run([sys.executable, "-m", "tools.agentops.suite_probe"], cwd=str(WORKTREE),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    last = [ln for ln in result.stdout.splitlines() if ln.strip()][-1]
    assert merge._WORKERS_LINE_RE.match(last), last
    assert last.endswith("cap 6"), last
    print(f"\nprobe line: {last}")


def test_with_workers_puts_the_probe_count_into_the_command(monkeypatch, tmp_path):
    fake = "workers 4: cores free 6.0 of 8 (cpu 25%), 0.5 core a worker -> 12 by CPU, RAM free 16.0 GB -> 32 by RAM, cap 6"
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {})  # the real settings path, key absent
    built: list[str] = []

    def fake_make_probe(template, cwd):
        built.append(template)
        return lambda: fake

    monkeypatch.setattr(merge, "_make_probe", fake_make_probe)
    monkeypatch.setattr(merge, "_xdist_available", lambda interp, cwd: True)
    cmd, k, head = merge._with_workers(CMD, True, tmp_path)
    assert built == [suite_probe.CORE_PROBE_COMMAND]
    assert k == 4 and cmd == CMD + " -n 4 --maxschedchunk=1"
    assert head == fake
