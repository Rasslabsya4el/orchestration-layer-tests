"""#954: `mods test` takes its pass/fail numbers from the runner's own summary line only (cloud-ready issue).

The summary shapes are the runner's: bun prints `362 pass` and `0 fail` on two lines (then `Ran 362 tests ...`),
the issue's form is `362 pass / 0 fail` on one line, pytest prints `2 failed, 7 passed in 1s`. A test name such as
`#944 failing writes` or `12 pass by default` is never a summary line, wherever it sits in the output.
"""

import sys

from tools.agentops import mods

NAMES = (
    "(pass) 12 pass by default [1.00ms]\n"
    "(fail) #944 failing writes [2.00ms]\n"
    "#944 failing writes: tree length 82811, control 82811, identical true\n"
)
ONE_LINE = "362 pass / 0 fail\n"
TWO_LINES = " 362 pass\n 0 fail\nRan 362 tests across 101 files. [95.56s]\n"


def test_summary_line_wins_when_the_test_names_follow_it():
    # the order the old stdout + stderr join produced: the runner's summary first, its test lines after
    assert mods.parse_counts(ONE_LINE + NAMES) == (362, 0)
    assert mods.parse_counts(TWO_LINES + NAMES) == (362, 0)


def test_summary_line_wins_when_the_test_names_come_first():
    assert mods.parse_counts(NAMES + ONE_LINE) == (362, 0)
    assert mods.parse_counts(NAMES + TWO_LINES) == (362, 0)


def test_pytest_and_bun_forms_are_still_read():
    assert mods.parse_counts("49 passed in 3.2s\n") == (49, 0)
    assert mods.parse_counts("2 failed, 7 passed in 1s\n") == (7, 2)
    assert mods.parse_counts("18 pass\n1 fail\n") == (18, 1)


def test_no_summary_line_reads_no_number():
    assert mods.parse_counts(NAMES) is None
    assert mods.parse_counts("error: no *.test.ts or *.test.tsx under .\n") is None
    assert mods.parse_counts("") is None


def test_no_summary_line_prints_a_plain_line_and_the_command_exits_nonzero(tmp_path, monkeypatch, capsys):
    mod = tmp_path / "mods" / "m"
    (mod / "test").mkdir(parents=True)
    (mod / "test" / "a.test.ts").write_text("")
    monkeypatch.setattr(mods, "repo_root", lambda: tmp_path)

    def runner(cmd, cwd):
        if cmd[1:3] == ["plugin", "validate"]:
            return 0, "Validation passed\n"
        return 0, NAMES  # exit 0, but no summary line anywhere

    rc = mods.run_test("m", runner)
    out = capsys.readouterr().out
    assert rc != 0
    assert "no summary line" in out
    assert "pass, " not in out and "fail, " not in out


def test_default_runner_keeps_stderr_in_the_runners_own_order():
    # stderr written first, stdout second: the output must keep that order, so its last line is the runner's last line
    code, out = mods._default_runner(
        [sys.executable, "-c",
         "import sys; print('stderr-first', file=sys.stderr, flush=True); print('stdout-second', flush=True)"],
        mods.repo_root())
    assert code == 0
    assert out == "stderr-first\nstdout-second\n"
