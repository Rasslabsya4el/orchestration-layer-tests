"""#340 unit 7: web/monitor/tab-merges.js renders a fixture (tests/fixtures/monitor/github.json) through node.

The checks run in tests/issue340_tab_merges_check.mjs (node 24, no DOM). Without node the tests skip.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "issue340_tab_merges_check.mjs"
FIXTURE = ROOT / "tests" / "fixtures" / "monitor" / "github.json"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the merges tab is rendered by node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_tab_contract(report):
    assert report["id"] == "merges"
    assert report["title"] == "Слияния"
    assert report["order"] == 50
    assert report["endpoints"] == ["github"]


def test_every_merge_row_of_the_fixture_shows_its_place(report):
    n = report["merges_expected"]
    print(f"\nmerge rows with a place: {report['rows_with_place']} of {n}")
    assert n > 0
    assert report["rows"] == n
    assert report["rows_with_place"] == n


def test_a_bare_pc_place_is_not_accepted(report):
    """A PC place without its reason (`PC`) fails the place check: the writers always print the reason."""
    assert report["bare_pc_rejected"] is True


def test_machines_and_minutes_bars_are_drawn(report):
    assert report["machines_shown"] is True
    assert report["minutes_bars"] == report["minutes_expected"] > 0


def test_an_error_text_is_shown(report):
    assert report["error_shown"] is True


def test_fixture_is_small_and_has_the_envelope_shape():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert {"at_ms", "refresh_s", "data"} <= set(doc)
    assert len(FIXTURE.read_bytes()) < 4096
