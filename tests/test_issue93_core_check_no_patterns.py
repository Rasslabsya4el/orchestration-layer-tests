"""Issue #93 unit 1: a leak check with no config/core_leak_patterns.json says so in one line and shows '-' in the
L1-L3 columns, instead of zeros that read as a clean check. Planted temp trees only; the one CLI test reads the
repo's own tree read-only and never writes to it."""

from __future__ import annotations

import json
from functools import partial

from tools.agentops import core_check
from tools.agentops.core_check import (DEFAULT_ROOT, NOT_CHECKED_LINE, format_report, main, patterns_file_exists,
                                       run_check, tracked_under_shared)

FILES = {"docs/core.md": "# core\nuse the Widget here\n", "core/a.py": "X = 1\n"}


def _rows(tmp_path, patterns_text=None):
    root = tmp_path / "root"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    manifest = tmp_path / "m.txt"
    manifest.write_text("\n".join(FILES) + "\n", encoding="utf-8")
    patterns = tmp_path / "core_leak_patterns.json"
    if patterns_text is not None:
        patterns.write_text(patterns_text, encoding="utf-8")
    rows, ok = run_check(root=root, manifest_path=manifest, patterns_path=patterns)
    return rows, ok, patterns


def _cells(line):
    """The columns of format_report: path 55, lines 7, then L1 L2 L3 H I of 5 each."""
    return {"L1": line[62:67].strip(), "L2": line[67:72].strip(), "L3": line[72:77].strip(),
            "H": line[77:82].strip(), "I": line[82:87].strip()}


def _line(report, start):
    return next(line for line in report.splitlines() if line.startswith(start))


# -- no settings file: one line, dashes, the same rc --------------------------------------------------

def test_1_no_patterns_file_prints_one_not_checked_line_and_dashes_in_l1_to_l3(tmp_path):
    rows, _ok, patterns = _rows(tmp_path)
    assert not patterns.exists()
    report = format_report(rows, patterns_checked=patterns_file_exists(patterns))
    assert report.splitlines()[-1] == NOT_CHECKED_LINE == "L1-L3 not checked: no config/core_leak_patterns.json"
    assert report.splitlines()[0].split()[:2] == ["path", "lines"]  # the header stays the first line
    assert sum("not checked" in line for line in report.splitlines()) == 1
    for start in ("docs/core.md", "core/a.py", "total"):
        cells = _cells(_line(report, start))
        assert cells["L1"] == cells["L2"] == cells["L3"] == "-", start
        assert cells["H"] == "0" and cells["I"] == "0", start


def test_2_no_patterns_file_keeps_the_result_as_it_was(tmp_path):
    rows, ok, _patterns = _rows(tmp_path)
    assert ok is True  # L1-L3 are not judged without the file; H and I still are, and the tree has neither


# -- with the file: no line, counts as before ---------------------------------------------------------

def test_3_with_the_file_the_line_is_absent_and_the_counts_are_as_before(tmp_path):
    rows, ok, patterns = _rows(tmp_path, patterns_text=json.dumps({"L1": "Widget"}))
    assert patterns_file_exists(patterns) is True
    report = format_report(rows, patterns_checked=patterns_file_exists(patterns))
    assert "not checked" not in report
    assert _cells(_line(report, "docs/core.md"))["L1"] == "1"
    assert _cells(_line(report, "core/a.py"))["L1"] == "0"
    total = _cells(_line(report, "total"))
    assert total["L1"] == "1" and total["L2"] == "0" and total["L3"] == "0"
    assert ok is False


def test_4_format_report_default_is_the_checked_report(tmp_path):
    rows, _ok, _patterns = _rows(tmp_path)  # no file, but the caller says the check ran
    report = format_report(rows)
    assert "not checked" not in report
    assert _cells(_line(report, "total"))["L1"] == "0"


# -- the CLI path: `core check` reads the settings file it is given -----------------------------------

def _patch_run_check(monkeypatch, patterns_path):
    """Isolate the CLI the way tests/test_issue1096_core_check_imports.py does: run_check reads ``patterns_path``."""
    monkeypatch.setattr(core_check, "run_check", partial(core_check.run_check, patterns_path=patterns_path))


def test_5_core_check_cli_without_the_file_says_so_and_keeps_its_rc(tmp_path, monkeypatch, capsys):
    absent = tmp_path / "core_leak_patterns.json"
    monkeypatch.setattr(core_check, "PATTERNS_PATH", absent)
    _patch_run_check(monkeypatch, absent)
    _rows_real, ok = core_check.run_check(root=DEFAULT_ROOT, patterns_path=absent)
    expected_rc = 0 if ok and not tracked_under_shared(DEFAULT_ROOT) else 1
    rc = main(["core", "check"])
    out = capsys.readouterr().out.splitlines()
    assert NOT_CHECKED_LINE in out and sum("not checked" in line for line in out) == 1
    assert out[0].startswith("path")
    assert _cells(next(line for line in out if line.startswith("total")))["L3"] == "-"
    assert rc == expected_rc


def test_6_core_check_cli_with_the_file_has_no_line_and_numbers_in_l1_to_l3(tmp_path, monkeypatch, capsys):
    present = tmp_path / "core_leak_patterns.json"
    present.write_text(json.dumps({"L1": "zz" + "qq-never-in-any-file"}), encoding="utf-8")  # spelled in parts: no file holds it
    monkeypatch.setattr(core_check, "PATTERNS_PATH", present)
    _patch_run_check(monkeypatch, present)
    main(["core", "check"])
    out = capsys.readouterr().out
    assert "not checked" not in out
    total = _cells(next(line for line in out.splitlines() if line.startswith("total")))
    assert total["L1"] == "0" and total["L2"] == "0" and total["L3"] == "0"


def test_7_patched_absent_run_check_reads_no_real_patterns_even_where_the_real_file_exists(tmp_path, monkeypatch,
                                                                                         capsys):
    """A target holding the real file: the test-style patch (run_check reads an absent file) still isolates the CLI,
    so the planted 'Widget' is not judged by the real L1 pattern."""
    real = tmp_path / "real_core_leak_patterns.json"
    real.write_text(json.dumps({"L1": "Widget"}), encoding="utf-8")
    monkeypatch.setattr(core_check, "PATTERNS_PATH", real)  # the target's own file exists
    root = tmp_path / "root"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    manifest = tmp_path / "cli_manifest.txt"
    manifest.write_text("\n".join(FILES) + "\n", encoding="utf-8")
    monkeypatch.setattr(core_check, "run_check", partial(core_check.run_check, manifest_path=manifest,
                                                         patterns_path=tmp_path / "absent.json"))
    rc = main(["core", "check", "--root", str(root)])
    out = capsys.readouterr().out
    assert _cells(_line(out, "docs/core.md"))["L1"] == "0"
    assert _cells(_line(out, "total"))["L1"] == "0"
    assert rc == 0
