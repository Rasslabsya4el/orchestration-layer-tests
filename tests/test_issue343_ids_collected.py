"""#343 unit 3: every id the merge's plan sends to the GitHub job is collected there, whatever text its parameters hold.

The cause (measured on the issue-422 runs): the PC built the plan's ids with pytest 9.0.3, the GitHub job installs the
newest pytest (9.2.0), and 9.2.0 replaces the auto-generated id of a str parameter longer than 100 characters with
``<argname><index>`` (``parametrize_long_str_id_strategy`` = ``short``). The plan named ``f[<the whole text>]``, the job
collected ``f[headline0]``: ``ids sent but not collected`` for exactly the long-text parameters. The repo's own pytest
config pins the strategy, so the PC and the job spell an id the same way whichever pytest each has.

These tests run real pytest sessions in ``tmp_path`` over a planted parametrized test: newlines, quotes, backslashes,
non-ASCII, a text over 100 characters.
"""

from __future__ import annotations

import base64
import configparser
import gzip
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops.github_run import encode_ids
from tools.agentops.util import no_window_flags

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "parametrize_long_str_id_strategy"
LONG = "a long parameter text, over a hundred characters, as a recorded reply is: " + "word " * 30
PLANTED = f'''
import pytest

TEXTS = [
    "line one\\nline two\\n\\nline four",
    'both "double" and \\'single\\' quotes',
    "back\\\\slash and C:\\\\Work\\\\var",
    "\\u201cquoted\\u201d \\u2192 arrow \\u2028 separator \\x0b tab",
    {LONG!r},
    {(LONG + chr(10) + LONG + chr(0x201c))!r},
]


@pytest.mark.parametrize("text", TEXTS)
def test_text(text):
    assert text


@pytest.mark.parametrize("text,flag", [(t, f) for t in TEXTS[:2] for f in (True, False)])
def test_pair(text, flag):
    assert text
'''


def _project(tmp_path: Path, extra_ini: str = "", planted: str = PLANTED) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    (proj / "pytest.ini").write_text(ini + extra_ini, encoding="utf-8")
    (proj / "tests" / "test_planted.py").write_text(planted, encoding="utf-8")
    return proj


def _pytest(proj: Path, args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    for key in ("TEST_SHARD", "TEST_IDS_FILE", "TEST_SKIP_FILE", "TEST_PC_ONLY"):
        env.pop(key, None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=proj, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          creationflags=no_window_flags())


def _knows_long_str_strategy() -> bool:
    return STRATEGY in subprocess.run([sys.executable, "-m", "pytest", "--help"], capture_output=True, text=True,
                                      encoding="utf-8", timeout=60, creationflags=no_window_flags()).stdout


def _plan_ids(proj: Path, pc_args: list[str]) -> list[str]:
    """The ids the plan lists: a ``--collect-only -q`` of the PC's pytest, one node id per line (cut on ``\\n`` only)."""
    out = _pytest(proj, ["--collect-only", "-q", *pc_args]).stdout
    return [line.rstrip("\r") for line in out.split("\n") if "::" in line]


def _the_job(proj: Path, ids: list[str]) -> subprocess.CompletedProcess:
    """What the workflow does: the ``ids`` input is ``encode_ids`` (gzip + base64), decoded to ``ids.txt``, then the part runs."""
    file = proj / "ids.txt"
    file.write_bytes(gzip.decompress(base64.b64decode(encode_ids(ids))))
    return _pytest(proj, ["tests", "-q", "-p", "tools.agentops.test_shard_plugin"],
                   {"TEST_SHARD": "1/1", "TEST_IDS_FILE": str(file)})


def _the_pc_that_still_keeps_long_ids() -> list[str]:
    """The PC's pytest 9.0.3 spells a long text in the id; a pytest that knows the setting is told to do the same."""
    return ["-o", f"{STRATEGY}=legacy"] if _knows_long_str_strategy() else []


def test_the_plan_lists_every_planted_id_and_the_job_collects_all_of_them(tmp_path):
    proj = _project(tmp_path)
    ids = _plan_ids(proj, _the_pc_that_still_keeps_long_ids())
    assert len(ids) == 10 and len(set(ids)) == 10  # 6 texts + 2 texts x 2 flags
    assert any("line one\\nline two" in i for i in ids) and any("\\u201cquoted" in i for i in ids)
    assert any(LONG in i for i in ids)  # the PC's id holds the whole long text
    done = _the_job(proj, ids)
    assert "not collected" not in done.stdout + done.stderr
    assert done.returncode == 0, done.stdout + done.stderr
    assert "10 passed" in done.stdout


def test_the_job_collects_a_plan_made_by_the_job_s_own_pytest_too(tmp_path):
    proj = _project(tmp_path)
    ids = _plan_ids(proj, [])
    done = _the_job(proj, ids)
    assert "not collected" not in done.stdout + done.stderr
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_repo_pytest_config_pins_the_spelling_of_a_long_text_in_an_id():
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    assert parser["pytest"].get(STRATEGY) == "legacy"


def test_the_not_collected_line_names_the_running_pytest(tmp_path):
    proj = _project(tmp_path)
    ids = _plan_ids(proj, []) + ["tests/test_planted.py::test_text[no such id]"]
    done = _the_job(proj, ids)
    assert done.returncode == 4
    assert f"pytest {pytest.__version__}: ids sent but not collected: 1 of 11: tests/test_planted.py::test_text[no such id]" in (
        done.stdout + done.stderr)


@pytest.mark.parametrize("workflow", ["tests.yml", "map.yml"])
def test_every_job_prints_its_pytest_version_right_after_installing_it(workflow):
    lines = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8").splitlines()
    installs = [n for n, line in enumerate(lines) if "pip install pytest" in line]
    assert installs
    for n in installs:
        assert lines[n + 1].strip() == "- run: python -m pytest --version"
