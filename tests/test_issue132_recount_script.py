"""#132 unit 1: the `script:` line of a `## Recount` block and `recount --run`.

The seams are the lister's candidates, `format_candidates` (the printed lines) and the CLI. The `gh` call is a fake
as in test_agentops_recount_1108.py. The count scripts are real files under a tmp repo root that the tests pass in,
run with the same `python -I` argv the command builds.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from tools.agentops import recount
from tools.agentops.cli import main

SINCE = date(2026, 10, 1)
PROSE = "one line per rule the brain posted this week, counted by hand from the logs"


class _FakeGh:
    """Stands in for `run_gh`: answers with one fixed JSON list."""

    def __init__(self, reply: object) -> None:
        self.reply = reply

    def __call__(self, args, **kwargs):
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(self.reply), "")


def _block(count_line: str) -> str:
    return f"## Recount\n- incident: a rule posted without a count\n- how counted: `grep -c` over the logs\n{count_line}"


def _issue(body: str) -> dict:
    return {"number": 1, "title": "issue 1", "closedAt": "2026-10-15T09:00:00Z", "body": body, "comments": []}


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _printed(body: str, root: Path, run: bool = False, timeout: float | None = None) -> list[str]:
    candidates = recount.find_candidates(SINCE, runner=_FakeGh([_issue(body)]))
    text = recount.format_candidates(candidates, SINCE, root=root, run=run, timeout=timeout)
    return text.splitlines()


def test_ok_script_when_the_file_exists(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "print('count: 1')\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py --since 2026-10-01"), tmp_path)
    assert "    script: ok scripts/counts/7_x.py" in out


def test_backticked_count_line_is_read_the_same(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "print('count: 1')\n")
    out = _printed(_block("- Count: `python -I scripts/counts/7_x.py --since 2026-10-01`"), tmp_path)
    assert "    script: ok scripts/counts/7_x.py" in out


def test_absent_file_is_missing_with_its_path(tmp_path: Path) -> None:
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py --since 2026-10-01"), tmp_path)
    assert "    script: missing scripts/counts/7_x.py" in out


def test_prose_count_line_is_missing_with_its_first_60_chars(tmp_path: Path) -> None:
    assert len(PROSE[:60]) == 60
    out = _printed(_block(f"- Count: {PROSE}"), tmp_path)
    assert f"    script: missing {PROSE[:60]}" in out


def test_block_without_a_count_line_is_missing(tmp_path: Path) -> None:
    out = _printed("## Recount\n- incident: one\n- how counted: two", tmp_path)
    assert "    script: missing no Count line in the block" in out


@pytest.mark.parametrize("named", ["scripts/other/x.py", "../x.py"])
def test_path_outside_scripts_counts_is_missing_and_never_run(tmp_path: Path, named: str) -> None:
    root = tmp_path / "repo"
    marker = tmp_path / "marker.txt"
    _write(tmp_path, named.replace("../", ""), f"open(r'{marker}', 'w').write('ran')\nprint('count: 9')\n")
    out = _printed(_block(f"- Count: python -I {named} --since 2026-10-01"), root, run=True)
    assert f"    script: missing {named}" in out
    assert not marker.exists()
    assert not any(line.strip().startswith("count:") for line in out)


def test_without_run_nothing_is_run(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    _write(tmp_path, "scripts/counts/7_x.py", f"open(r'{marker}', 'w').write('ran')\nprint('count: 42')\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py --since 2026-10-01"), tmp_path)
    assert "    script: ok scripts/counts/7_x.py" in out
    assert not marker.exists()
    assert "    count: 42" not in out


def test_run_prints_the_last_stdout_line(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "print('warming up')\nprint('count: 42')\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py"), tmp_path, run=True)
    assert "    count: 42" in out


def test_args_after_the_file_name_are_passed(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "import sys\nprint('count: ' + str(len(sys.argv) - 1))\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py --since 2026-10-01"), tmp_path, run=True)
    assert "    count: 2" in out


def test_failing_script_prints_its_return_code(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "import sys\nsys.exit(3)\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py"), tmp_path, run=True)
    assert "    failed: rc 3" in out


def test_script_past_the_timeout_prints_timed_out(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "import time\ntime.sleep(30)\n")
    out = _printed(_block("- Count: python -I scripts/counts/7_x.py"), tmp_path, run=True, timeout=1)
    assert "    timed out" in out


def test_cli_run_flag_runs_the_ok_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "scripts/counts/7_x.py", "print('count: 42')\n")
    monkeypatch.setattr(recount, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(recount, "run_gh", _FakeGh([_issue(_block("- Count: python -I scripts/counts/7_x.py"))]))
    assert main(["recount", "--since", "2026-10-01", "--run"]) == 0
    out = capsys.readouterr().out
    assert "    script: ok scripts/counts/7_x.py" in out
    assert "    count: 42" in out
