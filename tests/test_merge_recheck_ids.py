"""#189 unit 1: the recheck of failed test ids on a tree names a test by its id whole, whatever characters it holds.

Seam: ``merge._recheck_failed`` on a tmp dir (a real tiny pytest file where a test must run), with the run helper
wrapped to count the pytest processes it starts. Rows 1, 2 and 4 of the issue."""
from __future__ import annotations

import sys
import textwrap

import pytest

from tools.agentops import merge

SUITE_CMD = f'"{sys.executable}" -m pytest tests'
INCIDENT_ID = 'tests/test_collect_tickets_delta.py::test_a_missing_or_unreadable_snapshot_is_read_in_full[{"full_at": 1, "nodes": 3}]'


@pytest.fixture
def runs(monkeypatch):
    """The commands the recheck started, in order."""
    started: list[str] = []
    real = merge._run_suite

    def counting(cmd, *args, **kwargs):
        started.append(cmd)
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(merge, "_run_suite", counting)
    return started


def test_ids_of_a_file_absent_on_the_tree_are_not_failing_and_start_no_pytest(tmp_path, runs):
    """Row 1 and row 4: the incident's 18 ids, the file is not in the checked tree."""
    (tmp_path / "tests").mkdir()
    ids = [f"tests/test_collect_tickets_delta.py::test_plain_{n}" for n in range(17)] + [INCIDENT_ID]
    still, _output = merge._recheck_failed(SUITE_CMD, ids, tmp_path, None, None)
    assert still == []
    assert len(runs) <= 2


# the five characters the incident's id taught: one id each, in the parameter part
ODD_PARAMS = ['say "hi"', "with space", "open[bracket", "brace{x}", "colon:inside"]


def _write_suite(tmp_path, body_passes: bool):
    (tmp_path / "test_odd.py").write_text(textwrap.dedent(f"""\
        import pytest

        @pytest.mark.parametrize("v", [pytest.param(1, id=p) for p in {ODD_PARAMS!r}])
        def test_odd(v):
            assert {body_passes}
        """), encoding="utf-8")
    return [f"test_odd.py::test_odd[{p}]" for p in ODD_PARAMS]


def test_odd_ids_present_and_failing_are_returned_whole(tmp_path, runs):
    ids = _write_suite(tmp_path, body_passes=False)
    still, _output = merge._recheck_failed(SUITE_CMD, ids, tmp_path, None, None)
    assert sorted(still) == sorted(ids)
    assert len(runs) <= 2


def test_odd_ids_present_and_passing_are_not_returned(tmp_path, runs):
    ids = _write_suite(tmp_path, body_passes=True)
    still, _output = merge._recheck_failed(SUITE_CMD, ids, tmp_path, None, None)
    assert still == []


def test_an_id_pytest_cannot_find_is_dropped_by_its_whole_text(tmp_path, runs):
    """The file is there, the test function is gone: pytest says "not found" and the id (with its spaces) goes."""
    ids = _write_suite(tmp_path, body_passes=False)
    gone = 'test_odd.py::test_gone[a "b" c]'
    still, _output = merge._recheck_failed(SUITE_CMD, ids + [gone], tmp_path, None, None)
    assert sorted(still) == sorted(ids)


@pytest.mark.parametrize("says", ["", "print('boom')"])
def test_a_red_run_that_names_no_id_is_could_not_tell_not_a_failure_of_every_id(tmp_path, runs, says):
    """Rule 3: a red exit with no FAILED line (or no output at all) is not "all ids still fail"."""
    (tmp_path / "test_x.py").write_text("", encoding="utf-8")
    stub = tmp_path / "stub.py"
    stub.write_text(chr(10).join(["import sys", says, "sys.exit(1)", ""]), encoding="utf-8")
    with pytest.raises(merge.RecheckInconclusive):
        merge._recheck_failed(f'"{sys.executable}" "{stub}"', ["test_x.py::test_x"], tmp_path, None, None)


SPACE_ID = 'test_odd.py::test_odd[{"a": 1, "b": 2}]'


def test_failed_ids_of_a_summary_keep_spaces_quotes_and_brackets():
    """Rule 2 end to end, the reading side: an id is the text up to ` - <message>` or the end of the line."""
    lines = ["=== short test summary info ===",
             'FAILED tests/x.py::t[{"a": 1, "b": 2}] - assert 0',
             "FAILED tests/x.py::plain - assert 0",
             "FAILED tests/x.py::bare",
             "3 failed in 1s"]
    output = chr(10).join(lines) + chr(10)
    want = ['tests/x.py::t[{"a": 1, "b": 2}]', "tests/x.py::plain", "tests/x.py::bare"]
    assert merge._failed_ids(output) == want
    assert merge._parse_pytest_failures(output) == (3, want)


def test_ids_read_from_a_real_red_output_are_rechecked_still_failing_never_green(tmp_path, runs):
    """The first-recheck path of a merge: ids come FROM the suite output, one holds a space and is present."""
    (tmp_path / "test_odd.py").write_text(textwrap.dedent("""\
        import pytest

        @pytest.mark.parametrize("v", [pytest.param(1, id='{"a": 1, "b": 2}'), pytest.param(2, id="plain")])
        def test_odd(v):
            assert False
        """), encoding="utf-8")
    ok, first = merge._run_suite(f'"{sys.executable}" -m pytest -q -p no:faulthandler -rf', tmp_path)
    assert not ok
    red_ids = merge._failed_ids(first)
    assert SPACE_ID in red_ids and len(red_ids) == 2
    still, _output = merge._recheck_failed(SUITE_CMD, red_ids, tmp_path, None, None)
    assert sorted(still) == sorted(red_ids)
